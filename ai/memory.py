import json


class UserMemory:
    """Stores non-secret preferences only; credentials belong in encrypted storage."""
    ALLOWED_KEYS = {"default_time_unit", "preferred_session_id", "confirmation_preference"}

    def __init__(self, storage):
        self.storage = storage

    def get_preferences(self, discord_user_id: int) -> dict:
        with self.storage._connect() as db:
            row = db.execute(
                "SELECT preferences_json FROM user_preferences WHERE discord_user_id=?",
                (str(discord_user_id),),
            ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["preferences_json"])
        except json.JSONDecodeError:
            return {}

    def set_preference(self, discord_user_id: int, key: str, value):
        if key not in self.ALLOWED_KEYS:
            raise ValueError("Unsupported preference")
        prefs = self.get_preferences(discord_user_id)
        prefs[key] = value
        with self.storage._connect() as db:
            db.execute("""
                INSERT INTO user_preferences(discord_user_id, preferences_json)
                VALUES (?, ?)
                ON CONFLICT(discord_user_id) DO UPDATE SET preferences_json=excluded.preferences_json
            """, (str(discord_user_id), json.dumps(prefs)))
