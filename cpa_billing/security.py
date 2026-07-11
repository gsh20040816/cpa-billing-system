from __future__ import annotations

import hashlib
import hmac
import secrets


def cpamp_key_hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.strip().encode()).hexdigest()


def login_fingerprint(raw_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), raw_key.strip().encode(), hashlib.sha256).hexdigest()


def hash_token(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def secure_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def generate_api_key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_urlsafe(32).rstrip('=')}"


def mask_api_key(raw_key: str) -> str:
    value = raw_key.strip()
    if len(value) <= 12:
        return "****"
    return f"{value[:8]}...{value[-4:]}"


def mask_hash(value: str) -> str:
    return f"key:{value[:8]}...{value[-4:]}" if len(value) >= 12 else "key:****"


def constant_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())

