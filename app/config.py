from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    public_base_url: str
    database_path: str
    yookassa_shop_id: str
    yookassa_secret_key: str
    subscription_price: Decimal
    subscription_days: int
    proxy_url: str | None
    trust_proxy_headers: bool
    enable_legacy_yoomoney: bool
    yoomoney_wallet: str | None
    yoomoney_notification_secret: str | None

    @property
    def payment_return_url(self) -> str:
        return f"{self.public_base_url}/?payment=return"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_settings() -> Settings:
    public_base_url = _required("PUBLIC_BASE_URL").rstrip("/")
    parsed_url = urlparse(public_base_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise RuntimeError("PUBLIC_BASE_URL must be an absolute HTTPS URL")
    try:
        price = Decimal(os.getenv("SUBSCRIPTION_PRICE", "120.00")).quantize(Decimal("0.01"))
        days = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError("SUBSCRIPTION_PRICE or SUBSCRIPTION_DAYS is invalid") from exc
    if price <= 0 or days <= 0:
        raise RuntimeError("SUBSCRIPTION_PRICE and SUBSCRIPTION_DAYS must be positive")
    legacy_enabled = os.getenv("ENABLE_LEGACY_YOOMONEY", "false").lower() == "true"
    legacy_wallet = os.getenv("YOOMONEY_WALLET", "").strip() or None
    legacy_secret = os.getenv("YOOMONEY_NOTIFICATION_SECRET", "").strip() or None
    if legacy_enabled and (not legacy_wallet or not legacy_secret):
        raise RuntimeError("Legacy YooMoney requires YOOMONEY_WALLET and YOOMONEY_NOTIFICATION_SECRET")
    return Settings(
        bot_token=_required("BOT_TOKEN"), public_base_url=public_base_url,
        database_path=os.getenv("DATABASE_PATH", "data/app.db"),
        yookassa_shop_id=_required("YOOKASSA_SHOP_ID"),
        yookassa_secret_key=_required("YOOKASSA_SECRET_KEY"),
        subscription_price=price, subscription_days=days,
        proxy_url=os.getenv("PROXY_URL", "").strip() or None,
        trust_proxy_headers=os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true",
        enable_legacy_yoomoney=legacy_enabled, yoomoney_wallet=legacy_wallet,
        yoomoney_notification_secret=legacy_secret,
    )
