"""Local numeric accounting only; no provider, network or private profile."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math
import sqlite3

import pytest

from desktop.store import LocalStore
from desktop.usage import UsageLedger


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone(timedelta(hours=5)))
MODEL = "qwen3:4b-instruct-2507-q4_K_M"


def ledger(tmp_path):
    store = LocalStore(tmp_path)
    return UsageLedger(store, clock=lambda: NOW), store


def test_empty_snapshot_is_local_day_without_fabricated_token_or_latency_data(tmp_path):
    value, _store = ledger(tmp_path)
    data = value.snapshot()
    assert data["date"] == "2026-10-29" and data["timezone"] == "UTC+5"
    assert data["requests"] == data["successful"] == data["failed"] == 0
    assert data["known_input_requests"] == data["known_output_requests"] == 0
    assert data["mean_latency_ms"] is None


def test_known_counts_success_failure_and_latency_are_aggregated_without_content(tmp_path):
    value, store = ledger(tmp_path)
    assert value.record(MODEL, {"input_tokens": 100, "output_tokens": 50, "elapsed_ms": 200})
    assert value.record("other:model", {"input_tokens": 20, "output_tokens": 0, "elapsed_ms": 400}, success=False)
    data = value.snapshot()
    assert data["requests"] == 2 and data["successful"] == data["failed"] == 1
    assert data["known_input_tokens"] == 120 and data["known_output_tokens"] == 50
    assert data["unknown_usage_requests"] == 0
    assert data["known_input_requests"] == data["known_output_requests"] == data["latency_samples"] == 2
    assert data["mean_latency_ms"] == 300
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM local_ai_usage").fetchone()[0] == 2


def test_unavailable_counts_are_unknown_and_partial_metadata_preserves_known_counts(tmp_path):
    value, _store = ledger(tmp_path)
    value.record(MODEL)
    value.record(MODEL, {"input_tokens": 0, "elapsed_ms": 0})
    value.record(MODEL, {"output_tokens": 17})
    data = value.snapshot()
    assert data["requests"] == data["unknown_usage_requests"] == 3
    assert data["known_input_tokens"] == 0 and data["known_output_tokens"] == 17
    assert data["known_input_requests"] == data["known_output_requests"] == 1
    assert data["mean_latency_ms"] == 0 and data["latency_samples"] == 1


@pytest.mark.parametrize("bad", [-1, True, 1.5, "5", math.nan, math.inf, 10**50, None])
def test_malformed_token_counts_are_never_estimated_or_counted_as_known(tmp_path, bad):
    value, _store = ledger(tmp_path)
    value.record(MODEL, {"input_tokens": bad, "output_tokens": 2})
    data = value.snapshot()
    assert data["known_input_requests"] == data["known_input_tokens"] == 0
    assert data["known_output_tokens"] == 2 and data["unknown_usage_requests"] == 1


@pytest.mark.parametrize("bad", [-1, True, "50", math.nan, math.inf, 10**30])
def test_malformed_latency_does_not_invent_a_mean(tmp_path, bad):
    value, _store = ledger(tmp_path)
    value.record(MODEL, {"input_tokens": 0, "output_tokens": 0, "elapsed_ms": bad})
    data = value.snapshot()
    assert data["mean_latency_ms"] is None and data["latency_samples"] == 0
    assert data["unknown_usage_requests"] == 0  # Valid reported zero tokens stay known.


def test_non_mapping_usage_is_an_unknown_attempt_without_error_content(tmp_path):
    value, _store = ledger(tmp_path)
    value.record(MODEL, "private provider error", success=False)
    data = value.snapshot()
    assert data["requests"] == data["failed"] == data["unknown_usage_requests"] == 1
    assert data["known_input_requests"] == data["known_output_requests"] == 0


def test_utc_plus_five_midnight_separates_days_and_accepts_aware_or_epoch_clock(tmp_path):
    value, _store = ledger(tmp_path)
    before = datetime(2026, 10, 29, 18, 59, 59, tzinfo=timezone.utc)
    after = before + timedelta(seconds=2)
    value.record(MODEL, {"input_tokens": 8, "output_tokens": 4}, now=before)
    value.record(MODEL, {"input_tokens": 30, "output_tokens": 10}, now=after.timestamp())
    assert value.snapshot(now=before)["date"] == "2026-10-29"
    assert value.snapshot(now=before)["known_input_tokens"] == 8
    assert value.snapshot(now=after)["date"] == "2026-10-30"
    assert value.snapshot(now=after)["known_input_tokens"] == 30


def test_persistence_reopen_and_additive_migration_preserve_other_data(tmp_path):
    path = tmp_path / "desktop.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY,text TEXT NOT NULL,created_at REAL NOT NULL)")
        db.execute("INSERT INTO notes VALUES(1,'existing note',1)")
        db.execute("PRAGMA user_version=3")
    value, store = ledger(tmp_path)
    value.record(MODEL, {"input_tokens": 7, "output_tokens": 3, "elapsed_ms": 55})
    reopened = LocalStore(tmp_path)
    assert UsageLedger(reopened, clock=lambda: NOW).snapshot() == value.snapshot()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT text FROM notes WHERE id=1").fetchone()[0] == "existing note"


def test_only_numeric_aggregates_and_bounded_model_name_are_persisted(tmp_path):
    value, store = ledger(tmp_path)
    private = "NEVER_STORE_PRIVATE_PROMPT_RESPONSE_TOKEN"
    value.record(MODEL, {"input_tokens": 1, "output_tokens": 2, "elapsed_ms": 3,
                         "prompt": private, "response": private, "api_key": private})
    with store.connection() as db:
        dump = "\n".join(db.iterdump())
        names = [row[1] for row in db.execute("PRAGMA table_info(local_ai_usage)")]
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 0
    assert private not in dump and MODEL in dump
    assert not {"prompt", "response", "api_key", "error", "request_text"}.intersection(names)


@pytest.mark.parametrize("name", [None, "", "model\nprivate", "x" * 101, "model'; DROP TABLE notes;--", "модель"])
def test_invalid_model_names_cannot_write_content_or_sql(tmp_path, name):
    value, _store = ledger(tmp_path)
    assert not value.record(name)
    assert value.snapshot()["requests"] == 0


def test_invalid_outcome_and_timestamps_do_not_create_an_attempt(tmp_path):
    value, _store = ledger(tmp_path)
    assert not value.record(MODEL, success="yes")
    for stamp in (True, math.nan, math.inf, datetime(2026, 10, 29)):
        with pytest.raises(ValueError):
            value.record(MODEL, now=stamp)
    assert value.snapshot()["requests"] == 0


def test_concurrent_recorders_increment_atomically(tmp_path):
    value, _store = ledger(tmp_path)
    def batch(_index):
        for _ in range(12):
            assert value.record(MODEL, {"input_tokens": 2, "output_tokens": 1, "elapsed_ms": 10})
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(batch, range(4)))
    data = value.snapshot()
    assert data["requests"] == data["successful"] == 48
    assert data["known_input_tokens"] == 96 and data["known_output_tokens"] == 48
