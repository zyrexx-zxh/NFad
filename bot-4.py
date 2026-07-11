#!/usr/bin/env python3
"""
North Files — Instagram Ban/Recovery Monitor Bot
--------------------------------------------------
discord.py bot that watches PUBLIC Instagram accounts and posts for
status changes (removed/banned <-> recovered/restored) and posts a
branded embed the moment it detects a change.

Uses HikerAPI (https://hikerapi.com) for reliable Instagram data — pay-per-
request, cheap at low volume, works from any server IP including Render.

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

HIKERAPI_KEY = os.environ.get("HIKERAPI_KEY")
HIKERAPI_BASE = "https://api.hikerapi.com"
HEADERS = {"x-access-key": HIKERAPI_KEY or ""}

POST_URL_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")

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
def extract_shortcode(link: str):
    match = POST_URL_RE.search(link)
    return match.group(1) if match else None


IG_URL_RE = re.compile(r'(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?', re.IGNORECASE)
INVISIBLE_CHARS_RE = re.compile(r'[\u200b-\u200f\u202a-\u202e\ufeff\xa0]')
VALID_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.]{1,30}$')


def clean_username(raw: str) -> str:
    """
    Normalizes a username argument. Handles three real-world cases that were
    silently breaking usernames containing '.' or '_':
      1. The person pasted a full profile link instead of a bare username.
      2. Mobile keyboards / copy-paste sometimes insert invisible unicode
         characters (zero-width spaces, non-breaking spaces) around
         punctuation, which look identical but fail exact string matches.
      3. A leading '@' was included.
    """
    raw = raw.strip()
    raw = INVISIBLE_CHARS_RE.sub("", raw)

    url_match = IG_URL_RE.search(raw)
    if url_match:
        raw = url_match.group(1)

    raw = raw.lstrip("@").strip()
    return raw


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
async def get_account_stats(session: aiohttp.ClientSession, username: str) -> dict:
    url = f"{HIKERAPI_BASE}/v1/user/by/username"
    try:
        async with session.get(url, params={"username": username}, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 404:
                return {"status": 404}
            if resp.status != 200:
                body = await resp.text()
                print(f"[hikerapi] account status {resp.status} for {username}: {body[:200]}")
                return {"status": resp.status}
            data = await resp.json()
    except Exception as e:
        print(f"[hikerapi] account fetch error for {username}: {e}")
        return {"status": "error"}

    return {
        "status": 200,
        "followers": data.get("follower_count"),
        "following": data.get("following_count"),
        "posts": data.get("media_count"),
        "pic": data.get("profile_pic_url"),
        "full_name": data.get("full_name") or username,
        "biography": data.get("biography") or "",
        "is_verified": bool(data.get("is_verified")),
    }


async def get_post_stats(session: aiohttp.ClientSession, post_url: str) -> dict:
    url = f"{HIKERAPI_BASE}/v2/media/info/by/url"
    try:
        async with session.get(url, params={"url": post_url}, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 404:
                return {"status": 404}
            if resp.status != 200:
                body = await resp.text()
                print(f"[hikerapi] post status {resp.status} for {post_url}: {body[:200]}")
                return {"status": resp.status}
            data = await resp.json()
    except Exception as e:
        print(f"[hikerapi] post fetch error for {post_url}: {e}")
        return {"status": "error"}

    thumb_url = None
    try:
        candidates = (data.get("image_versions2") or {}).get("candidates") or []
        if candidates:
            thumb_url = candidates[0].get("url")
        if not thumb_url:
            thumb_url = data.get("thumbnail_url")
    except Exception:
        thumb_url = None

    return {"status": 200, "thumb": thumb_url}


# ---------------------------------------------------------------------------
# Stats card image generation (Pillow)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Profile screenshot generation (Pillow) — mimics the iPhone IG profile tab
# ---------------------------------------------------------------------------
async def fetch_image_bytes(url: str):
    """Downloads an image from a plain URL (e.g. Instagram's CDN) without
    attaching the HikerAPI auth header — that header should only ever be
    sent to api.hikerapi.com."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        print(f"[fetch_image_bytes] error: {e}")
    return None


def _load_fonts():
    try:
        return {
            "bold_lg": ImageFont.truetype("DejaVuSans-Bold.ttf", 30),
            "bold_md": ImageFont.truetype("DejaVuSans-Bold.ttf", 24),
            "bold_sm": ImageFont.truetype("DejaVuSans-Bold.ttf", 20),
            "reg_md": ImageFont.truetype("DejaVuSans.ttf", 20),
            "reg_sm": ImageFont.truetype("DejaVuSans.ttf", 17),
            "reg_xs": ImageFont.truetype("DejaVuSans.ttf", 15),
        }
    except Exception:
        d = ImageFont.load_default()
        return {k: d for k in ("bold_lg", "bold_md", "bold_sm", "reg_md", "reg_sm", "reg_xs")}


def _circle_mask_paste(base: Image.Image, pic_bytes, box, fallback_letter: str):
    x0, y0, x1, y1 = box
    size = x1 - x0
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)

    if pic_bytes:
        try:
            pic = Image.open(io.BytesIO(pic_bytes)).convert("RGB").resize((size, size))
            base.paste(pic, (x0, y0), mask)
            return
        except Exception as e:
            print(f"[profile_screenshot] pic paste failed: {e}")

    # Fallback: a solid circle with the account's first letter
    circle = Image.new("RGB", (size, size), (48, 48, 56))
    d = ImageDraw.Draw(circle)
    font = ImageFont.load_default()
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size // 2)
    except Exception:
        pass
    bbox = d.textbbox((0, 0), fallback_letter, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2, (size - th) / 2 - bbox[1]), fallback_letter, font=font, fill=(200, 200, 210))
    base.paste(circle, (x0, y0), mask)


def draw_grid_icon(draw, cx, cy, size, color):
    s = size // 3
    for row in range(3):
        for col in range(3):
            x = cx - size // 2 + col * s
            y = cy - size // 2 + row * s
            draw.rectangle([x, y, x + s - 3, y + s - 3], outline=color, width=2)


def draw_reels_icon(draw, cx, cy, size, color):
    half = size // 2
    draw.rounded_rectangle([cx - half, cy - half, cx + half, cy + half], radius=6, outline=color, width=2)
    draw.polygon(
        [(cx - 5, cy - 8), (cx - 5, cy + 8), (cx + 8, cy)],
        fill=color,
    )


def draw_tagged_icon(draw, cx, cy, size, color):
    half = size // 2
    draw.rounded_rectangle([cx - half, cy - half, cx + half, cy + half], radius=6, outline=color, width=2)
    draw.ellipse([cx - 6, cy - 8, cx + 6, cy + 4], outline=color, width=2)
    draw.arc([cx - 10, cy + 2, cx + 10, cy + 16], start=200, end=340, fill=color, width=2)


async def generate_profile_screenshot(username: str, stats: dict) -> io.BytesIO:
    W, H = 720, 1080
    bg = (0, 0, 0)
    fg = (245, 245, 245)
    muted = (142, 142, 147)
    accent = (0, 149, 246)  # IG blue
    border = (38, 38, 40)

    fonts = _load_fonts()
    pic_bytes = await fetch_image_bytes(stats["pic"]) if stats.get("pic") else None

    img = Image.new("RGB", (W, H), color=bg)
    draw = ImageDraw.Draw(img)

    # --- iPhone status bar -------------------------------------------------
    draw.text((28, 16), "9:41", font=fonts["bold_sm"], fill=fg)
    # signal dots
    for i in range(4):
        h = 6 + i * 3
        draw.rectangle([W - 110 + i * 8, 28 - h, W - 110 + i * 8 + 5, 28], fill=fg)
    # wifi arc
    draw.arc([W - 74, 10, W - 54, 30], start=225, end=315, fill=fg, width=2)
    # battery
    draw.rounded_rectangle([W - 46, 14, W - 20, 28], radius=3, outline=fg, width=2)
    draw.rectangle([W - 19, 18, W - 16, 24], fill=fg)
    draw.rectangle([W - 43, 17, W - 30, 25], fill=fg)

    # --- header --------------------------------------------------------------
    header_y = 60
    draw.line([(24, 20), (14, header_y - 5), (24, header_y - 30)], fill=fg, width=3, joint="curve")
    name_font = fonts["bold_md"]
    name_text = username
    bbox = draw.textbbox((0, 0), name_text, font=name_font)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) / 2, header_y - 20), name_text, font=name_font, fill=fg)
    if stats.get("is_verified"):
        vb_x = (W + tw) / 2 + 8
        draw.ellipse([vb_x, header_y - 18, vb_x + 20, header_y + 2], fill=accent)
        draw.line([vb_x + 5, header_y - 8, vb_x + 9, header_y - 3, vb_x + 15, header_y - 13], fill=(255, 255, 255), width=2, joint="curve")
    for i in range(3):
        draw.ellipse([W - 40, header_y - 15 + i * 6, W - 36, header_y - 11 + i * 6], fill=fg)

    draw.line([(0, header_y + 20), (W, header_y + 20)], fill=border, width=1)

    # --- profile picture + stats row -----------------------------------------
    pic_size = 170
    pic_x, pic_y = 32, header_y + 45
    _circle_mask_paste(img, pic_bytes, (pic_x, pic_y, pic_x + pic_size, pic_y + pic_size), username[:1].upper())

    def fmt(v):
        return "0" if v is None else f"{v:,}"

    stat_labels = [("Posts", fmt(stats.get("posts"))), ("Followers", fmt(stats.get("followers"))), ("Following", fmt(stats.get("following")))]
    stat_col_w = (W - (pic_x + pic_size + 24) - 24) // 3
    sx = pic_x + pic_size + 24
    sy = pic_y + 35
    for label, value in stat_labels:
        vb = draw.textbbox((0, 0), value, font=fonts["bold_md"])
        vw = vb[2] - vb[0]
        draw.text((sx + (stat_col_w - vw) / 2, sy), value, font=fonts["bold_md"], fill=fg)
        lb = draw.textbbox((0, 0), label, font=fonts["reg_sm"])
        lw = lb[2] - lb[0]
        draw.text((sx + (stat_col_w - lw) / 2, sy + 36), label, font=fonts["reg_sm"], fill=muted)
        sx += stat_col_w

    # --- full name + bio ------------------------------------------------------
    text_y = pic_y + pic_size + 16
    full_name = stats.get("full_name") or username
    draw.text((32, text_y), full_name, font=fonts["bold_sm"], fill=fg)
    text_y += 30

    bio = (stats.get("biography") or "").strip()
    if bio:
        words = bio.split()
        line = ""
        max_width = W - 64
        for word in words:
            test = f"{line} {word}".strip()
            tb = draw.textbbox((0, 0), test, font=fonts["reg_sm"])
            if tb[2] - tb[0] > max_width and line:
                draw.text((32, text_y), line, font=fonts["reg_sm"], fill=fg)
                text_y += 24
                line = word
            else:
                line = test
        if line:
            draw.text((32, text_y), line, font=fonts["reg_sm"], fill=fg)
            text_y += 24

    # --- buttons ---------------------------------------------------------------
    btn_y = text_y + 16
    btn_h = 56
    draw.rounded_rectangle([32, btn_y, W // 2 - 8, btn_y + btn_h], radius=8, fill=(38, 38, 40))
    draw.rounded_rectangle([W // 2 + 8, btn_y, W - 32, btn_y + btn_h], radius=8, fill=(38, 38, 40))

    def center_text(txt, x0, x1, y0, y1, font, color=fg):
        b = draw.textbbox((0, 0), txt, font=font)
        w, h = b[2] - b[0], b[3] - b[1]
        draw.text((x0 + ((x1 - x0) - w) / 2, y0 + ((y1 - y0) - h) / 2 - b[1]), txt, font=font, fill=color)

    center_text("Message", 32, W // 2 - 8, btn_y, btn_y + btn_h, fonts["bold_sm"])
    center_text("Following", W // 2 + 8, W - 32, btn_y, btn_y + btn_h, fonts["bold_sm"])

    # --- tab icon row ------------------------------------------------------------
    tabs_y = btn_y + btn_h + 30
    draw.line([(0, tabs_y - 14), (W, tabs_y - 14)], fill=border, width=1)
    icon_size = 30
    third = W // 3
    draw_grid_icon(draw, third // 2, tabs_y + icon_size // 2, icon_size, fg)
    draw_reels_icon(draw, third + third // 2, tabs_y + icon_size // 2, icon_size, muted)
    draw_tagged_icon(draw, 2 * third + third // 2, tabs_y + icon_size // 2, icon_size, muted)
    draw.rectangle([0, tabs_y + icon_size + 12, third, tabs_y + icon_size + 15], fill=fg)

    # --- post grid placeholders (real thumbnails aren't pulled to save API credits) ---
    grid_top = tabs_y + icon_size + 24
    gap = 2
    tile = (W - gap * 2) // 3
    tones = [(24, 24, 27), (20, 20, 23), (28, 28, 31)]
    i = 0
    y = grid_top
    while y + tile <= H - 10:
        for col in range(3):
            x = col * (tile + gap)
            color = tones[i % len(tones)]
            draw.rectangle([x, y, x + tile, y + tile], fill=color)
            # small camera-corner glyph so tiles read as "posts" not blank boxes
            draw.rectangle([x + tile - 26, y + 8, x + tile - 10, y + 20], outline=muted, width=1)
            i += 1
        y += tile + gap

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------
async def build_account_embed(event: str, username: str, stats: dict, start_time: datetime):
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
    try:
        buf = await generate_profile_screenshot(username, stats)
        file = discord.File(buf, filename="profile.png")
        embed.set_image(url="attachment://profile.png")
    except Exception as e:
        print(f"[build_account_embed] screenshot generation failed: {e}")
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
    elif isinstance(error, commands.NotOwner):
        await ctx.send("⚠️ This command is restricted to the bot owner.")
    else:
        print(f"[command error] {error}")
        await ctx.send(f"⚠️ Something went wrong: `{error}`")


# --- .remove ----------------------------------------------------------------
@bot.command(name="remove")
async def cmd_remove(ctx, username: str):
    username = clean_username(username)
    if not VALID_USERNAME_RE.match(username):
        await ctx.send(f"⚠️ `{username}` doesn't look like a valid Instagram username. Just send the bare username, no link needed.")
        return
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
    username = clean_username(username)
    if not VALID_USERNAME_RE.match(username):
        await ctx.send(f"⚠️ `{username}` doesn't look like a valid Instagram username. Just send the bare username, no link needed.")
        return
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
        stats = await get_post_stats(session, link)

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
        stats = await get_post_stats(session, link)

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


# --- .customremove / .customrecover (manual test commands) ------------------
DURATION_RE = re.compile(r'(\d+)\s*([smhdSMHD])')


def parse_duration(raw: str):
    """Parses strings like '1h30m', '2d5h10m30s', '45s' into a timedelta."""
    matches = DURATION_RE.findall(raw.strip())
    if not matches:
        return None
    unit_map = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    total = {"seconds": 0, "minutes": 0, "hours": 0, "days": 0}
    for value, unit in matches:
        total[unit_map[unit.lower()]] += int(value)
    from datetime import timedelta
    return timedelta(**total)


def parse_count_arg(raw: str):
    """Parses '1234', '1,234', '12.3k', '4.5m' into an int, or None on failure."""
    raw = raw.strip().replace(",", "")
    if not raw:
        return None
    suffix_map = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    try:
        if raw[-1].lower() in suffix_map:
            return int(float(raw[:-1]) * suffix_map[raw[-1].lower()])
        return int(float(raw))
    except ValueError:
        return None


async def _fire_custom_alert(ctx, event: str, username, followers, following, posts, name, pfp, duration):
    username = clean_username(username)
    if not VALID_USERNAME_RE.match(username):
        await ctx.send(f"⚠️ `{username}` doesn't look like a valid username.")
        return

    followers_n = parse_count_arg(followers)
    following_n = parse_count_arg(following)
    posts_n = parse_count_arg(posts)
    if None in (followers_n, following_n, posts_n):
        await ctx.send("⚠️ Followers/Following/Posts need to be numbers (e.g. `1234`, `1.2k`, `4.5m`).")
        return

    duration = parse_duration(duration)
    if duration is None:
        await ctx.send("⚠️ Couldn't parse the duration. Use combos like `1h30m`, `2d5h`, `45s`, `10m`.")
        return

    pfp_url = None if pfp.strip().lower() in ("none", "-", "null", "") else pfp.strip()

    fake_stats = {
        "status": 200,
        "followers": followers_n,
        "following": following_n,
        "posts": posts_n,
        "pic": pfp_url,
        "full_name": name,
        "biography": "",
        "is_verified": False,
    }
    fake_start_time = datetime.now(timezone.utc) - duration

    embed, file = await build_account_embed(event, username, fake_stats, fake_start_time)
    embed.set_footer(text=embed.footer.text + " • TEST")
    if file:
        await ctx.send(embed=embed, file=file)
    else:
        await ctx.send(embed=embed)


@bot.command(name="customremove")
@commands.is_owner()
async def cmd_customremove(ctx, username: str, followers: str, following: str, posts: str, name: str, pfp: str, duration: str):
    await _fire_custom_alert(ctx, "removed", username, followers, following, posts, name, pfp, duration)


@bot.command(name="customrecover")
@commands.is_owner()
async def cmd_customrecover(ctx, username: str, followers: str, following: str, posts: str, name: str, pfp: str, duration: str):
    await _fire_custom_alert(ctx, "recovered", username, followers, following, posts, name, pfp, duration)


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
                embed, file = await build_account_embed(fired_event, entry["username"], use_stats, entry["start_time"])
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
                stats = await get_post_stats(session, entry["url"])
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
    if not HIKERAPI_KEY:
        raise RuntimeError("HIKERAPI_KEY environment variable is not set.")

    keep_alive()
    bot.run(token)
