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
            db.execute("""CREATE TABLE IF NOT EXISTS linked_accounts (
                discord_user_id TEXT PRIMARY KEY,
                emlalock_user_id TEXT NOT NULL,
                encrypted_api_key BLOB NOT NULL,
                connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS user_preferences (
                discord_user_id TEXT PRIMARY KEY,
                preferred_session_id TEXT,
                preferences_json TEXT NOT NULL DEFAULT '{}'
            )""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_emlalock_user_link ON linked_accounts(emlalock_user_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                wearer_role_id TEXT,
                keyholder_role_id TEXT,
                dual_role_id TEXT
            )""")

    def link_account(self, discord_user_id: int, emlalock_user_id: str, api_key: str):
        encrypted = self.cipher.encrypt(api_key.encode("utf-8"))
        with self._connect() as db:
            existing = db.execute("SELECT discord_user_id FROM linked_accounts WHERE emlalock_user_id=?",
                                   (emlalock_user_id,)).fetchone()
            if existing and existing["discord_user_id"] != str(discord_user_id):
                raise ValueError("That EmlaLock User ID is already linked to another Discord account.")
            db.execute("""INSERT INTO linked_accounts(discord_user_id, emlalock_user_id, encrypted_api_key)
                VALUES (?, ?, ?) ON CONFLICT(discord_user_id) DO UPDATE SET
                emlalock_user_id=excluded.emlalock_user_id,
                encrypted_api_key=excluded.encrypted_api_key,
                connected_at=CURRENT_TIMESTAMP""",
                (str(discord_user_id), emlalock_user_id, encrypted))

    def _decode_account(self, row):
        if row is None:
            return None
        try:
            key = self.cipher.decrypt(row["encrypted_api_key"]).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Stored credential cannot be decrypted; check the encryption key.") from exc
        return {"discord_user_id": int(row["discord_user_id"]),
                "emlalock_user_id": row["emlalock_user_id"], "api_key": key}

    def get_account(self, discord_user_id: int) -> Optional[dict]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM linked_accounts WHERE discord_user_id=?",
                             (str(discord_user_id),)).fetchone()
        return self._decode_account(row)

    def get_account_by_emlalock_id(self, emlalock_user_id: str) -> Optional[dict]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM linked_accounts WHERE emlalock_user_id=?",
                             (str(emlalock_user_id),)).fetchone()
        return self._decode_account(row)

    def list_guild_accounts(self, discord_user_ids: list[int]) -> list[dict]:
        if not discord_user_ids:
            return []
        placeholders = ",".join("?" for _ in discord_user_ids)
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM linked_accounts WHERE discord_user_id IN ({placeholders})",
                              [str(x) for x in discord_user_ids]).fetchall()
        return [self._decode_account(row) for row in rows]

    def unlink_account(self, discord_user_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM linked_accounts WHERE discord_user_id=?",
                                (str(discord_user_id),))
            return cursor.rowcount > 0

    def set_preferred_session(self, discord_user_id: int, session_id: str):
        with self._connect() as db:
            db.execute("""INSERT INTO user_preferences(discord_user_id, preferred_session_id)
                VALUES (?, ?) ON CONFLICT(discord_user_id) DO UPDATE SET
                preferred_session_id=excluded.preferred_session_id""",
                (str(discord_user_id), session_id))

    def save_guild_roles(self, guild_id: int, wearer: int, holder: int, dual: int):
        with self._connect() as db:
            db.execute("""INSERT INTO guild_settings(guild_id, wearer_role_id, keyholder_role_id, dual_role_id)
                VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET
                wearer_role_id=excluded.wearer_role_id, keyholder_role_id=excluded.keyholder_role_id,
                dual_role_id=excluded.dual_role_id""",
                (str(guild_id), str(wearer), str(holder), str(dual)))

    def get_guild_roles(self, guild_id: int):
        with self._connect() as db:
            row = db.execute("SELECT * FROM guild_settings WHERE guild_id=?",
                             (str(guild_id),)).fetchone()
        return dict(row) if row else None
