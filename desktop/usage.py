"""Local model accounting without prompts, answers, credentials or prices.

Only daily/model aggregates and provider-reported counts are persisted. Missing
counts remain explicitly unknown; this ledger never estimates tokens or money,
calls a model, or records user content. All dates use the owner's UTC+5 day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re
import time


_ZONE = timezone(timedelta(hours=5))
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")
_MAX_TOKENS = 1_000_000_000
_MAX_LATENCY_MS = 3_600_000


class UsageLedger:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock if clock is not None else time.time

    def _day(self, now):
        value = self.clock() if now is None else now
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("An aware accounting timestamp is required")
            return value.astimezone(_ZONE).date().isoformat()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("A finite accounting timestamp is required")
        return datetime.fromtimestamp(value, _ZONE).date().isoformat()

    @staticmethod
    def _tokens(value):
        return value if type(value) is int and 0 <= value <= _MAX_TOKENS else None

    @staticmethod
    def _latency(value):
        if (type(value) in {int, float} and math.isfinite(value)
                and 0 <= value <= _MAX_LATENCY_MS):
            return float(value)
        return None

    def record(self, model, usage=None, *, success=True, now=None):
        """Record one attempted model response, with validated numeric metadata.

        Returns False for invalid names/outcome flags. Extra usage keys are
        ignored, never serialized; neither failure messages nor content belong
        here. The caller decides success after validating the final response.
        """
        if not isinstance(model, str) or not _MODEL.fullmatch(model) or type(success) is not bool:
            return False
        day = self._day(now)
        data = usage if isinstance(usage, dict) else {}
        inputs = self._tokens(data.get("input_tokens"))
        outputs = self._tokens(data.get("output_tokens"))
        latency = self._latency(data.get("elapsed_ms"))
        values = (day, model, 1, int(success), int(not success), inputs or 0, outputs or 0,
                  int(inputs is not None), int(outputs is not None),
                  int(inputs is None or outputs is None), latency or 0, int(latency is not None))
        with self.store.connection() as db:
            result = db.execute("""INSERT INTO local_ai_usage(
                on_date,model,requests,successful,failed,input_tokens,output_tokens,
                input_samples,output_samples,unknown_requests,latency_total_ms,latency_samples)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(on_date,model) DO UPDATE SET
                    requests=requests+1, successful=successful+excluded.successful,
                    failed=failed+excluded.failed,
                    input_tokens=input_tokens+excluded.input_tokens,
                    output_tokens=output_tokens+excluded.output_tokens,
                    input_samples=input_samples+excluded.input_samples,
                    output_samples=output_samples+excluded.output_samples,
                    unknown_requests=unknown_requests+excluded.unknown_requests,
                    latency_total_ms=latency_total_ms+excluded.latency_total_ms,
                    latency_samples=latency_samples+excluded.latency_samples
                WHERE requests<1000000000""", values)
        return result.rowcount == 1

    def snapshot(self, now=None):
        day = self._day(now)
        with self.store.connection() as db:
            row = db.execute("""SELECT coalesce(sum(requests),0) AS requests,
                coalesce(sum(successful),0) AS successful,coalesce(sum(failed),0) AS failed,
                coalesce(sum(input_tokens),0) AS known_input_tokens,
                coalesce(sum(output_tokens),0) AS known_output_tokens,
                coalesce(sum(input_samples),0) AS known_input_requests,
                coalesce(sum(output_samples),0) AS known_output_requests,
                coalesce(sum(unknown_requests),0) AS unknown_usage_requests,
                coalesce(sum(latency_total_ms),0) AS latency_total_ms,
                coalesce(sum(latency_samples),0) AS latency_samples
                FROM local_ai_usage WHERE on_date=?""", (day,)).fetchone()
        result = dict(row)
        total_latency = result.pop("latency_total_ms")
        samples = result["latency_samples"]
        result["mean_latency_ms"] = round(total_latency / samples, 2) if samples else None
        return {"date": day, "timezone": "UTC+5", **result}
