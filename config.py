import os
from pathlib import Path
from dotenv import load_dotenv

# Load env variables from local .env
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# ── telegram credentials ─────────────────────────────────────────────────────
API_ID    = int(os.getenv("API_ID", "0"))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ── developer info ───────────────────────────────────────────────────────────
DEV_URL = os.getenv("DEV_URL", "https://t.me/DmOwner")

# ── log channel ──────────────────────────────────────────────────────────────
# set to your private channel's numeric id, e.g. -1001234567890
# leave as 0 to disable logging
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "0"))

# ── mongodb (Optional) ───────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME   = os.getenv("DB_NAME", "spoti_music_bot")
