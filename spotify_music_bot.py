import os
import re
import json
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
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
MAX_WORKERS = 5

SPOTIFY_RE = re.compile(
    r"https?://open\.spotify\.com/(track|playlist|album)/[A-Za-z0-9]+"
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
    s._csrf = {hidden["name"]: hidden["value"]}
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
    path = os.path.join(DOWNLOAD_DIR, f"{safe}.mp3")
    with requests.get(url, stream=True, timeout=120,
                      headers={"User-Agent": UA}) as r:
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
        return index, name, title, artist, None, None, "no link found"

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


def spotify_get_playlist(spotify_url: str, on_result=None):
    s = _make_session()
    html = _fetch_action(s, spotify_url)
    forms, fallback_thumb = _parse_forms(html)
    total = len(forms)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one, s, form, i, fallback_thumb): i
            for i, form in enumerate(forms)
        }
        for future in as_completed(futures):
            index, name, title, artist, local_path, thumb, err = future.result()
            if on_result:
                on_result(index, total, name, title, artist, local_path, thumb, err)


# ── helpers ───────────────────────────────────────────────────────────────────

def cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def user_tag(user) -> str:
    return f"@{user.username}" if user.username else f"<code>{user.id}</code>"


def spotify_type(url: str) -> str:
    if "/track/" in url:
        return "track"
    if "/playlist/" in url:
        return "playlist"
    if "/album/" in url:
        return "album"
    return "unknown"


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
        "🎵 <b>Track Downloaded</b>\n\n"
        f"<b>User     :</b>  <b>{uname}</b>  ({tag})\n"
        f"<b>ID       :</b>  <code>{user.id}</code>\n\n"
        f"<b>Track    :</b>  <i>{name}</i>"
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
    except Exception as e:
        print(f"[db] {e}")

    await msg.reply_text(
        "<blockquote>\n"
        "<b>Hey 👋</b>\n"
        "<b>Send me a Spotify track or playlist link and I'll download it for you.</b>\n\n"
        "<i>Just paste the link below.</i>\n"
        "</blockquote>",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Updates Channel", url=config.CHANNEL_URL, style=ButtonStyle.PRIMARY),
                InlineKeyboardButton("Dev", url=config.DEV_URL, style=ButtonStyle.PRIMARY),
            ],
            [
                InlineKeyboardButton("Credits", callback_data="credits", style=ButtonStyle.PRIMARY),
            ],
        ]),
    )


async def cb_credits(_, cb: CallbackQuery):
    await cb.answer()
    await cb.message.reply_text(
        "<blockquote>\n"
        "<b>Credits</b>\n\n"
        "<b>Developer:</b> @himayubhai\n"
        "<b>Updates Channel:</b> @az_hawas_adda\n\n"
        "<i>Enjoy downloading your favorite Spotify music!</i>\n"
        "</blockquote>",
        parse_mode=ParseMode.HTML,
    )


async def handle_message(bot: Client, msg: Message):
    text = msg.text.strip()

    match = SPOTIFY_RE.search(text)
    if not match:
        await msg.reply_text("that doesn't look like a spotify link.")
        return

    url   = match.group(0)
    stype = spotify_type(url)
    user  = msg.from_user

    # ── single track ──────────────────────────────────────────────────────────
    if stype == "track":
        status = await msg.reply_text("fetching track...")
        local_path = thumb = None
        try:
            loop = asyncio.get_running_loop()
            name, title, artist, local_path, thumb = await loop.run_in_executor(
                None, spotify_get_track, url
            )
            await status.edit_text("uploading...")
            if thumb:
                await msg.reply_photo(photo=thumb, caption=f"<b>{name}</b>", parse_mode=ParseMode.HTML)
            await msg.reply_audio(
                audio=local_path,
                title=title,
                performer=artist,
                thumb=thumb,
                parse_mode=ParseMode.HTML,
            )
            await status.delete()
            await log_download(bot, user, name)

        except Exception as e:
            await status.edit_text(
                f"something went wrong\n\n<code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )
        finally:
            cleanup(local_path)
            cleanup(thumb)

    # ── playlist / album ──────────────────────────────────────────────────────
    elif stype in ("playlist", "album"):
        status = await msg.reply_text("fetching playlist...")

        completed = 0
        failed    = 0
        main_loop = asyncio.get_event_loop()

        def on_result(_, total, name, title, artist, local_path, thumb, err):
            nonlocal completed, failed
            if err:
                failed += 1
            else:
                completed += 1
            asyncio.run_coroutine_threadsafe(
                _send_track(
                    bot, msg, status,
                    name, title, artist, local_path, thumb, err,
                    completed, failed, total, user,
                ),
                loop=main_loop,
            )

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: spotify_get_playlist(url, on_result=on_result),
            )
            try:
                await status.delete()
            except Exception:
                pass

        except Exception as e:
            await status.edit_text(
                f"something went wrong\n\n<code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )

    else:
        await msg.reply_text("unsupported spotify link type.")


async def _send_track(
    bot, msg, status,
    name, title, artist, local_path, thumb, err,
    completed, failed, total, user,
):
    if err:
        print(f"[skip] {name}: {err}")
        return
    try:
        await msg.reply_audio(
            audio=local_path,
            caption=f"<b>{name}</b>",
            title=title,
            performer=artist,
            thumb=thumb,
            parse_mode=ParseMode.HTML,
        )
        await log_download(bot, user, name)
        try:
            await status.edit_text(
                f"<blockquote>📥 <b>{completed + failed}/{total}</b> done\n✅ <b>{completed}</b> succeeded   ❌ <b>{failed}</b> failed</blockquote>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    except Exception as e:
        print(f"[send] {name}: {e}")
    finally:
        cleanup(local_path)
        cleanup(thumb)


# ── entry point ───────────────────────────────────────────────────────────────

async def main():
    bot = Client(
        "spoti_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
    )

    bot.add_handler(MessageHandler(cmd_start,        filters.command("start") & filters.private))
    bot.add_handler(CallbackQueryHandler(cb_credits,  filters.regex("^credits$")))
    bot.add_handler(MessageHandler(handle_message,
                                   filters.text & filters.private & ~filters.command(["start"])))

    await mongodb.connect()
    await bot.start()
    print("[bot] running — waiting for messages...")
    await idle()
    await bot.stop()
    await mongodb.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
