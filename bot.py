import asyncio
import csv
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands
import requests
from dotenv import load_dotenv

load_dotenv()

# -----------------------------
# Configuration
# -----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CLIPROXY_KEY = os.getenv("CLIPROXY_KEY", "").strip()
CLIPROXY_TOKEN = os.getenv("CLIPROXY_TOKEN", "").strip()

# 900 seconds = 15 minutes
CHECK_INTERVAL = max(60, int(os.getenv("CHECK_INTERVAL", "900")))

# At most this many Cliproxy requests run at once.
MAX_CONCURRENT_REQUESTS = max(
    1, int(os.getenv("MAX_CONCURRENT_REQUESTS", "10"))
)

MONITORS_FILE = Path(os.getenv("MONITORS_FILE", "monitors.json"))

ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

CLIPROXY_URL = "https://api.cliproxy.com/v1/ipList"

CLIPROXY_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "Origin": "https://dash.cliproxy.com",
    "Referer": "https://dash.cliproxy.com/",
    "User-Agent": "Mozilla/5.0",
}

# -----------------------------
# Persistent monitor storage
# -----------------------------
monitors_lock = asyncio.Lock()
monitor_cycle_lock = asyncio.Lock()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None

    try:
        result = datetime.fromisoformat(str(value))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result
    except (ValueError, TypeError):
        return None


def load_monitors() -> dict[str, dict[str, Any]]:
    if not MONITORS_FILE.exists():
        return {}

    try:
        raw = json.loads(
            MONITORS_FILE.read_text(encoding="utf-8")
        )
    except Exception as exc:
        print(f"Could not read {MONITORS_FILE}: {exc}")
        return {}

    if isinstance(raw, dict):
        for monitor in raw.values():
            if not isinstance(monitor, dict):
                continue
            prefix = str(monitor.get("prefix", "")).strip()
            if prefix and not prefix.endswith("."):
                parts = prefix.split(".")
                if len(parts) == 3 and all(x.isdigit() for x in parts):
                    monitor["prefix"] = prefix + "."
        return raw

    # Older versions used a list. Do not let that crash the new bot.
    print(
        "WARNING: Old monitors.json format detected. "
        "Starting with an empty monitor database."
    )
    return {}


monitors: dict[str, dict[str, Any]] = load_monitors()


def save_monitors_sync() -> None:
    MONITORS_FILE.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix="monitors_",
        suffix=".tmp",
        dir=str(MONITORS_FILE.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                monitors,
                f,
                indent=2,
                ensure_ascii=False,
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_name, MONITORS_FILE)

    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


async def save_monitors() -> None:
    async with monitors_lock:
        await asyncio.to_thread(save_monitors_sync)


# -----------------------------
# Validation / naming
# -----------------------------
PREFIX_RE = re.compile(r"^(?:\d{1,3}\.){2}\d{1,3}$")


def normalize_prefix(value: str) -> Optional[str]:
    value = value.strip()

    # Accept both 47.148.2 and 47.148.2., but ALWAYS
    # normalize to the exact Cliproxy subnet form: 47.148.2.
    if value.endswith("."):
        value = value[:-1].strip()

    if not PREFIX_RE.fullmatch(value):
        return None

    parts = value.split(".")

    if any(int(part) > 255 for part in parts):
        return None

    return value + "."


def normalize_country(value: str) -> Optional[str]:
    value = value.strip().upper()

    if not re.fullmatch(r"[A-Z]{2}", value):
        return None

    return value


def normalize_name(value: str) -> str:
    return " ".join(value.strip().split())


def name_key(value: str) -> str:
    return normalize_name(value).casefold()


def user_monitors(
    user_id: int,
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (key, monitor)
        for key, monitor in monitors.items()
        if int(monitor.get("user_id", 0)) == user_id
    ]


def find_monitor_by_name(
    user_id: int,
    name: str,
) -> Optional[tuple[str, dict[str, Any]]]:
    wanted = name_key(name)

    for key, monitor in user_monitors(user_id):
        if name_key(str(monitor.get("name", ""))) == wanted:
            return key, monitor

    return None


def make_key(user_id: int) -> str:
    import uuid
    return f"{user_id}:{uuid.uuid4().hex}"


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    return str(value).strip().casefold() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def display_status(monitor: dict[str, Any]) -> str:
    status = monitor.get("status", "waiting")

    if status == "online":
        return "🟢 ONLINE"

    if status == "offline":
        return "🔴 OFFLINE"

    if status == "paused":
        return "⏸️ PAUSED"

    if status == "error":
        return "🟡 API ERROR"

    return "⚪ WAITING"


# -----------------------------
# Cliproxy request
# -----------------------------
def cliproxy_search_sync(
    prefix: str,
    country: str,
) -> Optional[list[dict[str, Any]]]:

    data = {
        "country": country,
        "state": "",
        "city": "",
        "asn": "",
        "key": CLIPROXY_KEY,

        # IMPORTANT: send the exact 3-octet subnet WITH
        # the trailing dot, e.g. 47.148.2.
        "ipc": prefix,

        "lang": "en",
        "token": CLIPROXY_TOKEN,
    }

    try:
        response = requests.post(
            CLIPROXY_URL,
            headers=CLIPROXY_HEADERS,
            data=data,
            timeout=30,
        )

        if response.status_code == 429:
            print(
                f"Cliproxy rate limited {prefix}/{country}"
            )
            return None

        response.raise_for_status()

        payload = response.json()

        if payload.get("code") != 0:
            print(
                f"Cliproxy API error for "
                f"{prefix}/{country}: "
                f"{payload.get('msg')}"
            )
            return None

        results = payload.get("data", [])

        if not isinstance(results, list):
            return []

        # Cliproxy may mask the second octet as "*", but the
        # first and third octets must still match the requested
        # subnet. For 47.148.2. the accepted form is 47.*.2.<host>.
        requested = prefix.rstrip(".").split(".")
        if len(requested) != 3:
            return []

        filtered = []
        for item in results:
            if not isinstance(item, dict):
                continue
            ip = str(item.get("ip", "")).strip()
            parts = ip.split(".")
            if len(parts) != 4:
                continue
            if (parts[0] == requested[0] and
                (parts[1] == "*" or parts[1] == requested[1]) and
                parts[2] == requested[2]):
                filtered.append(item)

        return filtered

    except (requests.RequestException, ValueError) as exc:
        print(
            f"Cliproxy request failed for "
            f"{prefix}/{country}: {exc}"
        )
        return None


# -----------------------------
# Discord bot
# -----------------------------
intents = discord.Intents.default()
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)

monitor_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_REQUESTS
)

monitor_task: Optional[asyncio.Task] = None


async def allowed_user(
    interaction: discord.Interaction,
) -> bool:

    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message(
            "You are not authorized to use this bot.",
            ephemeral=True,
        )
        return False

    return True


async def do_search(
    monitor: dict[str, Any],
) -> Optional[list[dict[str, Any]]]:

    async with monitor_semaphore:
        return await asyncio.to_thread(
            cliproxy_search_sync,
            monitor["prefix"],
            monitor["country"],
        )


def result_ips(
    results: list[dict[str, Any]],
) -> list[str]:

    ips = []

    for result in results:
        if isinstance(result, dict):
            ip = result.get("ip")

            if ip:
                ips.append(str(ip))

    return ips


async def dm_user(
    user_id: int,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> bool:

    try:
        user = (
            bot.get_user(user_id)
            or await bot.fetch_user(user_id)
        )

        if not user:
            return False

        await user.send(
            content=content,
            embed=embed,
            view=view,
        )

        return True

    except (discord.Forbidden, discord.HTTPException) as exc:
        print(
            f"Could not DM user {user_id}: {exc}"
        )
        return False


# -----------------------------
# Pause buttons
# -----------------------------
class PauseView(discord.ui.View):
    """
    Persistent buttons.

    The custom IDs are unique to the bot and the buttons
    remain usable after a restart because the same view
    is registered in setup_hook().
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def pause_watch(
        self,
        interaction: discord.Interaction,
        days: int,
    ) -> None:

        # Find the watch from the found-notification embed.
        if (
            not interaction.message
            or not interaction.message.embeds
        ):
            await interaction.response.send_message(
                "I couldn't identify this watch.",
                ephemeral=True,
            )
            return

        embed = interaction.message.embeds[0]

        name = None

        for field in embed.fields:
            if field.name.casefold() == "name":
                name = field.value.strip()
                break

        if not name:
            await interaction.response.send_message(
                "I couldn't identify this watch.",
                ephemeral=True,
            )
            return

        found = find_monitor_by_name(
            interaction.user.id,
            name,
        )

        if not found:
            await interaction.response.send_message(
                "I couldn't find that watch under your account.",
                ephemeral=True,
            )
            return

        _, monitor = found

        # Save the state to return to after the pause.
        previous_status = monitor.get(
            "status",
            "waiting",
        )

        if previous_status not in {
            "waiting",
            "online",
            "offline",
        }:
            previous_status = "waiting"

        monitor["resume_status"] = previous_status
        monitor["status"] = "paused"

        resume_at = (
            now_utc()
            + timedelta(days=days)
        )

        monitor["paused_until"] = resume_at.isoformat()
        monitor["updated_at"] = now_iso()

        await save_monitors()

        await interaction.response.edit_message(
            content=(
                f"⏸️ **{monitor['name']}** is paused "
                f"for **{days} days**.\n\n"
                f"It will automatically resume on "
                f"<t:{int(resume_at.timestamp())}:F>."
            ),
            embed=None,
            view=None,
        )

    @discord.ui.button(
        label="Pause 4 Days",
        style=discord.ButtonStyle.secondary,
        emoji="⏸️",
        custom_id="cliproxy_pause_4_days",
    )
    async def pause_4_days(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        await self.pause_watch(
            interaction,
            4,
        )

    @discord.ui.button(
        label="Pause 7 Days",
        style=discord.ButtonStyle.secondary,
        emoji="⏸️",
        custom_id="cliproxy_pause_7_days",
    )
    async def pause_7_days(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        await self.pause_watch(
            interaction,
            7,
        )


# -----------------------------
# Notifications
# -----------------------------
async def send_found_notification(
    monitor: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:

    ips = result_ips(results)

    shown = "\n".join(
        f"• `{ip}`"
        for ip in ips[:10]
    )

    if not shown:
        shown = "• Match found"

    if len(ips) > 10:
        shown += (
            f"\n• …and {len(ips) - 10} more"
        )

    embed = discord.Embed(
        title="🟢 CLIPROXY PROXY FOUND",
        description=(
            "A matching result was found in Cliproxy.\n\n"
            "The bot will automatically continue monitoring "
            "this 3-segment prefix and will notify you if it "
            "goes offline."
        ),
    )

    embed.add_field(
        name="Name",
        value=monitor["name"],
        inline=False,
    )

    embed.add_field(
        name="3 Seg",
        value=f"`{monitor['prefix']}xxx`",
        inline=True,
    )

    embed.add_field(
        name="Country",
        value=monitor["country"],
        inline=True,
    )

    embed.add_field(
        name="Matches",
        value=str(len(results)),
        inline=True,
    )

    embed.add_field(
        name="Cliproxy result(s)",
        value=shown,
        inline=False,
    )

    await dm_user(
        int(monitor["user_id"]),
        embed=embed,
        view=PauseView(),
    )


async def send_offline_notification(
    monitor: dict[str, Any],
) -> None:

    embed = discord.Embed(
        title="🔴 CLIPROXY PROXY OFFLINE",
        description=(
            "The previously detected proxy is no longer "
            "available.\n\n"
            "The bot will continue monitoring it every "
            "15 minutes and will notify you if it comes "
            "back online."
        ),
    )

    embed.add_field(
        name="Name",
        value=monitor["name"],
        inline=False,
    )

    embed.add_field(
        name="3 Seg",
        value=f"`{monitor['prefix']}xxx`",
        inline=True,
    )

    embed.add_field(
        name="Country",
        value=monitor["country"],
        inline=True,
    )

    await dm_user(
        int(monitor["user_id"]),
        embed=embed,
    )


async def send_back_online_notification(
    monitor: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:

    ips = result_ips(results)

    shown = "\n".join(
        f"• `{ip}`"
        for ip in ips[:10]
    )

    if not shown:
        shown = "• Match found"

    if len(ips) > 10:
        shown += (
            f"\n• …and {len(ips) - 10} more"
        )

    embed = discord.Embed(
        title="🟢 CLIPROXY PROXY BACK ONLINE",
        description=(
            "A matching result is available again.\n\n"
            "The bot will continue monitoring it."
        ),
    )

    embed.add_field(
        name="Name",
        value=monitor["name"],
        inline=False,
    )

    embed.add_field(
        name="3 Seg",
        value=f"`{monitor['prefix']}xxx`",
        inline=True,
    )

    embed.add_field(
        name="Country",
        value=monitor["country"],
        inline=True,
    )

    embed.add_field(
        name="Matches",
        value=str(len(results)),
        inline=True,
    )

    embed.add_field(
        name="Cliproxy result(s)",
        value=shown,
        inline=False,
    )

    await dm_user(
        int(monitor["user_id"]),
        embed=embed,
    )


# -----------------------------
# Monitor state machine
# -----------------------------
async def check_one(
    key: str,
    monitor_snapshot: dict[str, Any],
) -> None:

    results = await do_search(
        monitor_snapshot
    )

    # None means network/API/rate-limit error.
    # Never interpret that as offline.
    if results is None:
        return

    should_notify_found = False
    should_notify_offline = False
    should_notify_back_online = False

    async with monitors_lock:
        monitor = monitors.get(key)

        if not monitor:
            return

        old_status = monitor.get(
            "status",
            "waiting",
        )

        monitor["last_checked"] = now_iso()
        monitor["last_error"] = None

        if results:
            monitor["status"] = "online"
            monitor["found"] = True
            monitor["found_before"] = True

            monitor["last_match"] = (
                result_ips(results)[:25]
            )

            monitor["last_match_count"] = len(
                results
            )

            # WAITING -> ONLINE:
            # send the initial "found" notification.
            if old_status == "waiting":
                should_notify_found = True

            # OFFLINE -> ONLINE:
            # send a back-online notification.
            elif old_status == "offline":
                if monitor.get(
                    "notify_offline",
                    True,
                ):
                    should_notify_back_online = True

        else:
            monitor["last_match"] = []
            monitor["last_match_count"] = 0

            if (
                old_status == "online"
                and monitor.get("found_before", False)
            ):
                # ONLINE -> OFFLINE
                monitor["status"] = "offline"

                if monitor.get(
                    "notify_offline",
                    True,
                ):
                    should_notify_offline = True

            elif monitor.get(
                "found_before",
                False,
            ):
                monitor["status"] = "offline"

            else:
                # It has never been found.
                monitor["status"] = "waiting"

        monitor["updated_at"] = now_iso()

        # Save before sending a DM so the state is not lost
        # if Discord notification delivery fails.
        await asyncio.to_thread(
            save_monitors_sync
        )

    if should_notify_found:
        await send_found_notification(
            monitor,
            results,
        )

    elif should_notify_offline:
        await send_offline_notification(
            monitor,
        )

    elif should_notify_back_online:
        await send_back_online_notification(
            monitor,
            results,
        )


async def run_monitor_cycle() -> None:
    async with monitor_cycle_lock:
        snapshot = []

        for key, monitor in list(
            monitors.items()
        ):
            if not monitor.get(
                "active",
                True,
            ):
                continue

            status = monitor.get(
                "status",
                "waiting",
            )

            # -------------------------
            # Handle paused watches
            # -------------------------
            if status == "paused":
                paused_until = parse_iso(
                    monitor.get(
                        "paused_until"
                    )
                )

                if (
                    paused_until
                    and now_utc() < paused_until
                ):
                    continue

                # Pause expired.
                # Restore the status from before the pause.
                restored = monitor.get(
                    "resume_status",
                    "waiting",
                )

                if restored not in {
                    "waiting",
                    "online",
                    "offline",
                }:
                    restored = "waiting"

                monitor["status"] = restored
                monitor["resume_status"] = None
                monitor["paused_until"] = None
                monitor["updated_at"] = now_iso()

                # Check immediately when the pause expires.
                snapshot.append(
                    (
                        key,
                        dict(monitor),
                    )
                )
                continue

            if status in {
                "waiting",
                "online",
                "offline",
            }:
                snapshot.append(
                    (
                        key,
                        dict(monitor),
                    )
                )

        await asyncio.to_thread(
            save_monitors_sync
        )

        if not snapshot:
            return

        print(
            f"Starting monitor cycle for "
            f"{len(snapshot)} watches..."
        )

        # All watches can be scheduled here, but the semaphore
        # limits actual simultaneous HTTP requests.
        await asyncio.gather(
            *(
                check_one(
                    key,
                    monitor,
                )
                for key, monitor in snapshot
            ),
            return_exceptions=True,
        )

        print("Monitor cycle complete.")


async def monitor_loop() -> None:
    await bot.wait_until_ready()

    while not bot.is_closed():
        started = (
            asyncio.get_running_loop().time()
        )

        try:
            await run_monitor_cycle()

        except Exception as exc:
            print(
                f"Monitor cycle error: {exc}"
            )

        elapsed = (
            asyncio.get_running_loop().time()
            - started
        )

        sleep_for = max(
            5,
            CHECK_INTERVAL - elapsed,
        )

        await asyncio.sleep(
            sleep_for
        )


# -----------------------------
# List pagination
# -----------------------------
class ListView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        pages: list[discord.Embed],
    ):
        super().__init__(timeout=300)

        self.user_id = user_id
        self.pages = pages
        self.page = 0

        self.update_buttons()

    def update_buttons(self) -> None:
        self.previous.disabled = (
            self.page == 0
        )

        self.next.disabled = (
            self.page >= len(self.pages) - 1
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:

        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This list belongs to another user.",
                ephemeral=True,
            )
            return False

        return True

    async def show_page(
        self,
        interaction: discord.Interaction,
    ) -> None:

        self.update_buttons()

        await interaction.response.edit_message(
            embed=self.pages[self.page],
            view=self,
        )

    @discord.ui.button(
        label="◀ Previous",
        style=discord.ButtonStyle.secondary,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        self.page -= 1
        await self.show_page(
            interaction
        )

    @discord.ui.button(
        label="Next ▶",
        style=discord.ButtonStyle.secondary,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:

        self.page += 1
        await self.show_page(
            interaction
        )


# -----------------------------
# Commands
# -----------------------------
@bot.tree.command(
    name="watch",
    description="Add a named 3-segment Cliproxy monitor.",
)
@app_commands.describe(
    three_seg=(
        "3-segment prefix, e.g. "
        "180.183.130 or 180.183.130."
    ),
    country="Country code, e.g. US",
    name=(
        "Example: Triple, Double, Single. "
        "BE SPECIFIC SO YOU REMEMBER WHAT THIS SEARCH IS FOR."
    ),
)
async def watch(
    interaction: discord.Interaction,
    three_seg: str,
    country: str,
    name: str,
) -> None:

    if not await allowed_user(interaction):
        return

    prefix = normalize_prefix(
        three_seg
    )

    country = normalize_country(
        country
    )

    name = normalize_name(
        name
    )

    if not prefix:
        await interaction.response.send_message(
            (
                "⚠️ Invalid 3-segment prefix. "
                "Use something like "
                "`180.183.130` or `180.183.130.`"
            ),
            ephemeral=True,
        )
        return

    if not country:
        await interaction.response.send_message(
            (
                "⚠️ Country must be a "
                "2-letter code such as `US` or `TH`."
            ),
            ephemeral=True,
        )
        return

    if not name or len(name) > 80:
        await interaction.response.send_message(
            (
                "⚠️ Please enter a watch name "
                "between 1 and 80 characters."
            ),
            ephemeral=True,
        )
        return

    if find_monitor_by_name(
        interaction.user.id,
        name,
    ):
        await interaction.response.send_message(
            (
                "⚠️ **This name is already in use.**\n\n"
                f"Please use a different name, such as "
                f"`{name}2` or `{name}3`."
            ),
            ephemeral=True,
        )
        return

    key = make_key(
        interaction.user.id
    )

    monitors[key] = {
        "user_id": interaction.user.id,
        "name": name,
        "prefix": prefix,
        "country": country,
        "active": True,

        # Initial state.
        "found": False,
        "found_before": False,
        "status": "waiting",

        # Offline monitoring is automatic.
        "notify_offline": True,

        # Pause support.
        "paused_until": None,
        "resume_status": None,

        "last_checked": None,
        "last_match": [],
        "last_match_count": 0,
        "last_error": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }

    await save_monitors()

    await interaction.response.send_message(
        (
            "✅ **Watch created.**\n\n"
            f"**Name:** {name}\n"
            f"**3 Seg:** `{prefix}.xxx`\n"
            f"**Country:** `{country}`\n\n"
            f"The bot will check it every "
            f"{CHECK_INTERVAL // 60} minutes."
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="list",
    description="View your Cliproxy watches with pagination.",
)
@app_commands.describe(
    filter="Optional filter: all, online, offline, waiting, paused",
)
@app_commands.choices(
    filter=[
        app_commands.Choice(
            name="All",
            value="all",
        ),
        app_commands.Choice(
            name="Online",
            value="online",
        ),
        app_commands.Choice(
            name="Offline",
            value="offline",
        ),
        app_commands.Choice(
            name="Waiting",
            value="waiting",
        ),
        app_commands.Choice(
            name="Paused",
            value="paused",
        ),
    ]
)
async def list_watches(
    interaction: discord.Interaction,
    filter: Optional[
        app_commands.Choice[str]
    ] = None,
) -> None:

    if not await allowed_user(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    selected_filter = (
        filter.value
        if filter
        else "all"
    )

    all_items = user_monitors(
        interaction.user.id
    )

    if selected_filter == "all":
        items = all_items
    else:
        items = [
            (key, monitor)
            for key, monitor in all_items
            if monitor.get(
                "status",
                "waiting",
            ) == selected_filter
        ]

    items.sort(
        key=lambda pair: str(
            pair[1].get(
                "name",
                "",
            )
        ).casefold()
    )

    if not items:
        await interaction.followup.send(
            "No watches match that filter.",
            ephemeral=True,
        )
        return

    counts = {
        "online": 0,
        "offline": 0,
        "waiting": 0,
        "paused": 0,
    }

    for _, monitor in all_items:
        status = monitor.get(
            "status",
            "waiting",
        )

        if status in counts:
            counts[status] += 1

    chunk_size = 8
    pages: list[discord.Embed] = []

    total_pages = (
        len(items)
        + chunk_size
        - 1
    ) // chunk_size

    for start in range(
        0,
        len(items),
        chunk_size,
    ):
        chunk = items[
            start:start + chunk_size
        ]

        page_number = (
            len(pages) + 1
        )

        embed = discord.Embed(
            title="📊 Cliproxy Monitor",
            description=(
                f"**Total:** {len(all_items)}  "
                f"🟢 {counts['online']}  "
                f"🔴 {counts['offline']}  "
                f"⚪ {counts['waiting']}  "
                f"⏸️ {counts['paused']}\n"
                f"Filter: **{selected_filter}**\n"
                f"Page **{page_number}/{total_pages}**"
            ),
        )

        lines = []

        for index, (_, monitor) in enumerate(
            chunk,
            start=start + 1,
        ):
            safe_name = str(monitor.get("name", ""))
            if len(safe_name) > 50:
                safe_name = safe_name[:47] + "..."

            lines.append(
                f"**{index}. "
                f"{safe_name}** — "
                f"`{monitor['prefix']}` / "
                f"`{monitor['country']}` — "
                f"{display_status(monitor)}"
            )

        embed.add_field(
            name="Watches",
            value="\n".join(lines),
            inline=False,
        )

        pages.append(embed)

    view = ListView(
        interaction.user.id,
        pages,
    )

    await interaction.followup.send(
        embed=pages[0],
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="searchwatch",
    description="Find one of your watches by name.",
)
@app_commands.describe(
    name="Watch name to search for",
)
async def searchwatch(
    interaction: discord.Interaction,
    name: str,
) -> None:

    if not await allowed_user(interaction):
        return

    matches = [
        (key, monitor)
        for key, monitor
        in user_monitors(
            interaction.user.id
        )
        if name.casefold()
        in str(
            monitor.get(
                "name",
                "",
            )
        ).casefold()
    ]

    matches.sort(
        key=lambda pair: str(
            pair[1].get(
                "name",
                "",
            )
        ).casefold()
    )

    if not matches:
        await interaction.response.send_message(
            "No watch names matched that search.",
            ephemeral=True,
        )
        return

    lines = []

    for _, monitor in matches[:20]:
        lines.append(
            f"**{monitor['name']}** — "
            f"`{monitor['prefix']}` / "
            f"`{monitor['country']}` — "
            f"{display_status(monitor)}"
        )

    if len(matches) > 20:
        lines.append(
            f"\n…and {len(matches) - 20} more. "
            "Use a more specific name."
        )

    await interaction.response.send_message(
        "🔎 **Watch Search**\n\n"
        + "\n".join(lines),
        ephemeral=True,
    )


@bot.tree.command(
    name="watchinfo",
    description="Show detailed information about one watch.",
)
@app_commands.describe(
    name="Exact watch name",
)
async def watchinfo(
    interaction: discord.Interaction,
    name: str,
) -> None:

    if not await allowed_user(interaction):
        return

    found = find_monitor_by_name(
        interaction.user.id,
        name,
    )

    if not found:
        await interaction.response.send_message(
            "No watch with that name was found.",
            ephemeral=True,
        )
        return

    _, monitor = found

    embed = discord.Embed(
        title="📡 WATCH INFORMATION"
    )

    embed.add_field(
        name="Name",
        value=monitor["name"],
        inline=False,
    )

    embed.add_field(
        name="3 Seg",
        value=f"`{monitor['prefix']}xxx`",
        inline=True,
    )

    embed.add_field(
        name="Country",
        value=monitor["country"],
        inline=True,
    )

    embed.add_field(
        name="Status",
        value=display_status(monitor),
        inline=True,
    )

    embed.add_field(
        name="Offline notifications",
        value=(
            "✅ Automatic"
            if monitor.get(
                "notify_offline",
                True,
            )
            else "❌ Disabled"
        ),
        inline=True,
    )

    embed.add_field(
        name="Matches",
        value=str(
            monitor.get(
                "last_match_count",
                0,
            )
        ),
        inline=True,
    )

    embed.add_field(
        name="Last checked",
        value=(
            monitor.get(
                "last_checked"
            )
            or "Not checked yet"
        ),
        inline=False,
    )

    if monitor.get("status") == "paused":
        paused_until = parse_iso(
            monitor.get(
                "paused_until"
            )
        )

        if paused_until:
            embed.add_field(
                name="Paused until",
                value=(
                    f"<t:{int(paused_until.timestamp())}:F>"
                ),
                inline=False,
            )

    last_match = (
        monitor.get(
            "last_match"
        )
        or []
    )

    embed.add_field(
        name="Last result(s)",
        value=(
            "\n".join(
                f"• `{x}`"
                for x in last_match[:10]
            )
            or "None"
        ),
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="stop",
    description="Stop one of your watches by name.",
)
@app_commands.describe(
    name="Exact watch name",
)
async def stop(
    interaction: discord.Interaction,
    name: str,
) -> None:

    if not await allowed_user(interaction):
        return

    found = find_monitor_by_name(
        interaction.user.id,
        name,
    )

    if not found:
        await interaction.response.send_message(
            "No watch with that name was found.",
            ephemeral=True,
        )
        return

    _, monitor = found

    monitor["active"] = False
    monitor["updated_at"] = now_iso()

    await save_monitors()

    await interaction.response.send_message(
        f"🛑 Stopped **{monitor['name']}**.",
        ephemeral=True,
    )


# -----------------------------
# /pause
# -----------------------------
@bot.tree.command(
    name="pause",
    description="Pause one of your watches for 4 or 7 days.",
)
@app_commands.describe(
    name="Exact watch name",
    duration="Choose 4 days or 7 days.",
)
@app_commands.choices(
    duration=[
        app_commands.Choice(
            name="4 days",
            value="4",
        ),
        app_commands.Choice(
            name="7 days",
            value="7",
        ),
    ]
)
async def pause(
    interaction: discord.Interaction,
    name: str,
    duration: app_commands.Choice[str],
) -> None:

    if not await allowed_user(interaction):
        return

    found = find_monitor_by_name(
        interaction.user.id,
        name,
    )

    if not found:
        await interaction.response.send_message(
            f"No watch named `{name}` was found.",
            ephemeral=True,
        )
        return

    _, monitor = found

    days = int(duration.value)

    current_status = monitor.get(
        "status",
        "waiting",
    )

    if current_status == "paused":
        await interaction.response.send_message(
            "That watch is already paused.",
            ephemeral=True,
        )
        return

    if current_status not in {
        "waiting",
        "online",
        "offline",
    }:
        current_status = "waiting"

    monitor["resume_status"] = current_status
    monitor["status"] = "paused"

    resume_at = (
        now_utc()
        + timedelta(days=days)
    )

    monitor["paused_until"] = (
        resume_at.isoformat()
    )

    monitor["updated_at"] = now_iso()

    await save_monitors()

    await interaction.response.send_message(
        (
            f"⏸️ **{monitor['name']}** is paused "
            f"for **{days} days**.\n\n"
            f"It will automatically resume on "
            f"<t:{int(resume_at.timestamp())}:F>."
        ),
        ephemeral=True,
    )


# -----------------------------
# /resume
# -----------------------------
@bot.tree.command(
    name="resume",
    description="Resume one of your paused watches immediately.",
)
@app_commands.describe(
    name="Exact watch name",
)
async def resume(
    interaction: discord.Interaction,
    name: str,
) -> None:

    if not await allowed_user(interaction):
        return

    found = find_monitor_by_name(
        interaction.user.id,
        name,
    )

    if not found:
        await interaction.response.send_message(
            f"No watch named `{name}` was found.",
            ephemeral=True,
        )
        return

    _, monitor = found

    if monitor.get("status") != "paused":
        await interaction.response.send_message(
            "That watch is not currently paused.",
            ephemeral=True,
        )
        return

    restored = monitor.get(
        "resume_status",
        "waiting",
    )

    if restored not in {
        "waiting",
        "online",
        "offline",
    }:
        restored = "waiting"

    monitor["status"] = restored
    monitor["resume_status"] = None
    monitor["paused_until"] = None
    monitor["updated_at"] = now_iso()

    await save_monitors()

    await interaction.response.send_message(
        (
            f"▶️ **{monitor['name']}** has been resumed.\n"
            "It will be checked during the next monitoring cycle."
        ),
        ephemeral=True,
    )


# -----------------------------
# Export / Import
# -----------------------------
@bot.tree.command(
    name="export",
    description="Export all of your Cliproxy watches to a backup file.",
)
async def export_watches(
    interaction: discord.Interaction,
) -> None:

    if not await allowed_user(interaction):
        return

    data = {
        "format": "cliproxy-monitor-backup",
        "version": 3,
        "exported_at": now_iso(),
        "watches": [],
    }

    for _, monitor in user_monitors(
        interaction.user.id
    ):
        data["watches"].append(
            {
                "name": monitor["name"],
                "prefix": monitor["prefix"],
                "country": monitor["country"],
                "notify_offline": bool(
                    monitor.get(
                        "notify_offline",
                        True,
                    )
                ),
                "active": bool(
                    monitor.get(
                        "active",
                        True,
                    )
                ),
            }
        )

    raw = json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")

    file = discord.File(
        io.BytesIO(raw),
        filename="cliproxy_watches_backup.json",
    )

    await interaction.response.send_message(
        (
            f"✅ Exported "
            f"**{len(data['watches'])}** watches. "
            "Keep this file somewhere safe."
        ),
        file=file,
        ephemeral=True,
    )


@bot.tree.command(
    name="import",
    description="Import watches from a JSON or CSV backup file.",
)
@app_commands.describe(
    file="Your Cliproxy watch backup (.json or .csv)",
)
async def import_watches(
    interaction: discord.Interaction,
    file: discord.Attachment,
) -> None:

    if not await allowed_user(interaction):
        return

    filename = file.filename.casefold()

    if not (
        filename.endswith(".json")
        or filename.endswith(".csv")
        or filename.endswith(".txt")
    ):
        await interaction.response.send_message(
            (
                "⚠️ Please upload a `.json`, "
                "`.csv`, or `.txt` backup file."
            ),
            ephemeral=True,
        )
        return

    if file.size and file.size > 2_000_000:
        await interaction.response.send_message(
            (
                "⚠️ That backup is too large. "
                "Please keep it under 2 MB."
            ),
            ephemeral=True,
        )
        return

    try:
        raw = await file.read()
        text_data = raw.decode(
            "utf-8-sig"
        )

    except Exception as exc:
        await interaction.response.send_message(
            f"⚠️ Could not read the file: {exc}",
            ephemeral=True,
        )
        return

    records: list[dict[str, Any]] = []

    try:
        if filename.endswith(".json"):
            payload = json.loads(
                text_data
            )

            if isinstance(payload, dict):
                records = payload.get(
                    "watches",
                    [],
                )
            elif isinstance(payload, list):
                records = payload

        else:
            reader = csv.DictReader(
                io.StringIO(text_data)
            )

            records = [
                dict(row)
                for row in reader
            ]

    except Exception as exc:
        await interaction.response.send_message(
            f"⚠️ Could not parse the backup: {exc}",
            ephemeral=True,
        )
        return

    added = 0
    skipped = 0
    reasons: dict[str, int] = {}

    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            reasons["invalid record"] = (
                reasons.get(
                    "invalid record",
                    0,
                )
                + 1
            )
            continue

        name = normalize_name(
            str(
                record.get(
                    "name",
                    "",
                )
            )
        )

        prefix = normalize_prefix(
            str(
                record.get(
                    "prefix",
                    record.get(
                        "three_seg",
                        "",
                    ),
                )
            )
        )

        country = normalize_country(
            str(
                record.get(
                    "country",
                    "",
                )
            )
        )

        if not name or len(name) > 80:
            skipped += 1
            reasons["invalid name"] = (
                reasons.get(
                    "invalid name",
                    0,
                )
                + 1
            )
            continue

        if not prefix:
            skipped += 1
            reasons["invalid prefix"] = (
                reasons.get(
                    "invalid prefix",
                    0,
                )
                + 1
            )
            continue

        if not country:
            skipped += 1
            reasons["invalid country"] = (
                reasons.get(
                    "invalid country",
                    0,
                )
                + 1
            )
            continue

        if find_monitor_by_name(
            interaction.user.id,
            name,
        ):
            skipped += 1
            reasons["duplicate name"] = (
                reasons.get(
                    "duplicate name",
                    0,
                )
                + 1
            )
            continue

        key = make_key(
            interaction.user.id
        )

        monitors[key] = {
            "user_id": interaction.user.id,
            "name": name,
            "prefix": prefix,
            "country": country,
            "active": parse_bool(
                record.get(
                    "active",
                    True,
                )
            ),
            "found": False,
            "found_before": False,
            "status": "waiting",
            "notify_offline": parse_bool(
                record.get(
                    "notify_offline",
                    True,
                )
            ),
            "paused_until": None,
            "resume_status": None,
            "last_checked": None,
            "last_match": [],
            "last_match_count": 0,
            "last_error": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }

        added += 1

    await save_monitors()

    reason_text = "\n".join(
        f"• {reason}: {count}"
        for reason, count in reasons.items()
    )

    response = (
        "✅ **Import complete**\n\n"
        f"Added: **{added}**\n"
        f"Skipped: **{skipped}**"
    )

    if reason_text:
        response += (
            "\n\n**Skipped because:**\n"
            + reason_text
        )

    await interaction.response.send_message(
        response,
        ephemeral=True,
    )


# -----------------------------
# Startup
# -----------------------------
@bot.event
async def on_ready():
    print(
        f"Logged in as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"Loaded {len(monitors)} saved watches"
    )

    print(
        f"Check interval: "
        f"{CHECK_INTERVAL}s "
        f"({CHECK_INTERVAL / 60:.1f} minutes)"
    )

    print(
        "Max concurrent Cliproxy requests: "
        f"{MAX_CONCURRENT_REQUESTS}"
    )


async def setup_hook():
    global monitor_task

    # Register persistent pause buttons so buttons from
    # previous DMs can still work after a restart.
    bot.add_view(
        PauseView()
    )

    # Force slash commands to be synchronized.
    await bot.tree.sync()

    print(
        "Slash commands synced."
    )

    if (
        monitor_task is None
        or monitor_task.done()
    ):
        monitor_task = asyncio.create_task(
            monitor_loop()
        )


bot.setup_hook = setup_hook


if __name__ == "__main__":
    missing = []

    if not DISCORD_TOKEN:
        missing.append(
            "DISCORD_TOKEN"
        )

    if not ALLOWED_USER_IDS:
        missing.append(
            "ALLOWED_USER_IDS"
        )

    if not CLIPROXY_KEY:
        missing.append(
            "CLIPROXY_KEY"
        )

    if not CLIPROXY_TOKEN:
        missing.append(
            "CLIPROXY_TOKEN"
        )

    if missing:
        raise SystemExit(
            "Missing required .env values: "
            + ", ".join(missing)
        )

    bot.run(
        DISCORD_TOKEN
    )
