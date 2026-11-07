import os
import sqlite3
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


class Storage:
    def __init__(self, database_path: str, encryption_key: str):
        if not encryption_key:
            raise ValueError("CREDENTIAL_ENCRYPTION_KEY is required")
        try:
            self.cipher = Fernet(encryption_key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key") from exc

        self.database_path = database_path
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS linked_accounts (
                    discord_user_id TEXT PRIMARY KEY,
                    emlalock_user_id TEXT NOT NULL,
                    encrypted_api_key BLOB NOT NULL,
                    connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    discord_user_id TEXT PRIMARY KEY,
                    preferred_session_id TEXT,
                    preferences_json TEXT NOT NULL DEFAULT '{}'
                )
            """)

    def link_account(self, discord_user_id: int, emlalock_user_id: str, api_key: str):
        encrypted = self.cipher.encrypt(api_key.encode("utf-8"))
        with self._connect() as db:
            db.execute("""
                INSERT INTO linked_accounts(discord_user_id, emlalock_user_id, encrypted_api_key)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_user_id) DO UPDATE SET
                    emlalock_user_id=excluded.emlalock_user_id,
                    encrypted_api_key=excluded.encrypted_api_key,
                    connected_at=CURRENT_TIMESTAMP
            """, (str(discord_user_id), emlalock_user_id, encrypted))

    def get_account(self, discord_user_id: int) -> Optional[dict]:
        with self._connect() as db:
            row = db.execute(
                "SELECT emlalock_user_id, encrypted_api_key FROM linked_accounts WHERE discord_user_id=?",
                (str(discord_user_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            api_key = self.cipher.decrypt(row["encrypted_api_key"]).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Stored credential could not be decrypted; check encryption key configuration") from exc
        return {"emlalock_user_id": row["emlalock_user_id"], "api_key": api_key}

    def unlink_account(self, discord_user_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM linked_accounts WHERE discord_user_id=?",
                (str(discord_user_id),),
            )
            return cursor.rowcount > 0

    def set_preferred_session(self, discord_user_id: int, session_id: str):
        with self._connect() as db:
            db.execute("""
                INSERT INTO user_preferences(discord_user_id, preferred_session_id)
                VALUES (?, ?)
                ON CONFLICT(discord_user_id) DO UPDATE SET preferred_session_id=excluded.preferred_session_id
            """, (str(discord_user_id), session_id))
