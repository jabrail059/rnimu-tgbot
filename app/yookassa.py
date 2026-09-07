from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class YooKassaError(RuntimeError):
    pass


@dataclass(frozen=True)
class YooKassaPayment:
    id: str
    status: str
    confirmation_url: str | None
    amount: Decimal
    currency: str
    metadata: dict[str, str]


class YooKassaClient:
    """Async client for the official YooKassa v3 HTTP API."""

    def __init__(self, settings: Settings):
        self._auth = (settings.yookassa_shop_id, settings.yookassa_secret_key)
        self._return_url = settings.payment_return_url

    async def create_payment(self, order_id: str, user_id: int, amount: Decimal) -> YooKassaPayment:
        return await self._request("POST", "/payments", {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self._return_url},
            "description": "Подписка на материалы по патанатомии (30 дней)",
            "metadata": {"order_id": order_id, "user_id": str(user_id)},
        # order_id is persisted before the request and is below YooKassa's 64-char limit.
        # Reusing it makes a transport-level retry safe instead of creating another charge.
        }, order_id)

    async def get_payment(self, payment_id: str) -> YooKassaPayment:
        return await self._request("GET", f"/payments/{payment_id}")

    async def _request(self, method: str, path: str, payload: dict | None = None, idempotence_key: str | None = None) -> YooKassaPayment:
        headers = {"Idempotence-Key": idempotence_key} if idempotence_key else {}
        try:
            async with httpx.AsyncClient(base_url="https://api.yookassa.ru/v3", auth=self._auth, timeout=15.0) as client:
                response = await client.request(method, path, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("YooKassa API request failed: %s", exc)
            raise YooKassaError("YooKassa API is temporarily unavailable") from exc
        try:
            return YooKassaPayment(
                id=str(body["id"]), status=str(body["status"]),
                confirmation_url=body.get("confirmation", {}).get("confirmation_url"),
                amount=Decimal(str(body["amount"]["value"])), currency=str(body["amount"]["currency"]),
                metadata={str(key): str(value) for key, value in body.get("metadata", {}).items()},
            )
        except (KeyError, ValueError) as exc:
            raise YooKassaError("YooKassa returned an invalid payment response") from exc
