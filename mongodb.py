"""
Database module with MongoDB support and automatic SQLite fallback.
Tracks unique users so /stats and new user notifications work seamlessly with or without MongoDB Atlas.
"""

import os
import sqlite3
from config import MONGO_URI, DB_NAME

_client = None
_users = None
SQLITE_DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")


def _init_sqlite():
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[sqlite] init error: {e}")


async def connect():
    global _client, _users

    # Always ensure SQLite is initialized as local storage/fallback
    _init_sqlite()

    if not MONGO_URI:
        print("[db] Running with local SQLite database (users.db).")
        return

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        _client = AsyncIOMotorClient(MONGO_URI)
        db = _client[DB_NAME]
        _users = db["users"]

        # unique index so we never get duplicate entries
        await _users.create_index("user_id", unique=True)
        print(f"[mongodb] connected → {DB_NAME}")
    except Exception as e:
        print(f"[mongodb] Connection failed: {e}. Falling back to SQLite.")
        _client = None
        _users = None


async def disconnect():
    global _client
    if _client:
        _client.close()
        print("[mongodb] disconnected")


async def is_new_user(user_id: int) -> bool:
    if _users is not None:
        try:
            doc = await _users.find_one({"user_id": user_id}, {"_id": 1})
            return doc is None
        except Exception:
            pass

    # SQLite fallback
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row is None
    except Exception:
        return False


async def add_user(user_id: int, first_name: str, username: str | None, dc_id: int | None = None):
    # If MongoDB is available, save there
    if _users is not None:
        try:
            await _users.update_one(
                {"user_id": user_id},
                {
                    "$setOnInsert": {
                        "user_id":    user_id,
                        "first_name": first_name,
                        "username":   username,
                        "dc_id":      dc_id,
                    }
                },
                upsert=True,
            )
        except Exception as e:
            print(f"[mongodb] add_user failed: {e}")

    # Always also record in SQLite for instant local query / backup
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name, username) VALUES (?, ?, ?)",
            (user_id, first_name, username or "")
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[sqlite] add_user failed: {e}")


async def get_total_users() -> int:
    """Return total registered users count."""
    if _users is not None:
        try:
            count = await _users.count_documents({})
            return count
        except Exception:
            pass

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"[db] get_total_users error: {e}")
        return 0
