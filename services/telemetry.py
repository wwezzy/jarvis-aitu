"""Private usage ledger. No prompt, audio, response, credentials or URLs stored."""
import asyncio
import json
import logging
import math
import os
import time
from contextvars import ContextVar
from datetime import datetime, timedelta

from sqlalchemy import select

from config import get_settings
from database import engine
from database.time import aware
from database.v4_models import ProviderUsage

request_user = ContextVar("request_user", default=None)
last_call = {}
logger = logging.getLogger(__name__)


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def tokens(response, input_text=""):
    usage = _get(response, "usage") or _get(response, "usage_metadata")
    for input_key, output_key in [("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens"), ("prompt_token_count", "candidates_token_count")]:
        incoming, outgoing = _get(usage, input_key), _get(usage, output_key)
        if type(incoming) is int and incoming >= 0:
            return incoming, max(0, outgoing) if type(outgoing) is int else 0, "reported"
    text = _get(response, "output_text") or _get(response, "text") or ""
    if input_text and isinstance(text, str):
        return math.ceil(len(input_text) / 4), math.ceil(len(text) / 4), "estimated"
    return 0, 0, "unknown"


def price(provider, model, incoming, outgoing, source):
    if source == "unknown":
        return None
    try:
        rates = json.loads(os.getenv("AI_PRICE_JSON", "{}"))[f"{provider}:{model}"]
        values = [float(rates[k]) for k in ("input", "output")]
        if not all(math.isfinite(x) and x >= 0 for x in values):
            return None
        return (incoming * values[0] + outgoing * values[1]) / 1_000_000
    except (ValueError, KeyError, TypeError):
        return None


async def record(provider, model, capability, elapsed, response=None, error=None, input_text=""):
    incoming, outgoing, source = tokens(response, input_text) if response is not None else (0, 0, "unknown")
    entry = dict(provider=provider, model=model[:120], capability=capability, latency_ms=int(elapsed * 1000),
                 error_class=type(error).__name__ if error else None, input_tokens=incoming, output_tokens=outgoing,
                 usage_source=source, estimated_cost=price(provider, model, incoming, outgoing, source))
    last_call.update({k: entry[k] for k in ("provider", "model", "capability", "latency_ms", "error_class")})
    try:
        async with asyncio.timeout(2):
            async with engine.async_session_factory() as db:
                db.add(ProviderUsage(user_id=request_user.get() or get_settings().admin_id, **entry))
                await db.commit()
    except Exception as exc:
        logger.warning("Usage ledger unavailable error=%s", type(exc).__name__)


async def observe(provider, model, capability, operation, *, input_text=""):
    started = time.monotonic()
    try:
        response = await operation
    except asyncio.CancelledError as exc:
        await record(provider, model, capability, time.monotonic() - started, error=exc)
        raise
    except Exception as exc:
        await record(provider, model, capability, time.monotonic() - started, error=exc)
        raise
    await record(provider, model, capability, time.monotonic() - started, response=response, input_text=input_text)
    return response


async def daily_usage(user_id, now=None):
    now = aware(now or datetime.now(get_settings().timezone))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with engine.async_session_factory() as db:
        rows = list(await db.scalars(select(ProviderUsage).where(ProviderUsage.user_id == user_id,
            ProviderUsage.occurred_at >= start, ProviderUsage.occurred_at < start + timedelta(days=1))))
    groups = {}
    for row in rows:
        key = f"{row.provider}:{row.model}:{row.capability}"
        item = groups.setdefault(key, dict(requests=0, errors=0, input_tokens=0, output_tokens=0,
            estimated_tokens=0, unknown_usage_requests=0, cost_estimate=0.0, unpriced_requests=0))
        item["requests"] += 1
        item["errors"] += int(row.error_class is not None)
        item["input_tokens"] += row.input_tokens
        item["output_tokens"] += row.output_tokens
        item["estimated_tokens"] += row.input_tokens + row.output_tokens if row.usage_source == "estimated" else 0
        item["unknown_usage_requests"] += int(row.usage_source == "unknown")
        item["cost_estimate"] += row.estimated_cost or 0
        item["unpriced_requests"] += int(row.estimated_cost is None)
    total = sum(x["cost_estimate"] for x in groups.values())
    try:
        budget = float(os.getenv("AI_DAILY_BUDGET_USD", "0"))
    except ValueError:
        budget = 0
    return dict(date=start.date().isoformat(), groups=groups, requests=len(rows),
                configured_price_estimate_usd=total, unpriced_requests=sum(x["unpriced_requests"] for x in groups.values()),
                budget_usd=budget if math.isfinite(budget) and budget > 0 else None,
                budget_warning=bool(math.isfinite(budget) and budget > 0 and total >= budget * .8))


async def cost_text(user_id):
    data = await daily_usage(user_id)
    return (f"AI usage {data['date']} · запросов: {data['requests']}\n"
            f"Оценка по настроенным ценам: ${data['configured_price_estimate_usd']:.6f}\n"
            f"Без известной стоимости: {data['unpriced_requests']}\n"
            + ("⚠ Достигнуто 80% дневного бюджета.\n" if data['budget_warning'] else "")
            + json.dumps(data['groups'], ensure_ascii=False))
