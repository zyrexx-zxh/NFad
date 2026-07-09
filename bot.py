#!/usr/bin/env python3
"""
North Files — Instagram Ban/Recovery Monitor Bot
--------------------------------------------------
discord.py bot that watches PUBLIC Instagram accounts and posts for
status changes (removed/banned <-> recovered/restored) and posts a
branded embed the moment it detects a change.

100% free stack: discord.py + aiohttp + Pillow. No paid APIs.

Commands:
    .remove <username>      watch an ACTIVE account, alert when it 404s
    .recover <username>     watch a BANNED account, alert when it's back (200)
    .postremove <link>      watch a LIVE post, alert when it's removed
    .postrecover <link>     watch a REMOVED post, alert when it's restored
    .watching               list everything currently being tracked

Author: North Files | @Claxen
"""

import os
import re
import io
import asyncio
import threading
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands, tasks
from flask import Flask
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PREFIX = "."
CHECK_INTERVAL_SECONDS = 90      # how often the full sweep runs
PER_TARGET_DELAY_SECONDS = 3     # delay between individual IG requests in a sweep
REQUEST_TIMEOUT = 15

BRAND_NAME = "North Files | @Claxen"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

POST_URL_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")
DESC_RE = re.compile(
    r'([\d,\.]+[KkMm]?)\s+Followers,\s*([\d,\.]+[KkMm]?)\s+Following,\s*([\d,\.]+[KkMm]?)\s+Posts',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# In-memory tracking state
# ---------------------------------------------------------------------------
# monitored_accounts[username_lower] = {
#     "username": str, "mode": "remove" | "recover", "start_time": datetime,
#     "channel_id": int, "author_id": int, "last_stats": dict
# }
monitored_accounts = {}
# monitored_posts[shortcode] = { same shape + "url": str }
monitored_posts = {}

_state_lock = asyncio.Lock()  # guards the dicts above across command handlers + loop


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def parse_count(raw: str):
    raw = raw.strip().replace(",", "")
    multiplier = 1
    if raw and raw[-1] in ("K", "k"):
        multiplier = 1_000
        raw = raw[:-1]
    elif raw and raw[-1] in ("M", "m"):
        multiplier = 1_000_000
        raw = raw[:-1]
    try:
        return int(float(raw) * multiplier)
    except ValueError:
        return None


def extract_shortcode(link: str):
    match = POST_URL_RE.search(link)
    return match.group(1) if match else None


def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(start: datetime, end: datetime) -> str:
    total = int((end - start).total_seconds())
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours} hour, {minutes} minutes, {seconds} seconds"


# ---------------------------------------------------------------------------
# Instagram fetchers (async, non-blocking)
# ---------------------------------------------------------------------------
async def fetch(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True) as resp:
            text = await resp.text()
            return resp.status, text
    except Exception as e:
        print(f"[fetch] error for {url}: {e}")
        return None, None


async def get_account_stats(session: aiohttp.ClientSession, username: str) -> dict:
    url = f"https://www.instagram.com/{username}/"
    status, html = await fetch(session, url)

    if status is None:
        return {"status": "error"}
    if status == 404:
        return {"status": 404}
    if status != 200:
        return {"status": status}

    followers = following = posts = None
    pic_url = None

    desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', html or "")
    if desc_match:
        m = DESC_RE.search(desc_match.group(1))
        if m:
            followers = parse_count(m.group(1))
            following = parse_count(m.group(2))
            posts = parse_count(m.group(3))

    img_match = re.search(r'<meta property="og:image" content="([^"]+)"', html or "")
    if img_match:
        pic_url = img_match.group(1).replace("&amp;", "&")

    return {
        "status": 200,
        "followers": followers,
        "following": following,
        "posts": posts,
        "pic": pic_url,
    }


async def get_post_stats(session: aiohttp.ClientSession, shortcode: str) -> dict:
    url = f"https://www.instagram.com/p/{shortcode}/"
    status, html = await fetch(session, url)

    if status is None:
        return {"status": "error"}
    if status == 404:
        return {"status": 404}
    if status != 200:
        return {"status": status}

    # Instagram sometimes returns 200 for a dead link but the page content
    # signals it's gone. Treat that as removed too.
    lowered = (html or "").lower()
    if "page not found" in lowered or "sorry, this page" in lowered:
        return {"status": 404}

    thumb_url = None
    img_match = re.search(r'<meta property="og:image" content="([^"]+)"', html or "")
    if img_match:
        thumb_url = img_match.group(1).replace("&amp;", "&")

    return {"status": 200, "thumb": thumb_url}


# ---------------------------------------------------------------------------
# Stats card image generation (Pillow)
# ---------------------------------------------------------------------------
def generate_stats_card(username: str, posts, followers, following) -> io.BytesIO:
    width, height = 600, 220
    bg = (18, 18, 22)
    accent = (0, 230, 150)
    muted = (170, 170, 180)

    img = Image.new("RGB", (width, height), color=bg)
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
        font_value = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        font_label = ImageFont.truetype("DejaVuSans.ttf", 16)
    except Exception:
        font_title = ImageFont.load_default()
        font_value = ImageFont.load_default()
        font_label = ImageFont.load_default()

    draw.text((24, 20), f"@{username}", font=font_title, fill=(255, 255, 255))
    draw.line([(24, 62), (width - 24, 62)], fill=(55, 55, 65), width=2)

    def fmt(v):
        return "N/A" if v is None else f"{v:,}"

    stats = [("Posts", fmt(posts)), ("Followers", fmt(followers)), ("Following", fmt(following))]
    col_width = (width - 48) // 3
    x = 24
    for label, value in stats:
        draw.text((x, 100), value, font=font_value, fill=accent)
        draw.text((x, 135), label, font=font_label, fill=muted)
        x += col_width

    draw.text((24, height - 30), BRAND_NAME, font=font_label, fill=(90, 90, 100))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------
def build_account_embed(event: str, username: str, stats: dict, start_time: datetime):
    now = datetime.now(timezone.utc)
    duration = format_duration(start_time, now)
    followers = stats.get("followers")
    followers_str = "N/A" if followers is None else f"{followers:,}"

    if event == "recovered":
        color = discord.Color.green()
        title = f"Account Recovered | @{username} 🏆✅"
        action_word = "Unbanned"
    else:
        color = discord.Color.red()
        title = f"Account Removed | @{username} 🪦❌"
        action_word = "Banned"

    embed = discord.Embed(
        title=title,
        description=f"Followers: {followers_str} | ⏱️ Time Taken: {duration}",
        color=color,
    )
    embed.set_footer(text=f"{action_word} at {utc_now_str()} UTC • {BRAND_NAME}")

    file = None
    if stats.get("posts") is not None or stats.get("followers") is not None:
        buf = generate_stats_card(
            username, stats.get("posts"), stats.get("followers"), stats.get("following")
        )
        file = discord.File(buf, filename="stats.png")
        embed.set_image(url="attachment://stats.png")

    if stats.get("pic"):
        embed.set_thumbnail(url=stats["pic"])

    return embed, file


def build_post_embed(event: str, shortcode: str, url: str, stats: dict, start_time: datetime):
    now = datetime.now(timezone.utc)
    duration = format_duration(start_time, now)

    if event == "recovered":
        color = discord.Color.green()
        title = f"Post Recovered | {shortcode} ✅"
        action_word = "Restored"
    else:
        color = discord.Color.red()
        title = f"Post Removed | {shortcode} 🪦❌"
        action_word = "Removed"

    embed = discord.Embed(
        title=title,
        description=f"Link: {url} | ⏱️ Time Taken: {duration}",
        color=color,
    )
    embed.set_footer(text=f"{action_word} at {utc_now_str()} UTC • {BRAND_NAME}")

    if stats.get("thumb"):
        embed.set_image(url=stats["thumb"])

    return embed, None


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # required to read prefix commands — enable in Dev Portal too

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    if not monitor_loop.is_running():
        monitor_loop.start()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing argument. Usage: `{PREFIX}{ctx.command} <value>`")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        print(f"[command error] {error}")
        await ctx.send(f"⚠️ Something went wrong: `{error}`")


# --- .remove ----------------------------------------------------------------
@bot.command(name="remove")
async def cmd_remove(ctx, username: str):
    username = username.lstrip("@").strip()
    key = username.lower()

    async with _state_lock:
        if key in monitored_accounts:
            await ctx.send(f"⚠️ Already monitoring **@{username}**.")
            return

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        stats = await get_account_stats(session, username)

    if stats["status"] == 404:
        await ctx.send(f"❌ **@{username}** already returns 404 — nothing to monitor.")
        return
    if stats["status"] != 200:
        await ctx.send(f"⚠️ Couldn't reach **@{username}** right now (status: {stats['status']}). Try again shortly.")
        return

    async with _state_lock:
        monitored_accounts[key] = {
            "username": username,
            "mode": "remove",
            "start_time": datetime.now(timezone.utc),
            "channel_id": ctx.channel.id,
            "author_id": ctx.author.id,
            "last_stats": stats,
        }

    await ctx.send(f"🔍 Watching **@{username}** — I'll alert this channel the moment it goes down.")


# --- .recover ----------------------------------------------------------------
@bot.command(name="recover")
async def cmd_recover(ctx, username: str):
    username = username.lstrip("@").strip()
    key = username.lower()

    async with _state_lock:
        if key in monitored_accounts:
            await ctx.send(f"⚠️ Already monitoring **@{username}**.")
            return

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        stats = await get_account_stats(session, username)

    if stats["status"] == 200:
        await ctx.send(f"✅ **@{username}** is already active (200 OK) — nothing to monitor.")
        return

    async with _state_lock:
        monitored_accounts[key] = {
            "username": username,
            "mode": "recover",
            "start_time": datetime.now(timezone.utc),
            "channel_id": ctx.channel.id,
            "author_id": ctx.author.id,
            "last_stats": {"status": stats["status"]},
        }

    await ctx.send(f"🔍 Watching **@{username}** for recovery — I'll alert this channel the moment it's back.")


# --- .postremove ---------------------------------------------------------------
@bot.command(name="postremove")
async def cmd_postremove(ctx, link: str):
    shortcode = extract_shortcode(link)
    if not shortcode:
        await ctx.send("⚠️ That doesn't look like a valid Instagram post/reel link.")
        return

    async with _state_lock:
        if shortcode in monitored_posts:
            await ctx.send("⚠️ Already monitoring that post.")
            return

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        stats = await get_post_stats(session, shortcode)

    if stats["status"] == 404:
        await ctx.send("❌ That post already appears to be removed — nothing to monitor.")
        return
    if stats["status"] != 200:
        await ctx.send(f"⚠️ Couldn't reach that post right now (status: {stats['status']}). Try again shortly.")
        return

    async with _state_lock:
        monitored_posts[shortcode] = {
            "url": link,
            "mode": "remove",
            "start_time": datetime.now(timezone.utc),
            "channel_id": ctx.channel.id,
            "author_id": ctx.author.id,
            "last_stats": stats,
        }

    await ctx.send(f"🔍 Watching post `{shortcode}` for removal.")


# --- .postrecover ---------------------------------------------------------------
@bot.command(name="postrecover")
async def cmd_postrecover(ctx, link: str):
    shortcode = extract_shortcode(link)
    if not shortcode:
        await ctx.send("⚠️ That doesn't look like a valid Instagram post/reel link.")
        return

    async with _state_lock:
        if shortcode in monitored_posts:
            await ctx.send("⚠️ Already monitoring that post.")
            return

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        stats = await get_post_stats(session, shortcode)

    if stats["status"] == 200:
        await ctx.send("✅ That post is already live — nothing to monitor.")
        return

    async with _state_lock:
        monitored_posts[shortcode] = {
            "url": link,
            "mode": "recover",
            "start_time": datetime.now(timezone.utc),
            "channel_id": ctx.channel.id,
            "author_id": ctx.author.id,
            "last_stats": {"status": stats["status"]},
        }

    await ctx.send(f"🔍 Watching post `{shortcode}` for recovery.")


# --- .watching ---------------------------------------------------------------
@bot.command(name="watching")
async def cmd_watching(ctx):
    async with _state_lock:
        acc_lines = [
            f"• @{v['username']} ({v['mode']}) — started {format_duration(v['start_time'], datetime.now(timezone.utc))} ago"
            for v in monitored_accounts.values()
        ]
        post_lines = [
            f"• `{sc}` ({v['mode']}) — started {format_duration(v['start_time'], datetime.now(timezone.utc))} ago"
            for sc, v in monitored_posts.items()
        ]

    if not acc_lines and not post_lines:
        await ctx.send("Nothing is currently being monitored.")
        return

    embed = discord.Embed(title="👁️ Currently Watching", color=discord.Color.blurple())
    if acc_lines:
        embed.add_field(name="Accounts", value="\n".join(acc_lines), inline=False)
    if post_lines:
        embed.add_field(name="Posts", value="\n".join(post_lines), inline=False)
    embed.set_footer(text=BRAND_NAME)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Background monitor loop
# ---------------------------------------------------------------------------
@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def monitor_loop():
    async with _state_lock:
        account_keys = list(monitored_accounts.keys())
        post_keys = list(monitored_posts.keys())

    if not account_keys and not post_keys:
        return

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # --- accounts ---
        for key in account_keys:
            async with _state_lock:
                entry = monitored_accounts.get(key)
            if not entry:
                continue

            try:
                stats = await get_account_stats(session, entry["username"])
            except Exception as e:
                print(f"[monitor_loop] account check failed for {entry['username']}: {e}")
                await asyncio.sleep(PER_TARGET_DELAY_SECONDS)
                continue

            fired_event = None
            if stats["status"] == 404 and entry["mode"] == "remove":
                fired_event = "removed"
            elif stats["status"] == 200 and entry["mode"] == "recover":
                fired_event = "recovered"

            if fired_event:
                use_stats = stats if fired_event == "recovered" else entry["last_stats"]
                embed, file = build_account_embed(fired_event, entry["username"], use_stats, entry["start_time"])
                channel = bot.get_channel(entry["channel_id"])
                if channel:
                    try:
                        if file:
                            await channel.send(embed=embed, file=file)
                        else:
                            await channel.send(embed=embed)
                    except Exception as e:
                        print(f"[monitor_loop] failed to send alert: {e}")
                async with _state_lock:
                    monitored_accounts.pop(key, None)
            elif stats["status"] == 200:
                async with _state_lock:
                    if key in monitored_accounts:
                        monitored_accounts[key]["last_stats"] = stats

            await asyncio.sleep(PER_TARGET_DELAY_SECONDS)

        # --- posts ---
        for shortcode in post_keys:
            async with _state_lock:
                entry = monitored_posts.get(shortcode)
            if not entry:
                continue

            try:
                stats = await get_post_stats(session, shortcode)
            except Exception as e:
                print(f"[monitor_loop] post check failed for {shortcode}: {e}")
                await asyncio.sleep(PER_TARGET_DELAY_SECONDS)
                continue

            fired_event = None
            if stats["status"] == 404 and entry["mode"] == "remove":
                fired_event = "removed"
            elif stats["status"] == 200 and entry["mode"] == "recover":
                fired_event = "recovered"

            if fired_event:
                use_stats = stats if fired_event == "recovered" else entry["last_stats"]
                embed, file = build_post_embed(fired_event, shortcode, entry["url"], use_stats, entry["start_time"])
                channel = bot.get_channel(entry["channel_id"])
                if channel:
                    try:
                        await channel.send(embed=embed)
                    except Exception as e:
                        print(f"[monitor_loop] failed to send alert: {e}")
                async with _state_lock:
                    monitored_posts.pop(shortcode, None)

            await asyncio.sleep(PER_TARGET_DELAY_SECONDS)


@monitor_loop.before_loop
async def before_monitor_loop():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Flask keep-alive server (for Render + UptimeRobot)
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "North Files bot is alive."


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN environment variable is not set.")

    keep_alive()
    bot.run(token)
