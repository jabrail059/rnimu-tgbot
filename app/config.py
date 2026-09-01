import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    public_base_url: str
    yoomoney_wallet: str
    yoomoney_notification_secret: str
    database_path: str


def get_settings() -> Settings:
    values = {
        "bot_token": os.getenv("BOT_TOKEN", ""),
        "public_base_url": os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        "yoomoney_wallet": os.getenv("YOOMONEY_WALLET", ""),
        "yoomoney_notification_secret": os.getenv("YOOMONEY_NOTIFICATION_SECRET", ""),
        "database_path": os.getenv("DATABASE_PATH", "data/app.db"),
    }
    missing = [name.upper() for name, value in values.items() if not value and name != "database_path"]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return Settings(**values)
