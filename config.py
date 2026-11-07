from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    discord_token: str
    openai_api_key: str
    openai_model: str
    database_path: str
    credential_encryption_key: str
    emlalock_api_base_url: str
    log_level: str


def load_settings() -> Settings:
    return Settings(
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        database_path=os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
        credential_encryption_key=os.getenv("CREDENTIAL_ENCRYPTION_KEY", ""),
        emlalock_api_base_url=os.getenv("EMLALOCK_API_BASE_URL", "https://api.emlalock.com"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
