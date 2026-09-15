# 🎵 Spotify Music Bot

A Telegram bot that downloads Spotify tracks, playlists, and albums and sends them directly to your chat as audio files.

---

## What it does

- Send any Spotify track, playlist, or album link
- Bot fetches and downloads the audio via [spotidown.app](https://spotidown.app)
- Sends it back as an `.mp3` audio file with title and artist tagged
- Playlists and albums are processed in parallel and sent track by track with a live progress counter
- New users and downloads are optionally logged to a private Telegram channel
- Unique users are tracked in MongoDB

---

## Stack

- **Python 3.11**
- **Pyrogram / Kurigram** — Telegram client
- **BeautifulSoup + Requests** — scraping & downloading
- **Motor** — async MongoDB driver

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `API_ID` | ✅ | Telegram App ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | ✅ | Telegram App Hash from [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | ✅ | Bot token from [@BotFather](https://t.me/BotFather) |
| `MONGO_URI` | ❌ | MongoDB connection string (Optional — leave empty to run without DB) |
| `DB_NAME` | ❌ | Database name (default: `spoti_music_bot`) |
| `LOG_CHANNEL` | ❌ | Numeric ID of your private log channel (default: `0` = off) |
| `DEV_URL` | ❌ | Your Telegram profile URL shown in the /start button |

---

## Running locally

```bash
pip install -r requirements.txt
# set your env vars, then:
python spotify_music_bot.py
```

---

## Deploy with Docker

```bash
docker build -t spoti-bot .
docker run --env-file .env spoti-bot
```

Also ships with ready configs for **Railway** (`railway.toml`), **Render** (`render.yaml`), **Heroku** (`heroku.yml`), and **Koyeb** (`nixpacks.toml`).

---

## Usage

1. Start the bot with `/start`
2. Paste a Spotify link - track, playlist, or album
3. Get your audio

---

## Credits

Made by **LastPerson X Mark**.