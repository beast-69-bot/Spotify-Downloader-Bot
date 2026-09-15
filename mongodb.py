"""
MongoDB helper module (Optional).
Used to track unique users. If MONGO_URI is not provided, the bot continues running normally without database logging.
"""

from config import MONGO_URI, DB_NAME

_client = None
_users = None


async def connect():
    global _client, _users

    if not MONGO_URI:
        print("[mongodb] MONGO_URI not set. Running in database-free mode.")
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
        print(f"[mongodb] Connection failed: {e}. Continuing without database.")
        _client = None
        _users = None


async def disconnect():
    global _client
    if _client:
        _client.close()
        print("[mongodb] disconnected")


async def is_new_user(user_id: int) -> bool:
    if _users is None:
        return False
    try:
        doc = await _users.find_one({"user_id": user_id}, {"_id": 1})
        return doc is None
    except Exception:
        return False


async def add_user(user_id: int, first_name: str, username: str | None, dc_id: int | None):
    if _users is None:
        return
    try:
        # upsert so it's safe to call more than once without creating duplicates
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
