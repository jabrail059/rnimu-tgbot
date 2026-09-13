"""Verify ES256 DPoP proofs (RFC 9449 format); persistence prevents replay.

This is a proof-bound Telegram session API, not an OAuth authorization server.
No custom encryption or client-side hiding of decryption keys is used.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import HTTPException


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if base64url(data) != value:
        raise ValueError
    return data


def _object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


@dataclass(frozen=True)
class Proof:
    key_id: str
    jti: str


def verify_proof(encoded: str, method: str, target: str, now: float, *, token: str | None = None) -> Proof:
    try:
        if not encoded or len(encoded) > 2048:
            raise ValueError
        header_part, payload_part, signature_part = encoded.split(".")
        header = json.loads(_decode(header_part), object_pairs_hook=_object)
        payload = json.loads(_decode(payload_part), object_pairs_hook=_object)
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise ValueError
        if set(header) != {"typ", "alg", "jwk"} or header["typ"] != "dpop+jwt" or header["alg"] != "ES256":
            raise ValueError
        jwk = header["jwk"]
        if not isinstance(jwk, dict) or set(jwk) != {"kty", "crv", "x", "y"} or jwk["kty"] != "EC" or jwk["crv"] != "P-256":
            raise ValueError
        x, y = _decode(jwk["x"]), _decode(jwk["y"])
        if len(x) != 32 or len(y) != 32:
            raise ValueError
        issued = payload["iat"]
        if type(issued) is not int or abs(now - issued) > 60:
            raise ValueError
        if payload.get("htm") != method or payload.get("htu") != target:
            raise ValueError
        jti = payload["jti"]
        if not isinstance(jti, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", jti):
            raise ValueError
        if token is not None and payload.get("ath") != base64url(hashlib.sha256(token.encode()).digest()):
            raise ValueError
        signature = _decode(signature_part)
        if len(signature) != 64:
            raise ValueError
        public_key = ec.EllipticCurvePublicNumbers(int.from_bytes(x), int.from_bytes(y), ec.SECP256R1()).public_key()
        der = encode_dss_signature(int.from_bytes(signature[:32]), int.from_bytes(signature[32:]))
        public_key.verify(der, f"{header_part}.{payload_part}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
        return Proof(digest(json.dumps(jwk, sort_keys=True, separators=(",", ":"))), digest(jti))
    except (ValueError, TypeError, KeyError, InvalidSignature, UnicodeError, RecursionError) as exc:
        raise HTTPException(401, "Не удалось проверить сеанс. Откройте приложение заново из бота.") from exc
