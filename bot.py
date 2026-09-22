import asyncio
import csv
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone
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
CHECK_INTERVAL = max(60, int(os.getenv("CHECK_INTERVAL", "900")))  # 15 minutes
MAX_CONCURRENT_REQUESTS = max(1, int(os.getenv("MAX_CONCURRENT_REQUESTS", "10")))
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_monitors() -> dict[str, dict[str, Any]]:
    if not MONITORS_FILE.exists():
        return {}

    try:
        raw = json.loads(MONITORS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read {MONITORS_FILE}: {exc}")
        return {}

    if isinstance(raw, dict):
        return raw

    # Older bot versions used a list. Do not let that crash the new bot.
    print("WARNING: Old monitors.json format detected. Starting with an empty monitor database.")
    return {}


monitors: dict[str, dict[str, Any]] = load_monitors()


def save_monitors_sync() -> None:
    MONITORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="monitors_", suffix=".tmp", dir=str(MONITORS_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(monitors, f, indent=2, ensure_ascii=False)
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
# Validation / naming helpers
# -----------------------------
PREFIX_RE = re.compile(r"^(?:\d{1,3}\.){2}\d{1,3}$")


def normalize_prefix(value: str) -> Optional[str]:
    value = value.strip()
    if value.endswith("."):
        value = value[:-1].strip()

    if not PREFIX_RE.fullmatch(value):
        return None

    parts = value.split(".")
    if any(int(part) > 255 for part in parts):
        return None
    return value


def normalize_name(value: str) -> str:
    return " ".join(value.strip().split())


def name_key(value: str) -> str:
    return normalize_name(value).casefold()


def user_monitors(user_id: int) -> list[tuple[str, dict[str, Any]]]:
    return [
        (key, monitor)
        for key, monitor in monitors.items()
        if int(monitor.get("user_id", 0)) == user_id
    ]


def find_monitor_by_name(user_id: int, name: str) -> Optional[tuple[str, dict[str, Any]]]:
    wanted = name_key(name)
    for key, monitor in user_monitors(user_id):
        if name_key(str(monitor.get("name", ""))) == wanted:
            return key, monitor
    return None


def display_status(monitor: dict[str, Any]) -> str:
    status = monitor.get("status", "waiting")
    return {
        "online": "🟢 ONLINE",
        "offline": "🔴 OFFLINE",
        "waiting": "⚪ WAITING",
        "error": "🟡 API ERROR",
    }.get(status, "⚪ WAITING")


def make_key(user_id: int) -> str:
    import uuid
    return f"{user_id}:{uuid.uuid4().hex}"


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "on"}


# -----------------------------
# Cliproxy request
# -----------------------------
def cliproxy_search_sync(prefix: str, country: str) -> Optional[list[dict[str, Any]]]:
    data = {
        "country": country,
        "state": "",
        "city": "",
        "asn": "",
        "key": CLIPROXY_KEY,
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
            print(f"Cliproxy rate limited {prefix}/{country}")
            return None

        response.raise_for_status()
        payload = response.json()

        if payload.get("code") != 0:
            print(f"Cliproxy API error for {prefix}/{country}: {payload.get('msg')}")
            return None

        results = payload.get("data", [])
        return results if isinstance(results, list) else []

    except (requests.RequestException, ValueError) as exc:
        print(f"Cliproxy request failed for {prefix}/{country}: {exc}")
        return None


# -----------------------------
# Discord bot
# -----------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
monitor_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
monitor_task: Optional[asyncio.Task] = None


async def allowed_user(interaction: discord.Interaction) -> bool:
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message(
            "You are not authorized to use this bot.",
            ephemeral=True,
        )
        return False
    return True


async def do_search(monitor: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    async with monitor_semaphore:
        return await asyncio.to_thread(
            cliproxy_search_sync,
            monitor["prefix"],
            monitor["country"],
        )


def result_ips(results: list[dict[str, Any]]) -> list[str]:
    ips = []
    for result in results:
        ip = result.get("ip")
        if ip:
            ips.append(str(ip))
    return ips


async def dm_user(user_id: int, content: Optional[str] = None, embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None) -> bool:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if not user:
            return False
        await user.send(content=content, embed=embed, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"Could not DM user {user_id}: {exc}")
        return False


class OfflineChoiceView(discord.ui.View):
    def __init__(self, monitor_key: str, user_id: int):
        super().__init__(timeout=300)
        self.monitor_key = monitor_key
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This choice belongs to the person who created the watch.", ephemeral=True)
            return False
        return True

    async def finish(self, interaction: discord.Interaction, notify: bool) -> None:
        monitor = monitors.get(self.monitor_key)
        if not monitor:
            await interaction.response.edit_message(content="This watch no longer exists.", view=None)
            return

        monitor["notify_offline"] = notify
        monitor["offline_prompt_pending"] = False
        monitor["updated_at"] = now_iso()
        await save_monitors()

        choice = "enabled" if notify else "disabled"
        await interaction.response.edit_message(
            content=(
                f"{'✅' if notify else 'ℹ️'} Offline notifications **{choice}** for **{monitor['name']}**.\n\n"
                "The bot will continue checking this watch every 15 minutes."
            ),
            view=None,
        )

    @discord.ui.button(label="Yes, notify me", style=discord.ButtonStyle.success)
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.finish(interaction, True)

    @discord.ui.button(label="No, don't notify me", style=discord.ButtonStyle.secondary)
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.finish(interaction, False)


async def send_found_notification(key: str, monitor: dict[str, Any], results: list[dict[str, Any]]) -> None:
    ips = result_ips(results)
    shown = "\n".join(f"• `{ip}`" for ip in ips[:10]) or "• Match found"
    if len(ips) > 10:
        shown += f"\n• …and {len(ips) - 10} more"

    embed = discord.Embed(title="🟢 CLIPROXY PROXY FOUND", description="A matching result was found in Cliproxy.")
    embed.add_field(name="Name", value=monitor["name"], inline=False)
    embed.add_field(name="3 Seg", value=f"`{monitor['prefix']}.xxx`", inline=True)
    embed.add_field(name="Country", value=monitor["country"], inline=True)
    embed.add_field(name="Matches", value=str(len(results)), inline=True)
    embed.add_field(name="Cliproxy result(s)", value=shown, inline=False)

    view = OfflineChoiceView(key, int(monitor["user_id"]))
    await dm_user(
        int(monitor["user_id"]),
        content=(
            "Would you like to be notified when this proxy goes offline?\n"
            "The bot will continue checking every 15 minutes."
        ),
        embed=embed,
        view=view,
    )


async def send_offline_notification(monitor: dict[str, Any]) -> None:
    embed = discord.Embed(title="🔴 CLIPROXY PROXY OFFLINE", description="The previously detected proxy is no longer available.")
    embed.add_field(name="Name", value=monitor["name"], inline=False)
    embed.add_field(name="3 Seg", value=f"`{monitor['prefix']}.xxx`", inline=True)
    embed.add_field(name="Country", value=monitor["country"], inline=True)
    embed.set_footer(text="The bot will continue monitoring it every 15 minutes.")
    await dm_user(int(monitor["user_id"]), embed=embed)


async def send_back_online_notification(monitor: dict[str, Any], results: list[dict[str, Any]]) -> None:
    ips = result_ips(results)
    shown = "\n".join(f"• `{ip}`" for ip in ips[:10]) or "• Match found"
    if len(ips) > 10:
        shown += f"\n• …and {len(ips) - 10} more"

    embed = discord.Embed(title="🟢 CLIPROXY PROXY BACK ONLINE", description="A matching result is available again.")
    embed.add_field(name="Name", value=monitor["name"], inline=False)
    embed.add_field(name="3 Seg", value=f"`{monitor['prefix']}.xxx`", inline=True)
    embed.add_field(name="Country", value=monitor["country"], inline=True)
    embed.add_field(name="Matches", value=str(len(results)), inline=True)
    embed.add_field(name="Cliproxy result(s)", value=shown, inline=False)
    await dm_user(int(monitor["user_id"]), embed=embed)


async def check_one(key: str, monitor_snapshot: dict[str, Any]) -> None:
    results = await do_search(monitor_snapshot)

    # None means API/network/rate-limit error. Never turn that into OFFLINE.
    if results is None:
        return

    async with monitors_lock:
        monitor = monitors.get(key)
        if not monitor:
            return

        old_status = monitor.get("status", "waiting")
        monitor["last_checked"] = now_iso()
        monitor["last_error"] = None

        if results:
            monitor["status"] = "online"
            monitor["found"] = True
            monitor["last_match"] = result_ips(results)[:25]
            monitor["last_match_count"] = len(results)

            should_notify_back = old_status == "offline" and bool(monitor.get("notify_offline"))
            should_prompt = old_status in {"waiting", "error"} and not monitor.get("found_before", False)
            monitor["found_before"] = True
            monitor["updated_at"] = now_iso()
        else:
            monitor["last_match"] = []
            monitor["last_match_count"] = 0
            monitor["status"] = "offline" if monitor.get("found_before", False) else "waiting"
            should_notify_offline = (
                old_status == "online"
                and monitor.get("found_before", False)
                and bool(monitor.get("notify_offline"))
            )
            monitor["updated_at"] = now_iso()
            should_notify_back = False
            should_prompt = False

            # Persist the state before sending notifications.
            await asyncio.to_thread(save_monitors_sync)
            if should_notify_offline:
                await send_offline_notification(monitor)
            return

        await asyncio.to_thread(save_monitors_sync)

    if should_prompt:
        await send_found_notification(key, monitor, results)
    elif should_notify_back:
        await send_back_online_notification(monitor, results)


async def run_monitor_cycle() -> None:
    async with monitor_cycle_lock:
        snapshot = [
            (key, dict(monitor))
            for key, monitor in monitors.items()
            if monitor.get("active", True)
        ]

        if not snapshot:
            return

        print(f"Starting monitor cycle for {len(snapshot)} watches...")
        await asyncio.gather(*(check_one(key, monitor) for key, monitor in snapshot), return_exceptions=True)
        print("Monitor cycle complete.")


async def monitor_loop() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        started = asyncio.get_running_loop().time()
        try:
            await run_monitor_cycle()
        except Exception as exc:
            print(f"Monitor cycle error: {exc}")

        elapsed = asyncio.get_running_loop().time() - started
        sleep_for = max(5, CHECK_INTERVAL - elapsed)
        await asyncio.sleep(sleep_for)


# -----------------------------
# Pagination view
# -----------------------------
class ListView(discord.ui.View):
    def __init__(self, user_id: int, pages: list[discord.Embed]):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.pages = pages
        self.page = 0
        self.update_buttons()

    def update_buttons(self) -> None:
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This list belongs to another user.", ephemeral=True)
            return False
        return True

    async def show_page(self, interaction: discord.Interaction) -> None:
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        await self.show_page(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        await self.show_page(interaction)


# -----------------------------
# Commands
# -----------------------------
@bot.tree.command(name="watch", description="Add a named 3-segment Cliproxy monitor.")
@app_commands.describe(
    three_seg="3-segment prefix, e.g. 180.183.130 or 180.183.130.",
    country="Country code, e.g. US",
    name="Example: Triple, Double, Single. BE SPECIFIC SO YOU REMEMBER WHAT THIS SEARCH IS FOR.",
)
async def watch(interaction: discord.Interaction, three_seg: str, country: str, name: str):
    if not await allowed_user(interaction):
        return

    prefix = normalize_prefix(three_seg)
    country = country.strip().upper()
    name = normalize_name(name)

    if not prefix:
        await interaction.response.send_message(
            "⚠️ Invalid 3-segment prefix. Use something like `180.183.130` or `180.183.130.`",
            ephemeral=True,
        )
        return

    if not re.fullmatch(r"[A-Z]{2}", country):
        await interaction.response.send_message("⚠️ Country must be a 2-letter code such as `US` or `TH`.", ephemeral=True)
        return

    if not name or len(name) > 80:
        await interaction.response.send_message("⚠️ Please enter a watch name between 1 and 80 characters.", ephemeral=True)
        return

    if find_monitor_by_name(interaction.user.id, name):
        await interaction.response.send_message(
            f"⚠️ **This name is already in use.**\n\nPlease use a different name, such as `{name}2` or `{name}3`.",
            ephemeral=True,
        )
        return

    key = make_key(interaction.user.id)
    monitors[key] = {
        "user_id": interaction.user.id,
        "name": name,
        "prefix": prefix,
        "country": country,
        "active": True,
        "found": False,
        "found_before": False,
        "status": "waiting",
        "notify_offline": False,
        "offline_prompt_pending": False,
        "last_checked": None,
        "last_match": [],
        "last_match_count": 0,
        "last_error": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await save_monitors()

    await interaction.response.send_message(
        f"✅ Watch created.\n\n**Name:** {name}\n**3 Seg:** `{prefix}.xxx`\n**Country:** `{country}`\n\nThe bot will check it every {CHECK_INTERVAL // 60} minutes.",
        ephemeral=True,
    )


@bot.tree.command(name="list", description="View your Cliproxy watches with pagination.")
@app_commands.describe(filter="Optional filter: all, online, offline, waiting")
@app_commands.choices(filter=[
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Online", value="online"),
    app_commands.Choice(name="Offline", value="offline"),
    app_commands.Choice(name="Waiting", value="waiting"),
])
async def list_watches(interaction: discord.Interaction, filter: Optional[app_commands.Choice[str]] = None):
    if not await allowed_user(interaction):
        return

    selected_filter = filter.value if filter else "all"
    items = user_monitors(interaction.user.id)
    if selected_filter != "all":
        items = [(k, m) for k, m in items if m.get("status", "waiting") == selected_filter]

    items.sort(key=lambda pair: str(pair[1].get("name", "")).casefold())

    counts = {"online": 0, "offline": 0, "waiting": 0}
    for _, monitor in user_monitors(interaction.user.id):
        status = monitor.get("status", "waiting")
        if status in counts:
            counts[status] += 1

    if not items:
        await interaction.response.send_message("No watches match that filter.", ephemeral=True)
        return

    chunk_size = 15
    pages: list[discord.Embed] = []
    for start in range(0, len(items), chunk_size):
        chunk = items[start:start + chunk_size]
        page_number = len(pages) + 1
        total_pages = (len(items) + chunk_size - 1) // chunk_size
        embed = discord.Embed(title="📊 Cliproxy Monitor", description=(
            f"**Total:** {len(user_monitors(interaction.user.id))}  "
            f"🟢 {counts['online']}  🔴 {counts['offline']}  ⚪ {counts['waiting']}\n"
            f"Filter: **{selected_filter}**\nPage **{page_number}/{total_pages}**"
        ))
        lines = []
        for index, (_, monitor) in enumerate(chunk, start=start + 1):
            lines.append(
                f"**{index}. {monitor['name']}** — `{monitor['prefix']}` / `{monitor['country']}` — {display_status(monitor)}"
            )
        embed.add_field(name="Watches", value="\n".join(lines), inline=False)
        pages.append(embed)

    view = ListView(interaction.user.id, pages)
    await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)


@bot.tree.command(name="searchwatch", description="Find one of your watches by name.")
@app_commands.describe(name="Watch name to search for")
async def searchwatch(interaction: discord.Interaction, name: str):
    if not await allowed_user(interaction):
        return

    matches = [
        (k, m) for k, m in user_monitors(interaction.user.id)
        if name.casefold() in str(m.get("name", "")).casefold()
    ]
    matches.sort(key=lambda pair: str(pair[1].get("name", "")).casefold())

    if not matches:
        await interaction.response.send_message("No watch names matched that search.", ephemeral=True)
        return

    lines = []
    for _, monitor in matches[:20]:
        lines.append(f"**{monitor['name']}** — `{monitor['prefix']}` / `{monitor['country']}` — {display_status(monitor)}")
    if len(matches) > 20:
        lines.append(f"\n…and {len(matches) - 20} more. Use a more specific name.")

    await interaction.response.send_message("🔎 **Watch Search**\n\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="watchinfo", description="Show detailed information about one watch.")
@app_commands.describe(name="Exact watch name")
async def watchinfo(interaction: discord.Interaction, name: str):
    if not await allowed_user(interaction):
        return

    found = find_monitor_by_name(interaction.user.id, name)
    if not found:
        await interaction.response.send_message("No watch with that name was found.", ephemeral=True)
        return

    _, monitor = found
    embed = discord.Embed(title="📡 WATCH INFORMATION")
    embed.add_field(name="Name", value=monitor["name"], inline=False)
    embed.add_field(name="3 Seg", value=f"`{monitor['prefix']}.xxx`", inline=True)
    embed.add_field(name="Country", value=monitor["country"], inline=True)
    embed.add_field(name="Status", value=display_status(monitor), inline=True)
    embed.add_field(name="Offline notifications", value="✅ Enabled" if monitor.get("notify_offline") else "❌ Disabled", inline=True)
    embed.add_field(name="Matches", value=str(monitor.get("last_match_count", 0)), inline=True)
    embed.add_field(name="Last checked", value=monitor.get("last_checked") or "Not checked yet", inline=False)
    last_match = monitor.get("last_match") or []
    embed.add_field(name="Last result(s)", value="\n".join(f"• `{x}`" for x in last_match[:10]) or "None", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stop", description="Stop one of your watches by name.")
@app_commands.describe(name="Exact watch name")
async def stop(interaction: discord.Interaction, name: str):
    if not await allowed_user(interaction):
        return

    found = find_monitor_by_name(interaction.user.id, name)
    if not found:
        await interaction.response.send_message("No watch with that name was found.", ephemeral=True)
        return

    key, monitor = found
    monitor["active"] = False
    monitor["updated_at"] = now_iso()
    await save_monitors()
    await interaction.response.send_message(f"🛑 Stopped **{monitor['name']}**.", ephemeral=True)


@bot.tree.command(name="export", description="Export all of your Cliproxy watches to a backup file.")
async def export_watches(interaction: discord.Interaction):
    if not await allowed_user(interaction):
        return

    data = {
        "format": "cliproxy-monitor-backup",
        "version": 2,
        "exported_at": now_iso(),
        "watches": [],
    }
    for _, monitor in user_monitors(interaction.user.id):
        data["watches"].append({
            "name": monitor["name"],
            "prefix": monitor["prefix"],
            "country": monitor["country"],
            "notify_offline": bool(monitor.get("notify_offline")),
            "active": bool(monitor.get("active", True)),
        })

    raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    file = discord.File(io.BytesIO(raw), filename="cliproxy_watches_backup.json")
    await interaction.response.send_message(
        f"✅ Exported **{len(data['watches'])}** watches. Keep this file somewhere safe.",
        file=file,
        ephemeral=True,
    )


@bot.tree.command(name="import", description="Import watches from a JSON or CSV backup file.")
@app_commands.describe(file="Your Cliproxy watch backup (.json or .csv)")
async def import_watches(interaction: discord.Interaction, file: discord.Attachment):
    if not await allowed_user(interaction):
        return

    filename = file.filename.casefold()
    if not (filename.endswith(".json") or filename.endswith(".csv") or filename.endswith(".txt")):
        await interaction.response.send_message("⚠️ Please upload a `.json`, `.csv`, or `.txt` backup file.", ephemeral=True)
        return

    if file.size and file.size > 2_000_000:
        await interaction.response.send_message("⚠️ That backup is too large. Please keep it under 2 MB.", ephemeral=True)
        return

    try:
        raw = await file.read()
        text_data = raw.decode("utf-8-sig")
    except Exception as exc:
        await interaction.response.send_message(f"⚠️ Could not read the file: {exc}", ephemeral=True)
        return

    records: list[dict[str, Any]] = []
    try:
        if filename.endswith(".json"):
            payload = json.loads(text_data)
            if isinstance(payload, dict):
                records = payload.get("watches", [])
            elif isinstance(payload, list):
                records = payload
        else:
            reader = csv.DictReader(io.StringIO(text_data))
            records = [dict(row) for row in reader]
    except Exception as exc:
        await interaction.response.send_message(f"⚠️ Could not parse the backup: {exc}", ephemeral=True)
        return

    added = 0
    skipped = 0
    reasons: dict[str, int] = {}

    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            reasons["invalid record"] = reasons.get("invalid record", 0) + 1
            continue

        name = normalize_name(str(record.get("name", "")))
        prefix = normalize_prefix(str(record.get("prefix", record.get("three_seg", ""))))
        country = str(record.get("country", "")).strip().upper()

        if not name or len(name) > 80:
            skipped += 1
            reasons["invalid name"] = reasons.get("invalid name", 0) + 1
            continue
        if not prefix:
            skipped += 1
            reasons["invalid prefix"] = reasons.get("invalid prefix", 0) + 1
            continue
        if not re.fullmatch(r"[A-Z]{2}", country):
            skipped += 1
            reasons["invalid country"] = reasons.get("invalid country", 0) + 1
            continue
        if find_monitor_by_name(interaction.user.id, name):
            skipped += 1
            reasons["duplicate name"] = reasons.get("duplicate name", 0) + 1
            continue

        key = make_key(interaction.user.id)
        monitors[key] = {
            "user_id": interaction.user.id,
            "name": name,
            "prefix": prefix,
            "country": country,
            "active": parse_bool(record.get("active", True)),
            "found": False,
            "found_before": False,
            "status": "waiting",
            "notify_offline": parse_bool(record.get("notify_offline", False)),
            "offline_prompt_pending": False,
            "last_checked": None,
            "last_match": [],
            "last_match_count": 0,
            "last_error": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        added += 1

    await save_monitors()

    reason_text = "\n".join(f"• {reason}: {count}" for reason, count in reasons.items())
    response = f"✅ **Import complete**\n\nAdded: **{added}**\nSkipped: **{skipped}**"
    if reason_text:
        response += f"\n\n**Skipped because:**\n{reason_text}"
    await interaction.response.send_message(response, ephemeral=True)


# -----------------------------
# Startup
# -----------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Loaded {len(monitors)} saved watches")
    print(f"Check interval: {CHECK_INTERVAL}s ({CHECK_INTERVAL / 60:.1f} minutes)")
    print(f"Max concurrent Cliproxy requests: {MAX_CONCURRENT_REQUESTS}")


async def setup_hook():
    global monitor_task
    await bot.tree.sync()
    monitor_task = asyncio.create_task(monitor_loop())


bot.setup_hook = setup_hook


if __name__ == "__main__":
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not ALLOWED_USER_IDS:
        missing.append("ALLOWED_USER_IDS")
    if not CLIPROXY_KEY:
        missing.append("CLIPROXY_KEY")
    if not CLIPROXY_TOKEN:
        missing.append("CLIPROXY_TOKEN")

    if missing:
        raise SystemExit("Missing required .env values: " + ", ".join(missing))

    bot.run(DISCORD_TOKEN)
