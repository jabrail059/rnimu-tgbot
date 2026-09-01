from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException


def telegram_user_id(init_data: str, bot_token: str) -> int:
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Telegram authorization data is missing")
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Invalid Telegram authorization data")
    try:
        if time.time() - int(fields["auth_date"]) > 3600:
            raise HTTPException(401, "Telegram authorization data expired")
        return int(json.loads(fields["user"])["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(401, "Invalid Telegram user data") from exc


def yoomoney_signature_is_valid(data: dict[str, str], secret: str) -> bool:
    signature = data.get("sign", "")
    payload = {key: value for key, value in data.items() if key != "sign"}
    from urllib.parse import urlencode
    message = urlencode(sorted(payload.items()))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
