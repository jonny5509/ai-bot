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
            db.execute("""CREATE TABLE IF NOT EXISTS daily_random_add_usage (
                emlalock_user_id TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                PRIMARY KEY (emlalock_user_id, usage_date)
            )""")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_emlalock_user_link ON linked_accounts(emlalock_user_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS friendlink_messages (
                message_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                wearer_emlalock_id TEXT NOT NULL
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(guild_settings)").fetchall()}
            if "notification_channel_id" not in columns:
                db.execute("ALTER TABLE guild_settings ADD COLUMN notification_channel_id TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                wearer_role_id TEXT,
                keyholder_role_id TEXT,
                dual_role_id TEXT,
                notification_channel_id TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS guild_channels (guild_id TEXT NOT NULL, purpose TEXT NOT NULL, channel_id TEXT, PRIMARY KEY (guild_id, purpose))""")
            db.execute("""CREATE TABLE IF NOT EXISTS adoption_roles (
                guild_id TEXT PRIMARY KEY,
                looking_for_keyholder_role_id TEXT NOT NULL,
                looking_for_wearer_role_id TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS adoption_requests (
                request_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                requester_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

    @staticmethod
    def _utc_usage_date():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).date().isoformat()

    def daily_random_add_used(self, emlalock_user_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM daily_random_add_usage WHERE emlalock_user_id=? AND usage_date=?",
                (str(emlalock_user_id), self._utc_usage_date()),
            ).fetchone()
        return row is not None

    def claim_daily_random_add(self, emlalock_user_id: str) -> bool:
        # The composite primary key makes this an atomic one-use-per-UTC-day claim,
        # including when two confirmation buttons are clicked at nearly the same time.
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO daily_random_add_usage (emlalock_user_id, usage_date) VALUES (?, ?)",
                (str(emlalock_user_id), self._utc_usage_date()),
            )
        return cursor.rowcount == 1

    def release_daily_random_add(self, emlalock_user_id: str):
        with self._connect() as db:
            db.execute(
                "DELETE FROM daily_random_add_usage WHERE emlalock_user_id=? AND usage_date=?",
                (str(emlalock_user_id), self._utc_usage_date()),
            )

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

    def save_friendlink_message(self, guild_id: int, channel_id: int, message_id: int,
                                session_id: str, wearer_emlalock_id: str):
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO friendlink_messages
                   (message_id, guild_id, channel_id, session_id, wearer_emlalock_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(message_id), str(guild_id), str(channel_id), str(session_id), str(wearer_emlalock_id)),
            )

    def list_ended_friendlink_messages(self, guild_id: int, active_session_ids: set[str],
                                      verified_wearer_ids: set[str]) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM friendlink_messages WHERE guild_id=?",
                (str(guild_id),),
            ).fetchall()
        return [
            dict(row) for row in rows
            if row["session_id"] not in active_session_ids
            and row["wearer_emlalock_id"] in verified_wearer_ids
        ]

    def delete_friendlink_message_record(self, message_id: int):
        with self._connect() as db:
            db.execute("DELETE FROM friendlink_messages WHERE message_id=?", (str(message_id),))


    def save_adoption_roles(self, guild_id: int, looking_for_keyholder: int, looking_for_wearer: int):
        with self._connect() as db:
            db.execute(
                """INSERT INTO adoption_roles
                   (guild_id, looking_for_keyholder_role_id, looking_for_wearer_role_id)
                   VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET
                   looking_for_keyholder_role_id=excluded.looking_for_keyholder_role_id,
                   looking_for_wearer_role_id=excluded.looking_for_wearer_role_id""",
                (str(guild_id), str(looking_for_keyholder), str(looking_for_wearer)),
            )

    def get_adoption_roles(self, guild_id: int):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM adoption_roles WHERE guild_id=?", (str(guild_id),)
            ).fetchone()
        return dict(row) if row else None

    def create_adoption_request(self, request_id: str, guild_id: int,
                                requester_id: int, target_id: int):
        with self._connect() as db:
            db.execute(
                """INSERT INTO adoption_requests
                   (request_id, guild_id, requester_id, target_id, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                (request_id, str(guild_id), str(requester_id), str(target_id)),
            )

    def get_adoption_request(self, request_id: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM adoption_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_adoption_request_status(self, request_id: str, status: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE adoption_requests SET status=?
                   WHERE request_id=? AND status='pending'""",
                (status, request_id),
            )
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

    def set_notification_channel(self, guild_id: int, channel_id: int | None):
        with self._connect() as db:
            db.execute(
                """INSERT INTO guild_settings(guild_id, notification_channel_id)
                   VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET
                   notification_channel_id=excluded.notification_channel_id""",
                (str(guild_id), str(channel_id) if channel_id is not None else None),
            )


    def set_guild_channel(self, guild_id: int, purpose: str, channel_id: int | None):
        if purpose not in {"commands", "sessions", "adoption", "friendlinks", "logs"}:
            raise ValueError("Unknown channel purpose")
        with self._connect() as db:
            db.execute("INSERT INTO guild_channels(guild_id,purpose,channel_id) VALUES(?,?,?) ON CONFLICT(guild_id,purpose) DO UPDATE SET channel_id=excluded.channel_id", (str(guild_id), purpose, str(channel_id) if channel_id is not None else None))

    def get_guild_channel(self, guild_id: int, purpose: str):
        with self._connect() as db:
            row = db.execute("SELECT channel_id FROM guild_channels WHERE guild_id=? AND purpose=?", (str(guild_id), purpose)).fetchone()
        return int(row["channel_id"]) if row and row["channel_id"] else None
