import os
import json
import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import requests

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CLIPROXY_KEY = os.getenv("CLIPROXY_KEY", "")
CLIPROXY_TOKEN = os.getenv("CLIPROXY_TOKEN", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

CLIPROXY_URL = "https://api.cliproxy.com/v1/ipList"
MONITORS_FILE = Path("monitors.json")


def load_monitors():
    if not MONITORS_FILE.exists():
        return {}
    try:
        with MONITORS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_monitors(monitors):
    with MONITORS_FILE.open("w", encoding="utf-8") as f:
        json.dump(monitors, f, indent=2)


monitors = load_monitors()


def cliproxy_search_sync(three_seg, country):
    data = {
        "country": country,
        "state": "",
        "city": "",
        "asn": "",
        "key": CLIPROXY_KEY,
        "ipc": three_seg,
        "lang": "en",
        "token": CLIPROXY_TOKEN,
    }

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.5",
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "origin": "https://dash.cliproxy.com",
        "user-agent": "Mozilla/5.0",
    }

    try:
        response = requests.post(
            CLIPROXY_URL,
            data=data,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("code") != 0:
            print(f"Cliproxy returned code {payload.get('code')}: {payload.get('msg')}")
            return []

        results = payload.get("data", [])
        return results if isinstance(results, list) else []

    except Exception as e:
        print(f"Cliproxy request error: {e}")
        return []


async def allowed_user(interaction):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message(
            "You are not authorized to use this bot.",
            ephemeral=True,
        )
        return False
    return True


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.tree.command(name="watch", description="Watch a Cliproxy 3-segment prefix")
@app_commands.describe(
    three_seg="First 3 IP segments, e.g. 24.7.110",
    country="Country code, e.g. US",
    name="Example: Triple, Double, Single. Be specific so you remember what this search is for.",
)
async def watch(interaction: discord.Interaction, three_seg: str, country: str, name: str):
    if not await allowed_user(interaction):
        return

    three_seg = three_seg.strip()

    # Accept both 24.7.14 and 24.7.14.
    # The trailing dot is removed before sending the API request.
    if three_seg.endswith("."):
        three_seg = three_seg[:-1]

    country = country.strip().upper()
    name = name.strip()

    parts = three_seg.split(".")
    if len(parts) != 3 or any(
        not part.isdigit() or not 0 <= int(part) <= 255
        for part in parts
    ):
        await interaction.response.send_message(
            "Invalid 3-segment prefix.\n"
            "Example: `24.7.110` or `24.7.110.`",
            ephemeral=True,
        )
        return

    if not name:
        await interaction.response.send_message(
            "Please provide a name for this search.",
            ephemeral=True,
        )
        return

    if len(country) != 2:
        await interaction.response.send_message(
            "Please provide a 2-letter country code, such as `US`.",
            ephemeral=True,
        )
        return

    key = f"{interaction.user.id}:{three_seg}:{country}:{name.lower()}"

    monitors[key] = {
        "three_seg": three_seg,
        "country": country,
        "name": name,
        "user_id": interaction.user.id,
        "found": False,
    }
    save_monitors(monitors)

    await interaction.response.send_message(
        f"🟢 Now watching **{name}**\n"
        f"**3 Seg:** `{three_seg}.xxx`\n"
        f"**Country:** `{country}`\n"
        f"Checks every **{CHECK_INTERVAL} seconds**.",
        ephemeral=True,
    )


@bot.tree.command(name="list", description="List your Cliproxy searches")
async def list_monitors(interaction: discord.Interaction):
    if not await allowed_user(interaction):
        return

    user_monitors = [
        m for m in monitors.values()
        if m.get("user_id") == interaction.user.id
    ]

    if not user_monitors:
        await interaction.response.send_message(
            "You are not watching any proxy searches.",
            ephemeral=True,
        )
        return

    lines = ["**Your Cliproxy searches:**", ""]

    for m in user_monitors:
        status = "🟢 Found" if m.get("found") else "🔎 Watching"
        lines.append(
            f"{status}\n"
            f"**Name:** {m.get('name', 'Unnamed')}\n"
            f"**3 Seg:** `{m.get('three_seg', '')}.xxx`\n"
            f"**Country:** `{m.get('country', '')}`\n"
        )

    await interaction.response.send_message(
        "\n".join(lines),
        ephemeral=True,
    )


@bot.tree.command(name="stop", description="Stop one of your Cliproxy searches")
@app_commands.describe(name="Name of the search to stop")
async def stop(interaction: discord.Interaction, name: str):
    if not await allowed_user(interaction):
        return

    wanted_name = name.strip().lower()

    matching_keys = [
        key for key, m in monitors.items()
        if m.get("user_id") == interaction.user.id
        and m.get("name", "").strip().lower() == wanted_name
    ]

    if not matching_keys:
        await interaction.response.send_message(
            f"No search named **{name}** was found.",
            ephemeral=True,
        )
        return

    for key in matching_keys:
        del monitors[key]

    save_monitors(monitors)

    await interaction.response.send_message(
        f"Stopped **{name}**.",
        ephemeral=True,
    )


async def monitor_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        for key, monitor in list(monitors.items()):
            try:
                results = await asyncio.to_thread(
                    cliproxy_search_sync,
                    monitor["three_seg"],
                    monitor["country"],
                )

                if results and not monitor.get("found", False):
                    monitor["found"] = True
                    save_monitors(monitors)

                    try:
                        user = bot.get_user(int(monitor["user_id"]))
                        if user is None:
                            user = await bot.fetch_user(int(monitor["user_id"]))
                    except Exception as e:
                        print(f"Could not find user {monitor['user_id']}: {e}")
                        continue

                    result_ips = [
                        str(result.get("ip", "Unknown"))
                        for result in results
                    ]

                    message = (
                        "🟢 **CLIPROXY PROXY FOUND**\n"
                        f"**Name:** {monitor['name']}\n"
                        f"**3 Seg:** `{monitor['three_seg']}.xxx`\n"
                        f"**Country:** `{monitor['country']}`\n"
                        f"**Matches:** `{len(results)}`\n"
                        f"**Cliproxy result(s):** "
                        + ", ".join(f"`{ip}`" for ip in result_ips)
                        + "\n\n"
                        "You can now search that same 3-segment prefix in Cliproxy"
                    )

                    try:
                        await user.send(message)
                        print(
                            f"FOUND: {monitor['name']} - "
                            f"{monitor['three_seg']} / {monitor['country']} - "
                            f"{len(results)} result(s)"
                        )
                    except Exception as e:
                        print(f"Could not DM user {monitor['user_id']}: {e}")

            except Exception as e:
                print(f"Monitor error for {key}: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Checking Cliproxy every {CHECK_INTERVAL} seconds.")


async def setup_hook():
    await bot.tree.sync()
    print("Slash commands synced.")
    asyncio.create_task(monitor_loop())


bot.setup_hook = setup_hook


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from .env")

if not ALLOWED_USER_IDS:
    raise RuntimeError("ALLOWED_USER_IDS is missing from .env")

if not CLIPROXY_KEY:
    raise RuntimeError("CLIPROXY_KEY is missing from .env")

if not CLIPROXY_TOKEN:
    raise RuntimeError("CLIPROXY_TOKEN is missing from .env")


bot.run(DISCORD_TOKEN)
