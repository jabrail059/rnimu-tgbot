"""Test-only browser client: no legacy-auth fallback exists in the application."""
import asyncio
import hashlib
import json
import re
import secrets
import time
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from app.proofs import base64url

_keys = {}


class Device:
    def __init__(self, key=None, origin="https://example.com"):
        self.key = key or ec.generate_private_key(ec.SECP256R1())
        self.origin = origin
        numbers = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": base64url(numbers.x.to_bytes(32, "big")), "y": base64url(numbers.y.to_bytes(32, "big"))}
        self.token = None
        self.views = {}

    def proof(self, method, path, *, token=None, iat=None, jti=None, claims=None, header=None):
        payload = {"htm": method.upper(), "htu": self.origin + urlsplit(path).path,
                   "iat": int(time.time()) if iat is None else iat, "jti": jti or secrets.token_urlsafe(24)}
        if token:
            payload["ath"] = base64url(hashlib.sha256(token.encode()).digest())
        payload.update(claims or {})
        fields = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.jwk}
        fields.update(header or {})
        data = ".".join(base64url(json.dumps(part, separators=(",", ":")).encode()) for part in (fields, payload))
        r, s = decode_dss_signature(self.key.sign(data.encode(), ec.ECDSA(hashes.SHA256())))
        return data + "." + base64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def headers(self, method, path, **kwargs):
        return {"Authorization": f"DPoP {self.token}", "DPoP": self.proof(method, path, token=self.token, **kwargs)}


class SessionClient(TestClient):
    def __init__(self, app, *, database, **kwargs):
        super().__init__(app, **kwargs)
        self.database = database
        self.devices = {}
        self.credentials = {}

    def login(self, credentials):
        fields = dict(parse_qsl(credentials))
        try:
            uid = json.loads(fields.get("user", "{}"))["id"]
        except (ValueError, KeyError, TypeError):
            uid = 0
        key = _keys.setdefault(uid, ec.generate_private_key(ec.SECP256R1()))
        device = self.devices.setdefault(uid, Device(key))
        response = super().request("POST", "/api/session", json={"init_data": credentials},
                                   headers={"DPoP": device.proof("POST", "/api/session")})
        if response.status_code == 200:
            device.token = response.json()["access_token"]
            device.views = {}
            self.credentials[credentials] = device
        return response, device

    def request(self, method, url, **kwargs):
        path = str(url)
        headers = dict(kwargs.get("headers") or {})
        authorization = headers.get("Authorization", "")
        if path == "/api/session" and method.upper() == "POST" and "DPoP" not in headers:
            return self.login(kwargs.get("json", {}).get("init_data", ""))[0]
        if not authorization.lower().startswith("tma "):
            return super().request(method, url, **kwargs)
        credentials = authorization[4:]
        device = self.credentials.get(credentials)
        if device is None:
            response, device = self.login(credentials)
            if response.status_code != 200:
                return response
        resource = re.fullmatch(r"/api/images/(\d+)", path)
        document = re.fullmatch(r"/api/documents/(\d+)/pages/(-?\d+)", path)
        extra = {}
        if method.upper() == "GET" and (resource or document):
            kind = "image" if resource else "document"
            identifier = int((resource or document)[1])
            page = 1 if resource else int(document[2])
            record = asyncio.run(self.database.image(identifier) if resource else self.database.document(identifier))
            if record and (resource or 1 <= page <= len(record["page_sizes"])):
                material_id = record["material_id"]
                # One current material, just like a browser reader.
                if material_id not in device.views:
                    material_path = f"/api/materials/{material_id}"
                    response = super().request("GET", material_path, headers=device.headers("GET", material_path))
                    if response.status_code != 200:
                        return response
                    device.views = {material_id: response.json()["view_token"]}
                view = device.views[material_id]
                response = super().request("POST", "/api/reader/ticket", headers=device.headers("POST", "/api/reader/ticket"),
                                           json={"view": view, "kind": kind, "resource_id": identifier, "page": page})
                if response.status_code != 200:
                    return response
                extra = {"X-Read-View": view, "X-Page-Ticket": response.json()["ticket"]}
        kwargs["headers"] = {**headers, **device.headers(method, path), **extra}
        response = super().request(method, url, **kwargs)
        if method.upper() == "GET" and re.fullmatch(r"/api/materials/\d+", path) and response.status_code == 200:
            data = response.json()
            device.views = {data["id"]: data["view_token"]}
        return response
