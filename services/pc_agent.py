from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
import uuid

VALID_COMMANDS = {"lock", "sleep", "shutdown", "restart", "status"}


def explicit_pc_command(text: str) -> str | None:
    """Only direct, whole-message requests can authorize a signed PC command."""
    aliases = {
        "заблокируй компьютер": "lock", "усыпи компьютер": "sleep",
        "выключи компьютер": "shutdown", "перезагрузи компьютер": "restart",
    }
    normalized = text.strip().lower().rstrip(".!?")
    for command in VALID_COMMANDS:
        if normalized in {f"/pc {command}", f"{command} pc"}:
            return command
    return aliases.get(normalized)


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
        cmd = data["cmd"]
        payload_user_id = data["user_id"]
        issued_at = data["issued_at"]
        nonce = data["nonce"]
        received = data["sig"]
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None

    if (not isinstance(cmd, str) or cmd not in VALID_COMMANDS
            or type(payload_user_id) is not int or payload_user_id != user_id
            or type(issued_at) is not int or not secret
            or not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce)
            or not isinstance(received, str) or not re.fullmatch(r"[a-f0-9]{64}", received)):
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


class ReplayGuard:
    """Persist consumed nonces before execution, including across process restarts."""

    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS consumed (nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL)")

    def claim(self, payload: str) -> bool:
        # Only pass a payload after successful signature verification.
        data = json.loads(payload)
        now = int(time.time())
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM consumed WHERE expires < ?", (now,))
            result = db.execute("INSERT OR IGNORE INTO consumed VALUES (?, ?)",
                                (data["nonce"], data["issued_at"] + 90))
            return result.rowcount == 1


def consume_command(redis, key: str, user_id: int, secret: str, guard: ReplayGuard, execute) -> bool:
    payload = redis.getdel(key)
    if not payload:
        return False
    command = verify_signed_command(payload, user_id, secret)
    if command is None or not guard.claim(payload):
        return False
    execute(command)
    return True


def signed_status(data, secret):
    body = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return json.dumps({"body": body, "sig": hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()})


def read_status(raw, user_id, secret):
    try:
        envelope = json.loads(raw)
        expected = hmac.new(secret.encode(), envelope["body"].encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, envelope["sig"]):
            return None
        data = json.loads(envelope["body"])
        if data["user_id"] != user_id or type(data["seen_at"]) is not int or data["seen_at"] > time.time() + 30:
            return None
        return data
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


async def pc_status(redis, user_id, secret):
    if redis is None or not secret:
        return {"online": False, "state": "not configured"}
    try:
        raw = await redis.get(f"jarvis:pc_status:{user_id}")
        data = read_status(raw, user_id, secret) if raw else None
        if data is None:
            return {"online": False, "state": "heartbeat unavailable"}
        return {**data, "online": time.time() - data["seen_at"] <= 45}
    except Exception as exc:
        return {"online": False, "state": "unavailable", "error": type(exc).__name__}


async def handle_pc_request(text, user_id, chat_id, redis, secret):
    """Call ONLY with original Telegram text, never transcripts/model output."""
    command = explicit_pc_command(text)
    confirm = re.fullmatch(r"/pc confirm ([a-f0-9]{16})", text.strip())
    if command is None and confirm is None:
        return None
    if redis is None or not secret:
        return "PC-agent unavailable: Redis/PC_AGENT_SECRET is not configured."
    if command == "status":
        return "PC: " + json.dumps(await pc_status(redis, user_id, secret), ensure_ascii=False)
    if confirm:
        raw = await redis.getdel(f"jarvis:pc_confirm:{user_id}:{chat_id}:{confirm[1]}")
        if raw not in {"shutdown", "restart"}:
            return "Подтверждение истекло или уже использовано. Повтори исходную команду."
        command = raw
    elif command in {"shutdown", "restart"}:
        token = uuid.uuid4().hex[:16]
        await redis.set(f"jarvis:pc_confirm:{user_id}:{chat_id}:{token}", command, ex=60)
        return f"Подтверди {command} в течение 60 секунд: /pc confirm {token}"
    payload = build_signed_command(command, user_id, secret)
    queued = await redis.set(f"jarvis:pc_command:{user_id}", payload, ex=90, nx=True)
    if queued is False or queued is None:
        return "Команда уже ожидает выполнения. Проверь /pc status; повтори после её обработки."
    return f"Signed PC command queued: {command}. Receipt: {json.loads(payload)['nonce']}. /pc status — ACK/result."
