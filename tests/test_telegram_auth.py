import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from services.telegram_auth import TelegramAuthError, validate_init_data


def build_init_data(bot_token: str, user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def test_valid_init_data():
    token = "123456:TEST_TOKEN"
    result = validate_init_data(build_init_data(token, 42), token, max_age_seconds=60)
    assert result.id == 42
    assert result.first_name == "Test"


def test_invalid_init_data_hash():
    token = "123456:TEST_TOKEN"
    raw = build_init_data(token, 42).replace("test-query", "tampered")
    try:
        validate_init_data(raw, token, max_age_seconds=60)
    except TelegramAuthError:
        pass
    else:
        raise AssertionError("tampered initData must fail")
