from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class TelegramAuthError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramWebAppUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str | None = None
    language_code: str | None = None


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> TelegramWebAppUser:
    if not init_data:
        raise TelegramAuthError("Missing Telegram initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise TelegramAuthError("initData hash is missing")

    # Bot-token validation excludes only `hash`; any `signature` field remains part of the data-check string.
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramAuthError("Invalid Telegram initData signature")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise TelegramAuthError("Invalid auth_date") from exc

    if auth_date <= 0:
        raise TelegramAuthError("auth_date is missing")
    now = int(time.time())
    if auth_date > now + 30:
        raise TelegramAuthError("Telegram initData auth_date is in the future")
    if max_age_seconds > 0 and now - auth_date > max_age_seconds:
        raise TelegramAuthError("Telegram initData is too old")

    raw_user = pairs.get("user")
    if not raw_user:
        raise TelegramAuthError("Telegram user is missing")
    try:
        user_data = json.loads(raw_user)
        user_id = int(user_data["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("Invalid Telegram user payload") from exc

    return TelegramWebAppUser(
        id=user_id,
        first_name=str(user_data.get("first_name") or ""),
        last_name=str(user_data.get("last_name") or ""),
        username=user_data.get("username"),
        language_code=user_data.get("language_code"),
    )
