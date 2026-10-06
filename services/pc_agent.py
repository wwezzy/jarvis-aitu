from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
import uuid
import asyncio
from contextlib import closing

VALID_COMMANDS = {"lock", "sleep", "hibernate", "shutdown", "restart", "status", "cancel_shutdown"}


def explicit_pc_command(text: str) -> str | None:
    """Only direct, whole-message requests can authorize a signed PC command."""
    aliases = {
        "заблокируй компьютер": "lock", "усыпи компьютер": "sleep", "гибернация компьютера": "hibernate",
        "отправь компьютер в гибернацию": "hibernate",
        "выключи компьютер": "shutdown", "перезагрузи компьютер": "restart",
    }
    normalized = text.strip().lower().rstrip(".!?")
    normalized = re.sub(r"^(?:джарвис|jarvis)[, ]+", "", normalized)
    normalized = re.sub(r"\b(?:ноутбук|ноут|пк|комп)\b", "компьютер", normalized)
    aliases.update({"выключить компьютер": "shutdown", "перезагрузить компьютер": "restart",
                    "выключить": "shutdown", "выключи": "shutdown",
                    "отмени выключение": "cancel_shutdown", "отмени перезагрузку": "cancel_shutdown",
                    "статус компьютера": "status"})
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
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS consumed (nonce TEXT PRIMARY KEY, expires INTEGER NOT NULL)")

    def claim(self, payload: str) -> bool:
        # Only pass a payload after successful signature verification.
        data = json.loads(payload)
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as db, db:
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
        if not secret or not isinstance(raw, str) or len(raw) > 50000:
            return None
        envelope = json.loads(raw)
        expected = hmac.new(secret.encode(), envelope["body"].encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, envelope["sig"]):
            return None
        data = json.loads(envelope["body"])
        if type(data["user_id"]) is not int or data["user_id"] != user_id or type(data["seen_at"]) is not int or data["seen_at"] > time.time() + 30:
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
        from services.redis_backend import safe_transport_reason
        return {"online": False, "state": "unavailable", "error": type(exc).__name__, "reason": safe_transport_reason(exc)}


def offline_help(status):
    reason = status.get("reason")
    if reason == "ip_not_allowed":
        return "Redis отклоняет IP ноутбука. Проверь Networking → IP allow list нужного экземпляра и внешний TLS REDIS_URL. agent.py --doctor покажет причину."
    if reason == "authentication_failed":
        return "Redis отклонил учётные данные. Проверь существующую локальную конфигурацию и выбранный экземпляр; не отправляй секреты в чат."
    return "Ноутбук не подтвердил связь: нет свежего подписанного heartbeat. Проверь agent.py --doctor, автозапуск, сеть и одинаковые ADMIN_ID/PC_AGENT_SECRET/Redis."


async def wait_receipt(redis, user_id, secret, nonce, seconds=6):
    end = asyncio.get_running_loop().time() + seconds
    accepted = None
    while True:
        raw = await redis.get(f"jarvis:pc_result:{user_id}:{nonce}")
        data = read_status(raw, user_id, secret) if raw else None
        result = data.get("last_result") if data else None
        if isinstance(result, dict) and result.get("nonce") == nonce:
            accepted = result
            if result.get("state") in {"completed", "scheduled", "failed"}:
                return result
        if asyncio.get_running_loop().time() >= end:
            return accepted
        await asyncio.sleep(0.25)


async def handle_pc_request(text, user_id, chat_id, redis, secret, *, wait_seconds=6):
    """Call ONLY with original Telegram text, never transcripts/model output."""
    command = explicit_pc_command(text)
    if command == "sleep":
        return "Sleep отключён на этом ПК. Используй /pc hibernate (поддержка зависит от настроек Windows)."
    if text.strip().lower() == "/pc diagnose":
        status = await pc_status(redis, user_id, secret)
        return "Подписанная связь с ноутбуком работает." if status["online"] else offline_help(status)
    confirm = re.fullmatch(r"/pc confirm ([a-f0-9]{16})", text.strip())
    if command is None and confirm is None:
        return "/pc status · diagnose · lock · hibernate · shutdown · restart · cancel_shutdown. Выключение/перезапуск требуют отдельного подтверждения." if text.strip().lower().startswith("/pc") else None
    if redis is None or not secret:
        return "PC-agent unavailable: Redis/PC_AGENT_SECRET is not configured."
    if command == "status":
        return "PC: " + json.dumps(await pc_status(redis, user_id, secret), ensure_ascii=False)
    try:
        if confirm:
            raw = await redis.getdel(f"jarvis:pc_confirm:{user_id}:{chat_id}:{confirm[1]}")
            if raw not in {"shutdown", "restart"}:
                return "Подтверждение истекло или уже использовано. Повтори исходную команду."
            command = raw
        status = await pc_status(redis, user_id, secret)
        if not status["online"]:
            return "Команда не отправлена. " + offline_help(status)
        if not confirm and command in {"shutdown", "restart"}:
            token = uuid.uuid4().hex[:16]
            await redis.set(f"jarvis:pc_confirm:{user_id}:{chat_id}:{token}", command, ex=60)
            return f"Ноутбук на связи. Подтверди {command} в течение 60 секунд: /pc confirm {token}. Windows получит 30 секунд для отмены: /pc cancel_shutdown."
        payload = build_signed_command(command, user_id, secret)
        queued = await redis.set(f"jarvis:pc_command:{user_id}", payload, ex=20, nx=True)
        if queued is False or queued is None:
            return "Команда уже ожидает выполнения. Проверь /pc status; повтори после её обработки."
        nonce = json.loads(payload)["nonce"]
        result = await wait_receipt(redis, user_id, secret, nonce, wait_seconds)
        if not result:
            return f"Команда queued: {command}; подтверждения от агента пока нет. Выполнение НЕ подтверждено. Ожидание максимум 20 секунд; /pc status. Receipt: {nonce}"
        if result["state"] == "failed":
            return f"Агент получил команду, Windows вернул ошибку: {result.get('error', 'unknown')}. /pc diagnose и локальный agent.py --doctor. Повтор автоматически не выполняется."
        if result["state"] == "scheduled":
            return f"Windows подтвердил планирование {command}. Это ещё не подтверждение выключенного питания. /pc cancel_shutdown — отменить shutdown/restart в течение 30 секунд. Receipt: {nonce}"
        if result["state"] == "completed":
            return f"Агент подтвердил выполнение {command}. Receipt: {nonce}"
        return f"Агент принял {command}, результат ещё ожидается. /pc status. Receipt: {nonce}"
    except Exception as exc:
        from services.redis_backend import safe_transport_reason
        return "Результат команды не подтверждён; автоматического повтора нет. " + offline_help({"reason": safe_transport_reason(exc)})
