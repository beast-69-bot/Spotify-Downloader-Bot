import os
import re
import json
import time
import asyncio
import base64
from difflib import SequenceMatcher

import requests
import pyrogram.errors
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BotCommand,
)
from pyrogram.enums import ParseMode, ButtonStyle

import config
import mongodb


DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SPOTI_BASE = "https://spotidown.app"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)

# Regex matching track, playlist, album, episode, and show links (including intl-xx URLs)
SPOTIFY_RE = re.compile(
    r"https?://(?:open\.)?spotify\.(?:com|link)/(?:intl-[a-zA-Z_-]+/)?(track|playlist|album|episode|show)/([A-Za-z0-9]+)"
)


# ── Spotify scraper ───────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    retry = requests.packages.urllib3.util.retry.Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": UA,
        "Referer": SPOTI_BASE + "/en2",
        "X-Requested-With": "XMLHttpRequest",
    })
    r = s.get(SPOTI_BASE + "/en2", timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    hidden = soup.find("input", {"type": "hidden", "name": re.compile(r"^_")})
    s._csrf = {hidden["name"]: hidden["value"]} if hidden else {}
    return s


def _fetch_action(s: requests.Session, spotify_url: str) -> str:
    r = s.post(SPOTI_BASE + "/action", data={
        "url": spotify_url,
        "g-recaptcha-response": "faketoken",
        **s._csrf,
    }, timeout=60)
    resp = r.json()
    if resp.get("error"):
        raise Exception(resp.get("message", "unknown error"))
    return resp["data"]


def _parse_forms(html: str):
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form", {"name": "submitspurl"})
    result = []
    for form in forms:
        fields = {}
        for inp in form.find_all("input"):
            if inp.get("name"):
                fields[inp["name"]] = inp.get("value", "")
        result.append(fields)
    img = soup.find("img")
    fallback_thumb = img["src"] if img else None
    return result, fallback_thumb


def _download_thumb(url: str, name: str):
    if not url or not url.startswith("http"):
        return None
    try:
        safe = re.sub(r'[\\/*?:"<>|]', "", name)[:80]
        path = os.path.join(DOWNLOAD_DIR, f"{safe}_thumb.jpg")
        with requests.get(url, timeout=30, headers={"User-Agent": UA}) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        return path if os.path.getsize(path) > 0 else None
    except Exception:
        return None


def _download_file(url: str, name: str) -> str:
    safe = re.sub(r'[\\/*?:"<>|]', "", name)[:100]
    ext = ".mp3"
    if ".m4a" in url.lower():
        ext = ".m4a"
    path = os.path.join(DOWNLOAD_DIR, f"{safe}{ext}")
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(128 * 1024):
                if chunk:
                    f.write(chunk)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError("downloaded file is empty")
    return path


def _fetch_one(s: requests.Session, form_data: dict, index: int, fallback_thumb: str | None = None):
    try:
        info      = json.loads(base64.b64decode(form_data.get("data", "")).decode())
        title     = info.get("name", f"Track {index + 1}")
        artist    = info.get("artist", "")
        name      = f"{title} - {artist}" if artist else title
        thumb_url = info.get("cover") or info.get("image") or info.get("thumb") or fallback_thumb
    except Exception:
        title, artist, name, thumb_url = f"Track {index + 1}", "", f"Track {index + 1}", fallback_thumb

    r = s.post(SPOTI_BASE + "/action/track", data=form_data, timeout=60)
    resp = r.json()
    if resp.get("error"):
        return index, name, title, artist, None, None, resp.get("message")

    soup = BeautifulSoup(resp["data"], "html.parser")

    img = soup.find("img")
    if img and not thumb_url:
        thumb_url = img.get("src")

    href = None
    a = soup.find("a", href=re.compile(r"/dl\?token=|rapid\.spotidown"))
    if a:
        href = a["href"]
        if href.startswith("/"):
            href = SPOTI_BASE + href
    else:
        for a in soup.find_all("a", href=re.compile(r"https?://")):
            href = a["href"]
            break

    if not href:
        return index, name, title, artist, None, None, "no download link found"

    try:
        local_path = _download_file(href, name)
    except Exception as e:
        return index, name, title, artist, None, None, f"download failed: {e}"

    local_thumb = _download_thumb(thumb_url, name)
    return index, name, title, artist, local_path, local_thumb, None


def spotify_get_track(spotify_url: str):
    s = _make_session()
    html = _fetch_action(s, spotify_url)
    forms, fallback_thumb = _parse_forms(html)
    if not forms:
        raise Exception("no track found")
    _, name, title, artist, local_path, thumb, err = _fetch_one(s, forms[0], 0, fallback_thumb)
    if err:
        raise Exception(err)
    return name, title, artist, local_path, thumb


def _fetch_playlist_forms(spotify_url: str):
    """Fetch playlist forms and retain the same session to prevent CSRF token expiration."""
    s = _make_session()
    html = _fetch_action(s, spotify_url)
    forms, fallback_thumb = _parse_forms(html)
    return s, forms, fallback_thumb


def _download_track_from_form(s: requests.Session, form_data: dict, index: int, fallback_thumb: str | None = None):
    """Download track using the SAME session that fetched the playlist forms."""
    _, name, title, artist, local_path, thumb, err = _fetch_one(s, form_data, index - 1, fallback_thumb)
    return name, title, artist, local_path, thumb, err


# ── Podcast helpers ───────────────────────────────────────────────────────────

def fetch_episode_meta(episode_id: str):
    url = f"https://open.spotify.com/embed/episode/{episode_id}"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            return None
        data = json.loads(script.string)
        entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
        title = entity.get("name")
        show = entity.get("subtitle") or ""
        images = entity.get("visualIdentity", {}).get("image", [])
        thumb = images[0].get("url") if images else None
        preview_url = entity.get("audioPreview", {}).get("url")
        return {
            "title": title,
            "show": show,
            "thumb": thumb,
            "preview_url": preview_url
        }
    except Exception as e:
        print(f"[podcast meta error] {e}")
        return None


def find_podcast_audio(title: str, show: str = ""):
    search_queries = []
    if show and title:
        search_queries.append(f"{show} {title}")
    if title:
        search_queries.append(title)
    if show:
        search_queries.append(show)

    for query in search_queries:
        url = f"https://itunes.apple.com/search?term={requests.utils.quote(query)}&media=podcast&entity=podcastEpisode&limit=10"
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            for res in data.get("results", []):
                track_name = res.get("trackName", "")
                ratio = SequenceMatcher(None, title.lower(), track_name.lower()).ratio()
                if ratio > 0.55 or title.lower() in track_name.lower() or track_name.lower() in title.lower():
                    return res.get("episodeUrl"), res.get("artworkUrl600")
        except Exception as e:
            print(f"[podcast search error] {e}")
    return None, None


def resolve_spotify_url(text: str) -> tuple[str, str, str] | None:
    short_match = re.search(r"https?://(?:spotify\.link|spoti\.fi)/[A-Za-z0-9]+", text)
    if short_match:
        try:
            r = requests.head(short_match.group(0), allow_redirects=True, timeout=10, headers={"User-Agent": UA})
            text = r.url
        except Exception:
            pass

    match = SPOTIFY_RE.search(text)
    if not match:
        return None
    stype = match.group(1)
    sid   = match.group(2)
    normalized_url = f"https://open.spotify.com/{stype}/{sid}"
    return stype, normalized_url, sid


# ── helpers ───────────────────────────────────────────────────────────────────

def cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def user_tag(user) -> str:
    return f"@{user.username}" if user.username else f"<code>{user.id}</code>"


# ── logging ───────────────────────────────────────────────────────────────────

async def log_new_user(bot: Client, user) -> None:
    if not config.LOG_CHANNEL:
        return
    name     = user.first_name + (f" {user.last_name}" if user.last_name else "")
    username = f"@{user.username}" if user.username else "<i>None</i>"
    text = (
        "<blockquote>"
        "🆕 <b>New User</b>\n\n"
        f"<b>Name     :</b>  <b>{name}</b>\n"
        f"<b>ID       :</b>  <code>{user.id}</code>\n"
        f"<b>Username :</b>  {username}"
        "</blockquote>"
    )
    photos = []
    try:
        async for photo in bot.get_chat_photos(user.id, limit=1):
            photos.append(photo)
    except Exception:
        pass
    try:
        if photos:
            await bot.send_photo(config.LOG_CHANNEL, photos[0].file_id,
                                 caption=text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(config.LOG_CHANNEL, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"[log] new-user: {e}")


async def log_download(bot: Client, user, name: str) -> None:
    if not config.LOG_CHANNEL:
        return
    tag   = user_tag(user)
    uname = user.first_name + (f" {user.last_name}" if user.last_name else "")
    text  = (
        "<blockquote>"
        "🎵 <b>Track / Episode Downloaded</b>\n\n"
        f"<b>User     :</b>  <b>{uname}</b>  ({tag})\n"
        f"<b>ID       :</b>  <code>{user.id}</code>\n\n"
        f"<b>Title    :</b>  <i>{name}</i>"
        "</blockquote>"
    )
    try:
        await bot.send_message(config.LOG_CHANNEL, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"[log] download: {e}")


# ── handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(bot: Client, msg: Message):
    user = msg.from_user
    try:
        if await mongodb.is_new_user(user.id):
            await mongodb.add_user(
                user_id=user.id,
                first_name=user.first_name,
                username=user.username,
                dc_id=user.dc_id,
            )
            await log_new_user(bot, user)
        else:
            # Ensure existing user is recorded
            await mongodb.add_user(
                user_id=user.id,
                first_name=user.first_name,
                username=user.username,
                dc_id=user.dc_id,
            )
    except Exception as e:
        print(f"[db] {e}")

    await msg.reply_text(
        "<blockquote>\n"
        "🎵 <b>Welcome to Spotify Music & Podcast Downloader!</b>\n\n"
        "<b>I can download high-quality audio from Spotify:</b>\n"
        "• 🎧 <b>Tracks:</b> 320kbps MP3 with Album Art\n"
        "• 📀 <b>Albums:</b> Complete album tracks\n"
        "• 📋 <b>Playlists:</b> Full playlist with live progress\n"
        "• 🎙️ <b>Podcasts:</b> Episodes with cover & show tags\n\n"
        "<i>⚡ Just paste any Spotify link below to get started!</i>\n"
        "</blockquote>",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Updates Channel 📢", url=config.CHANNEL_URL, style=ButtonStyle.PRIMARY),
                InlineKeyboardButton("Dev 👨‍💻", url=config.DEV_URL, style=ButtonStyle.PRIMARY),
            ],
            [
                InlineKeyboardButton("Stats 📊", callback_data="stats", style=ButtonStyle.PRIMARY),
                InlineKeyboardButton("Credits ⭐", callback_data="credits", style=ButtonStyle.PRIMARY),
            ],
        ]),
    )


async def cmd_stats(bot: Client, msg: Message):
    total_users = await mongodb.get_total_users()
    await msg.reply_text(
        "<blockquote>\n"
        "📊 <b>Bot Live Statistics</b>\n\n"
        f"👥 <b>Total Users:</b> <code>{total_users}</code>\n"
        "🟢 <b>Status:</b> <i>Online & Active</i>\n"
        "⚡ <b>Engine:</b> <i>Python + Kurigram</i>\n"
        "</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Updates Channel 📢", url=config.CHANNEL_URL),
                InlineKeyboardButton("Developer 👨‍💻", url=config.DEV_URL),
            ]
        ]),
    )


async def cb_stats(_, cb: CallbackQuery):
    await cb.answer()
    total_users = await mongodb.get_total_users()
    await cb.message.reply_text(
        "<blockquote>\n"
        "📊 <b>Bot Live Statistics</b>\n\n"
        f"👥 <b>Total Users:</b> <code>{total_users}</code>\n"
        "🟢 <b>Status:</b> <i>Online & Active</i>\n"
        "⚡ <b>Engine:</b> <i>Python + Kurigram</i>\n"
        "</blockquote>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(bot: Client, msg: Message):
    text = (
        "<blockquote>\n"
        "📖 <b>Spotify Downloader Help & Usage Guide</b>\n\n"
        "<b>Supported Spotify Links:</b>\n"
        "• 🎵 <b>Track:</b> <code>https://open.spotify.com/track/...</code>\n"
        "• 📀 <b>Album:</b> <code>https://open.spotify.com/album/...</code>\n"
        "• 📋 <b>Playlist:</b> <code>https://open.spotify.com/playlist/...</code>\n"
        "• 🎙️ <b>Podcast:</b> <code>https://open.spotify.com/episode/...</code>\n\n"
        "<b>How to use:</b>\n"
        "1. Open Spotify and copy the link of any song, album, or podcast.\n"
        "2. Send or paste the link here in this chat.\n"
        "3. The bot will automatically fetch, process, and send your audio!\n\n"
        "<b>Available Commands:</b>\n"
        "• /start - Start the bot\n"
        "• /stats - View total users & stats\n"
        "• /help - Show this guide\n"
        "• /ping - Check latency & response speed\n"
        "• /credits - Developer & channel information\n"
        "</blockquote>"
    )
    await msg.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Updates Channel", url=config.CHANNEL_URL),
                InlineKeyboardButton("Developer", url=config.DEV_URL),
            ]
        ]),
    )


async def cmd_ping(bot: Client, msg: Message):
    start_t = time.time()
    sent_msg = await msg.reply_text("🏓 <i>Pinging...</i>", parse_mode=ParseMode.HTML)
    latency = round((time.time() - start_t) * 1000, 2)
    await sent_msg.edit_text(
        f"<blockquote>🏓 <b>Pong!</b>\n⚡ <b>Latency:</b> <code>{latency}ms</code>\n🟢 <b>Status:</b> <i>Online & Ready</i></blockquote>",
        parse_mode=ParseMode.HTML
    )


async def cmd_credits(bot: Client, msg: Message):
    await msg.reply_text(
        "<blockquote>\n"
        "<b>Credits & Support</b>\n\n"
        "<b>Developer:</b> @himayubhai\n"
        "<b>Updates Channel:</b> @az_hawas_adda\n\n"
        "<i>Enjoy downloading your favorite Spotify music and podcasts!</i>\n"
        "</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Updates Channel", url=config.CHANNEL_URL),
                InlineKeyboardButton("Developer", url=config.DEV_URL),
            ]
        ]),
    )


async def cb_credits(_, cb: CallbackQuery):
    await cb.answer()
    await cb.message.reply_text(
        "<blockquote>\n"
        "<b>Credits</b>\n\n"
        "<b>Developer:</b> @himayubhai\n"
        "<b>Updates Channel:</b> @az_hawas_adda\n\n"
        "<i>Enjoy downloading your favorite Spotify music & podcasts!</i>\n"
        "</blockquote>",
        parse_mode=ParseMode.HTML,
    )


async def cb_help(_, cb: CallbackQuery):
    await cb.answer()
    await cmd_help(None, cb.message)


async def handle_message(bot: Client, msg: Message):
    text = msg.text.strip()
    user = msg.from_user

    # Keep track of active users
    try:
        await mongodb.add_user(user.id, user.first_name, user.username, user.dc_id)
    except Exception:
        pass

    resolved = resolve_spotify_url(text)
    if not resolved:
        await msg.reply_text(
            "<blockquote>⚠️ That doesn't look like a valid Spotify track, album, playlist, or podcast link.\n\nSend /help to view examples!</blockquote>",
            parse_mode=ParseMode.HTML
        )
        return

    stype, url, sid = resolved

    # ── single track ──────────────────────────────────────────────────────────
    if stype == "track":
        status = await msg.reply_text("🔍 <i>Fetching track information...</i>", parse_mode=ParseMode.HTML)
        local_path = thumb = None
        try:
            loop = asyncio.get_running_loop()
            name, title, artist, local_path, thumb = await loop.run_in_executor(
                None, spotify_get_track, url
            )
            await status.edit_text("📥 <i>Uploading audio to Telegram...</i>", parse_mode=ParseMode.HTML)
            
            for retry in range(3):
                try:
                    await msg.reply_audio(
                        audio=local_path,
                        title=title,
                        performer=artist,
                        thumb=thumb,
                        caption=f"<b>{name}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                    break
                except pyrogram.errors.FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                except Exception as e:
                    if retry == 2:
                        raise e
                    await asyncio.sleep(1)

            try:
                await status.delete()
            except Exception:
                pass

            await log_download(bot, user, name)

        except Exception as e:
            await status.edit_text(
                f"<blockquote>❌ <b>Something went wrong:</b>\n\n<code>{e}</code></blockquote>",
                parse_mode=ParseMode.HTML,
            )
        finally:
            cleanup(local_path)
            cleanup(thumb)

    # ── playlist / album ──────────────────────────────────────────────────────
    elif stype in ("playlist", "album"):
        status = await msg.reply_text(
            f"🔍 <i>Fetching {stype} details from Spotify...</i>",
            parse_mode=ParseMode.HTML
        )

        loop = asyncio.get_running_loop()
        try:
            # Retain the exact session that loaded the forms to preserve cookies and CSRF tokens
            session, forms, fallback_thumb = await loop.run_in_executor(None, _fetch_playlist_forms, url)
        except Exception as e:
            await status.edit_text(
                f"<blockquote>❌ <b>Error fetching {stype}:</b>\n\n<code>{e}</code></blockquote>",
                parse_mode=ParseMode.HTML,
            )
            return

        total = len(forms)
        if total == 0:
            await status.edit_text(
                f"<blockquote>⚠️ <b>No tracks found!</b>\nThe {stype} might be empty, private, or region-restricted.</blockquote>",
                parse_mode=ParseMode.HTML,
            )
            return

        await status.edit_text(
            f"<blockquote>🎵 <b>Found {total} tracks in {stype}!</b>\n\nStarting download and delivery...</blockquote>",
            parse_mode=ParseMode.HTML,
        )

        completed = 0
        failed = 0
        last_edit_time = time.time()

        for index, form in enumerate(forms, start=1):
            now = time.time()
            if now - last_edit_time > 2.5:
                try:
                    await status.edit_text(
                        f"<blockquote>📥 <b>Downloading {stype.capitalize()} ({index}/{total})</b>\n\n"
                        f"✅ <b>Sent:</b> {completed}   ❌ <b>Failed:</b> {failed}</blockquote>",
                        parse_mode=ParseMode.HTML,
                    )
                    last_edit_time = now
                except Exception:
                    pass

            local_path = None
            thumb_path = None
            try:
                # Pass session to reuse token
                name, title, artist, local_path, thumb_path, err = await loop.run_in_executor(
                    None, _download_track_from_form, session, form, index, fallback_thumb
                )
                if err or not local_path:
                    print(f"[playlist] track {index} skipped: {err}")
                    failed += 1
                    continue

                sent = False
                for retry in range(3):
                    try:
                        await msg.reply_audio(
                            audio=local_path,
                            title=title,
                            performer=artist,
                            thumb=thumb_path,
                            caption=f"<b>{name}</b>",
                            parse_mode=ParseMode.HTML,
                        )
                        sent = True
                        break
                    except pyrogram.errors.FloodWait as fw:
                        print(f"[floodwait] Waiting {fw.value}s...")
                        await asyncio.sleep(fw.value + 1)
                    except Exception as e:
                        print(f"[send error] {e}")
                        await asyncio.sleep(1.5)

                if sent:
                    completed += 1
                    await log_download(bot, user, name)
                else:
                    failed += 1

                await asyncio.sleep(1.0)

            except Exception as e:
                print(f"[track error] {e}")
                failed += 1
            finally:
                cleanup(local_path)
                cleanup(thumb_path)

        try:
            await status.edit_text(
                f"<blockquote>🎉 <b>{stype.capitalize()} Complete!</b>\n\n"
                f"📁 <b>Total Tracks:</b> {total}\n"
                f"✅ <b>Successfully Sent:</b> {completed}\n"
                f"❌ <b>Failed:</b> {failed}</blockquote>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # ── podcast episode ───────────────────────────────────────────────────────
    elif stype == "episode":
        status = await msg.reply_text("🔍 <i>Fetching podcast episode details...</i>", parse_mode=ParseMode.HTML)
        local_path = thumb_path = None
        try:
            loop = asyncio.get_running_loop()
            meta = await loop.run_in_executor(None, fetch_episode_meta, sid)
            if not meta or not meta.get("title"):
                await status.edit_text("❌ <b>Could not retrieve episode details.</b>", parse_mode=ParseMode.HTML)
                return

            title = meta["title"]
            show = meta["show"] or "Podcast"
            name = f"{title} - {show}"
            thumb_url = meta["thumb"]

            await status.edit_text(
                f"<blockquote>🎙️ <b>Found:</b> <i>{title}</i>\n📻 <b>Podcast:</b> <i>{show}</i>\n\n📥 <i>Downloading audio stream...</i></blockquote>",
                parse_mode=ParseMode.HTML
            )

            # Search for public audio file
            audio_url, itunes_thumb = await loop.run_in_executor(None, find_podcast_audio, title, show)
            is_preview = False
            if not audio_url and meta.get("preview_url"):
                audio_url = meta["preview_url"]
                is_preview = True

            if not audio_url:
                await status.edit_text(
                    "<blockquote>⚠️ <b>Spotify Exclusive DRM Podcast</b>\n\n"
                    "This podcast episode is DRM-protected and hosted exclusively inside Spotify without an external public audio stream.</blockquote>",
                    parse_mode=ParseMode.HTML
                )
                return

            target_thumb = thumb_url or itunes_thumb
            local_path = await loop.run_in_executor(None, _download_file, audio_url, name)
            thumb_path = await loop.run_in_executor(None, _download_thumb, target_thumb, name)

            caption = f"🎙️ <b>{title}</b>\n📻 <b>Show:</b> <i>{show}</i>"
            if is_preview:
                caption += "\n\n<i>⚠️ Note: Episode is a Spotify DRM exclusive. Sending high-quality audio preview.</i>"

            # Send to Telegram with retry
            for retry in range(3):
                try:
                    await msg.reply_audio(
                        audio=local_path,
                        title=title,
                        performer=show,
                        thumb=thumb_path,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                    break
                except pyrogram.errors.FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                except Exception as e:
                    if retry == 2:
                        raise e
                    await asyncio.sleep(1)

            try:
                await status.delete()
            except Exception:
                pass

            await log_download(bot, user, name)

        except Exception as e:
            await status.edit_text(
                f"<blockquote>❌ <b>Error downloading podcast:</b>\n\n<code>{e}</code></blockquote>",
                parse_mode=ParseMode.HTML
            )
        finally:
            cleanup(local_path)
            cleanup(thumb_path)

    # ── podcast show / series ─────────────────────────────────────────────────
    elif stype == "show":
        await msg.reply_text(
            "<blockquote>📻 <b>Podcast Show / Series Link Detected</b>\n\n"
            "To download a specific episode, please copy and send the link of that <b>individual episode</b>!\n\n"
            "<i>Example:</i> <code>https://open.spotify.com/episode/...</code></blockquote>",
            parse_mode=ParseMode.HTML
        )


# ── entry point ───────────────────────────────────────────────────────────────

async def main():
    bot = Client(
        "spoti_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
    )

    # Command handlers
    bot.add_handler(MessageHandler(cmd_start, filters.command("start") & filters.private))
    bot.add_handler(MessageHandler(cmd_stats, filters.command(["stats", "stat"]) & filters.private))
    bot.add_handler(MessageHandler(cmd_help, filters.command("help") & filters.private))
    bot.add_handler(MessageHandler(cmd_ping, filters.command("ping") & filters.private))
    bot.add_handler(MessageHandler(cmd_credits, filters.command("credits") & filters.private))

    # Callback query handlers
    bot.add_handler(CallbackQueryHandler(cb_stats, filters.regex("^stats$")))
    bot.add_handler(CallbackQueryHandler(cb_credits, filters.regex("^credits$")))
    bot.add_handler(CallbackQueryHandler(cb_help, filters.regex("^help$")))

    # Message handler
    bot.add_handler(MessageHandler(
        handle_message,
        filters.text & filters.private & ~filters.command(["start", "stats", "stat", "help", "ping", "credits"])
    ))

    await mongodb.connect()
    await bot.start()

    # Automatically register bot commands with Telegram
    try:
        await bot.set_bot_commands([
            BotCommand("start", "Start the bot & main menu"),
            BotCommand("stats", "View bot statistics & user count"),
            BotCommand("help", "How to use this bot"),
            BotCommand("ping", "Check latency & status"),
            BotCommand("credits", "Developer & channel info"),
        ])
        print("[bot] Bot commands registered with Telegram.")
    except Exception as e:
        print(f"[bot] Could not register commands: {e}")

    print("[bot] running — waiting for messages...")
    await idle()
    await bot.stop()
    await mongodb.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
