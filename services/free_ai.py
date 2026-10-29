"""Telegram's explicit free-only Gemini path; no paid clients or key rotation.

The existing PostgreSQL ProviderUsage ledger reserves each request BEFORE any
provider request. Unknown outcomes still count. A free Render Key Value cache
is not a durable budget: this path never uses Redis as its allowance ledger.
The small local SQLite character limit is an additional per-instance guard;
the durable 40/day request count survives server restarts and redeployments.
An operator attestation is not a guarantee against future billing changes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from sqlalchemy import func, select, text, update

from database import engine
from database.time import aware
from database.v4_models import ProviderUsage
from desktop import gemini_ai as shared


logger = logging.getLogger(__name__)
PROVIDER = "gemini-free"
CAPABILITY = "free_chat_reserved"
MODEL = "gemini-3.5-flash-lite"
DAILY_REQUEST_LIMIT = 40
QUOTA_COOLDOWN_SECONDS = 900
_ZONE = timezone(timedelta(hours=5))
_LOCK_ID = 741104008
_SLOTS = threading.BoundedSemaphore(2)
_REASONS = frozenset({"not_configured", "free_project_unverified", "free_project_expired", "quota_exhausted",
                      "budget_exceeded", "budget_unavailable", "authentication", "access_denied", "timeout",
                      "provider_unavailable", "input_limit", "busy", "canceled"})


class FreeAIUnavailable(RuntimeError):
    def __init__(self, reason):
        self.reason = reason if type(reason) is str and reason in _REASONS else "provider_unavailable"
        super().__init__(self.reason)


def _attestation(settings):
    key = getattr(settings, "gemini_api_key", None)
    raw = getattr(settings, "gemini_free_project_attestation", None)
    if not settings.llm_free_only or not isinstance(key, str) or not re.fullmatch(
            r"(?:[A-Za-z0-9_-]{20,200}|AQ\.[A-Za-z0-9_-]{20,197})", key):
        raise FreeAIUnavailable("not_configured")
    try:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 4096:
            raise ValueError
        value = shared._json(raw)
        if (not isinstance(value, dict) or set(value) != {"version", "project_id", "key_sha256", "billing_enabled", "verified_at", "expires_at"}
                or type(value["version"]) is not int or value["version"] != 1 or value["billing_enabled"] is not False
                or not isinstance(value["project_id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", value["project_id"])
                or value["key_sha256"] != hashlib.sha256(key.encode("ascii")).hexdigest()):
            raise ValueError
        verified, expires = shared._utc(value["verified_at"]), shared._utc(value["expires_at"])
        if not 0 < expires - verified <= 30 * 86400 or verified > time.time() + 60:
            raise ValueError
        if expires <= time.time():
            raise FreeAIUnavailable("free_project_expired")
        return key, json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except FreeAIUnavailable:
        raise
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        raise FreeAIUnavailable("free_project_unverified") from None


def _client(settings):
    key, raw = _attestation(settings)
    configured = getattr(settings, "gemini_free_state_dir", None)
    directory = Path(configured) if configured else Path(tempfile.gettempdir()) / "jarvis-server-free-ai"
    temporary = None
    try:
        directory = shared._directory(directory, create=True)
        target = directory / shared.MARKER_NAME
        same = target.exists() and shared._read_private(target, 4096) == raw
        if not same:
            descriptor, temporary = tempfile.mkstemp(prefix=".gemini-attestation-", dir=directory)
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            shared._directory(directory)
            if target.exists() or target.is_symlink():
                shared._regular(target, 4096)
            os.replace(temporary, target)
            temporary = None
    except (OSError, shared.GeminiUnavailable):
        raise FreeAIUnavailable("budget_unavailable") from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
    environ = {"JARVIS_GEMINI_ENABLED": "1", "GEMINI_API_KEY": key, "JARVIS_GEMINI_MODEL": MODEL,
               "JARVIS_GEMINI_HISTORY_ENABLED": "1" if settings.llm_free_history_enabled else "0",
               "JARVIS_GEMINI_PERSONALITY_CONTEXT_ENABLED": "1" if settings.llm_free_context_enabled else "0"}
    return shared.GeminiChat(directory, environ=environ)


async def _reserve(settings, now=None):
    now = aware(now or datetime.now(timezone.utc))
    start = now.astimezone(_ZONE).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    try:
        async with asyncio.timeout(3):
            async with engine.async_session_factory() as session:
                dialect = session.bind.dialect.name
                if dialect == "postgresql":
                    await session.execute(text("SET LOCAL lock_timeout = '2000ms'"))
                    await session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": _LOCK_ID})
                elif dialect == "sqlite":
                    await session.execute(text("BEGIN IMMEDIATE"))
                else:
                    raise FreeAIUnavailable("budget_unavailable")
                base = (ProviderUsage.provider == PROVIDER, ProviderUsage.capability == CAPABILITY)
                last_quota = await session.scalar(select(func.max(ProviderUsage.occurred_at)).where(
                    *base, ProviderUsage.error_class == "QuotaExhausted"))
                if last_quota is not None and (now - aware(last_quota)).total_seconds() < QUOTA_COOLDOWN_SECONDS:
                    raise FreeAIUnavailable("quota_exhausted")
                count = await session.scalar(select(func.count(ProviderUsage.id)).where(
                    *base, ProviderUsage.occurred_at >= start, ProviderUsage.occurred_at < start + timedelta(days=1)))
                if count >= DAILY_REQUEST_LIMIT:
                    raise FreeAIUnavailable("budget_exceeded")
                row = ProviderUsage(user_id=settings.admin_id, occurred_at=now, provider=PROVIDER, model=MODEL,
                                    capability=CAPABILITY, input_tokens=0, output_tokens=0, estimated_cost=None,
                                    latency_ms=0, error_class="Reserved", usage_source="unknown")
                session.add(row)
                await session.flush()
                identity = row.id
                await session.commit()
                return identity
    except asyncio.CancelledError:
        raise
    except FreeAIUnavailable:
        raise
    except Exception:
        raise FreeAIUnavailable("budget_unavailable") from None


async def _settle(identity, *, reason=None, usage=None, elapsed=0):
    fixed = {"quota_exhausted": "QuotaExhausted", "authentication": "Authentication", "access_denied": "AccessDenied",
             "timeout": "Timeout", "canceled": "Canceled"}.get(reason, "Unavailable" if reason else None)
    values = {"error_class": fixed, "latency_ms": min(3600000, max(0, round(elapsed * 1000)))}
    if isinstance(usage, dict) and all(type(usage.get(key)) is int and 0 <= usage[key] <= 10**9 for key in ("input_tokens", "output_tokens")):
        values.update(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"], usage_source="reported")
    try:
        async with asyncio.timeout(2):
            async with engine.async_session_factory() as session:
                await session.execute(update(ProviderUsage).where(ProviderUsage.id == identity,
                    ProviderUsage.provider == PROVIDER, ProviderUsage.capability == CAPABILITY).values(**values))
                await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        # The durable upfront row remains a counted unknown outcome.
        logger.warning("Free-only usage settlement unavailable")


def _history(items):
    from desktop.chat import bounded_history
    from desktop.personal import safe_context_text
    if not isinstance(items, list):
        return []
    pairs, pending = [], None
    for item in items[-20:]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip() or "\x00" in item["text"]:
            pending = None
            continue
        if item.get("role") == "user":
            pending = {"role": "user", "content": safe_context_text(item["text"], 2000)}
        elif item.get("role") == "assistant" and pending is not None:
            pairs.extend([pending, {"role": "assistant", "content": safe_context_text(item["text"], 2000)}])
            pending = None
        else:
            pending = None
    return bounded_history(pairs)


def _context(settings, preferences, memory_context):
    if not settings.llm_free_context_enabled:
        return None
    if any(value is not None and (not isinstance(value, str) or "\x00" in value) for value in (preferences, memory_context)):
        raise FreeAIUnavailable("input_limit")
    from desktop.personal import safe_context_text
    # Caller-provided previews only. Never read profile.local.md or other
    # files, and never promote historical memory to a current verified plan.
    value = {"authority": "data_only", "source": "Telegram database preview; historical facts may be outdated",
             "preferences_preview": safe_context_text(preferences, 600), "memory_preview": safe_context_text(memory_context, 1300),
             "projection_truncated": len(preferences or "") > 600 or len(memory_context or "") > 1300}
    while True:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= 2500:
            return serialized
        field = "memory_preview" if value["memory_preview"] else "preferences_preview"
        value[field] = value[field][:-min(256, len(value[field]))]
        value["projection_truncated"] = True


async def reply(*, text, settings, redis=None, history=None, preferences=None, memory_context=""):
    del redis  # An ephemeral cache is never the durable free allowance.
    if not isinstance(text, str) or not text.strip() or len(text) > 8000 or "\x00" in text:
        raise FreeAIUnavailable("input_limit")
    slot = _SLOTS
    if not slot.acquire(blocking=False):
        raise FreeAIUnavailable("busy")
    worker_started, client, identity = False, None, None
    canceled, started = threading.Event(), time.monotonic()
    try:
        client = _client(settings)
        if not client.prepare():
            raise FreeAIUnavailable(client.reason)
        context = _context(settings, preferences, memory_context)
        projected_history = _history(history) if settings.llm_free_history_enabled else None
        identity = await _reserve(settings)
        def work():
            try:
                if canceled.is_set():
                    raise shared.GeminiCancelled("canceled")
                return client.answer(text.strip(), history=projected_history, personality_context=context, cancel_event=canceled)
            finally:
                client.close()
                slot.release()
        worker = asyncio.create_task(asyncio.to_thread(work))
        worker_started = True
        # Shield preserves ownership of the worker so cancellation cannot
        # cancel a queued to_thread job before its finally releases the slot.
        worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            canceled.set()
            client.cancel()
            await _settle(identity, reason="canceled", elapsed=time.monotonic() - started)
            raise
        except shared.GeminiUnavailable as error:
            reason = FreeAIUnavailable(error.reason).reason
            await _settle(identity, reason=reason, elapsed=time.monotonic() - started)
            raise FreeAIUnavailable(reason) from None
        await _settle(identity, usage=client.last_usage, elapsed=time.monotonic() - started)
        return result, MODEL
    finally:
        if not worker_started:
            if client is not None:
                client.close()
            slot.release()
