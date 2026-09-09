from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

VALID_COMMANDS = {"lock", "sleep", "shutdown", "restart"}


def _message(cmd: str, user_id: int, issued_at: int, nonce: str) -> bytes:
    return f"{cmd}|{user_id}|{issued_at}|{nonce}".encode("utf-8")


def build_signed_command(cmd: str, user_id: int, secret: str) -> str:
    if cmd not in VALID_COMMANDS:
        raise ValueError("Unsupported PC command")
    if not secret:
        raise ValueError("PC_AGENT_SECRET is missing")
    issued_at = int(time.time())
    nonce = uuid.uuid4().hex
    signature = hmac.new(
        secret.encode("utf-8"),
        _message(cmd, user_id, issued_at, nonce),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps(
        {
            "cmd": cmd,
            "user_id": user_id,
            "issued_at": issued_at,
            "nonce": nonce,
            "sig": signature,
        },
        separators=(",", ":"),
    )


def verify_signed_command(payload: str, user_id: int, secret: str, max_age_seconds: int = 90) -> str | None:
    try:
        data = json.loads(payload)
        cmd = str(data["cmd"])
        payload_user_id = int(data["user_id"])
        issued_at = int(data["issued_at"])
        nonce = str(data["nonce"])
        received = str(data["sig"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None

    if cmd not in VALID_COMMANDS or payload_user_id != user_id or not secret:
        return None
    now = int(time.time())
    if issued_at > now + 30 or now - issued_at > max_age_seconds:
        return None

    expected = hmac.new(
        secret.encode("utf-8"),
        _message(cmd, payload_user_id, issued_at, nonce),
        hashlib.sha256,
    ).hexdigest()
    return cmd if hmac.compare_digest(expected, received) else None
