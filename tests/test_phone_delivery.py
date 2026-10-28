"""Fake provider responses only: no messages, phone calls or paid requests."""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import parse_qs
from xml.etree import ElementTree

import pytest

from desktop.phone_delivery import PhoneDelivery, _NoRedirect


BOT = "123456789:" + "AbCdEf_0123456789" * 3
SID = "AC" + "1" * 32
SECRET = "2" * 32
FROM, TO = "+15550000001", "+15550000002"
ENV = {"DESKTOP_TELEGRAM_ENABLED": "1", "DESKTOP_TELEGRAM_BOT_TOKEN": BOT,
       "DESKTOP_TELEGRAM_CHAT_ID": "123456789", "JARVIS_PHONE_CALLS_ENABLED": "1",
       "TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": SECRET, "TWILIO_FROM": FROM, "TWILIO_TO": TO}


class Response(io.BytesIO):
    def __init__(self, request, result, *, code=200, content_type="application/json", url=None):
        super().__init__(result if isinstance(result, bytes) else json.dumps(result).encode())
        self.code, self.url = code, url or request.full_url
        self.headers = {"Content-Type": content_type}

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, behavior=None):
        self.calls, self.behavior = [], behavior

    def open(self, request, timeout):
        self.calls.append(request)
        assert timeout == 8
        if self.behavior:
            return self.behavior(request)
        if request.full_url.startswith("https://api.telegram.org/"):
            return Response(request, {"ok": True, "result": {"message_id": len(self.calls), "chat": {"id": 123456789}}})
        return Response(request, {"sid": "CA" + "3" * 32, "account_sid": SID, "to": TO, "status": "queued"}, code=201)


@pytest.fixture
def factory(tmp_path):
    now = [datetime.fromisoformat("2026-10-28T12:00:00+05:00").timestamp()]
    counter = [0]

    def make(env=None, behavior=None, folder=None, lookup=None):
        counter[0] += 1
        opener = Opener(behavior)
        delivery = PhoneDelivery(folder or tmp_path / str(counter[0]), environ=ENV if env is None else env,
                                 opener=opener, clock=lambda: now[0], current_validator=lookup)
        return delivery, opener, now

    return make


def rows(delivery):
    with closing(sqlite3.connect(delivery._path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY provider,event_id")]


def test_default_disabled_has_no_files_or_network_and_does_not_claim_delivery(factory):
    delivery, opener, _ = factory(env={})
    assert not delivery.enqueue("event", "Hello", call=True, is_current=lambda: True)
    status = delivery.process_pending()
    assert status["telegram"] == status["calls"] == "not_configured"
    assert not delivery._path.exists() and not opener.calls


@pytest.mark.parametrize("changes", [{"DESKTOP_TELEGRAM_ENABLED": "0", "JARVIS_PHONE_CALLS_ENABLED": "0"},
    {"DESKTOP_TELEGRAM_BOT_TOKEN": "bad", "TWILIO_ACCOUNT_SID": "bad"},
    {"DESKTOP_TELEGRAM_CHAT_ID": "-100123456", "TWILIO_TO": "911"}])
def test_invalid_or_unapproved_configuration_cannot_target_others(factory, changes):
    delivery, opener, _ = factory(env={**ENV, **changes})
    assert not delivery.enqueue("event", "Hello", call=True, is_current=lambda: True)
    assert delivery.process_pending()["counts"] == {} and not opener.calls


def test_enqueue_is_durable_idempotent_per_provider_and_never_sends_by_itself(factory):
    delivery, opener, _ = factory()
    assert delivery.enqueue("event:1", "Your literal plan", is_current=lambda: True)
    assert not delivery.enqueue("event:1", "Changed text", is_current=lambda: True)
    assert not opener.calls
    assert delivery.process_pending()["counts"] == {"provider_accepted": 1}
    assert len(opener.calls) == 1
    assert delivery.process_pending()["counts"] == {"provider_accepted": 1}
    assert len(opener.calls) == 1 and rows(delivery)[0]["text"] == "Your literal plan"


def test_calls_require_both_env_flag_and_per_event_true(factory):
    delivery, opener, _ = factory(env={**ENV, "DESKTOP_TELEGRAM_ENABLED": "0"})
    assert not delivery.enqueue("text-only", "Reminder", is_current=lambda: True)
    assert delivery.enqueue("call", "Reminder", call=True, is_current=lambda: True)
    status = delivery.process_pending()
    assert status["counts"] == {"provider_accepted": 1} and len(opener.calls) == 1
    assert opener.calls[0].full_url.startswith("https://api.twilio.com/")


def test_telegram_uses_only_fixed_owner_and_literal_text_without_parse_mode(factory):
    delivery, opener, _ = factory()
    text = '<script>alert("no")</script> https://private.example/plan'
    delivery.enqueue("literal", text, is_current=lambda: True)
    delivery.process_pending()
    request = opener.calls[0]
    assert request.full_url == f"https://api.telegram.org/bot{BOT}/sendMessage"
    payload = json.loads(request.data)
    assert payload == {"chat_id": "123456789", "text": text, "link_preview_options": {"is_disabled": True}}
    assert request.method == "POST" and "parse_mode" not in payload


def test_twilio_inline_xml_escapes_text_and_has_no_callback_url_record_or_dial(factory):
    delivery, opener, _ = factory(env={**ENV, "DESKTOP_TELEGRAM_ENABLED": "0"})
    text = '</Say><Dial>+19999999999</Dial><Say> Сэр & план'
    delivery.enqueue("xml", text, call=True, is_current=lambda: True)
    status = delivery.process_pending()
    payload = parse_qs(opener.calls[0].data.decode())
    tree = ElementTree.fromstring(payload["Twiml"][0])
    assert tree.find("Say").text == text and tree.find("Dial") is None
    assert tree.find("Say").attrib == {"voice": "Polly.Tatyana", "language": "ru-RU"}
    assert payload["From"] == [FROM] and payload["To"] == [TO]
    assert payload["TimeLimit"] == ["60"] and payload["Record"] == ["false"]
    assert "Url" not in payload and "StatusCallback" not in payload
    assert status["voice"] == "provider_fallback"


def test_optional_personal_audio_is_only_fixed_https_env_and_never_model_text_url(factory):
    url = "https://audio.example/approved.wav?signature=private-query"
    delivery, opener, _ = factory(env={**ENV, "DESKTOP_TELEGRAM_ENABLED": "0", "TWILIO_PERSONAL_AUDIO_URL": url})
    delivery.enqueue("personal", "https://attacker.example/not-used.wav", call=True, is_current=lambda: True)
    status = delivery.process_pending()
    xml = parse_qs(opener.calls[0].data.decode())["Twiml"][0]
    root = ElementTree.fromstring(xml)
    assert root.find("Play").text == url and root.find("Say") is None
    assert "attacker.example" not in xml and status["voice"] == "personal_audio"
    assert b"private-query" not in delivery._path.read_bytes()


@pytest.mark.parametrize("url", ["http://audio.example/voice.wav", "https://localhost/voice.wav",
    "https://127.0.0.1/voice.wav", "https://10.0.0.1/voice.wav", "https://user:pass@audio.example/voice.wav",
    "https://audio.example:8443/voice.wav", "https://audio.example/voice.wav#fragment"])
def test_invalid_personal_audio_disables_calls_instead_of_silent_fallback(factory, url):
    delivery, opener, _ = factory(env={**ENV, "DESKTOP_TELEGRAM_ENABLED": "0", "TWILIO_PERSONAL_AUDIO_URL": url})
    assert not delivery.enqueue("call", "Reminder", call=True, is_current=lambda: True)
    assert delivery.status()["calls"] == "not_configured" and not opener.calls


def test_stale_missing_and_changed_current_event_guards_discard_without_network(factory):
    delivery, opener, _ = factory()
    current = [True]
    delivery.enqueue("missing", "No current validator")
    delivery.enqueue("stale", "Old plan", is_current=lambda: False)
    delivery.enqueue("changed", "Changed plan", is_current=lambda: current[0])
    current[0] = False
    assert delivery.process_pending()["counts"] == {"discarded": 3}
    assert not opener.calls and not delivery._guards


def test_guard_is_checked_again_immediately_before_post(factory):
    delivery, opener, _ = factory()
    checked = []

    def current():
        checked.append(True)
        return len(checked) == 1

    delivery.enqueue("changed-late", "Reminder", is_current=current)
    assert delivery.process_pending()["counts"] == {"discarded": 1}
    assert len(checked) == 2 and not opener.calls


def test_restart_discards_pending_closures_unless_explicit_current_lookup_revalidates(factory):
    first, _, _ = factory()
    first.enqueue("pending", "Current reminder", is_current=lambda: True)
    restarted, opener, _ = factory(folder=first._path.parent)
    assert restarted.process_pending()["counts"] == {"discarded": 1} and not opener.calls
    second, _, _ = factory()
    second.enqueue("lookup", "Current reminder", is_current=lambda: True)
    checked = []
    fresh, opener, _ = factory(folder=second._path.parent,
        lookup=lambda event_id, text: checked.append((event_id, text)) or True)
    assert fresh.process_pending()["counts"] == {"provider_accepted": 1}
    assert checked == [("lookup", "Current reminder"), ("lookup", "Current reminder")] and len(opener.calls) == 1


def test_claimed_event_after_crash_is_uncertain_and_never_sent_again(factory):
    delivery, _, _ = factory()
    delivery.enqueue("crash", "Reminder", is_current=lambda: True)
    with sqlite3.connect(delivery._path) as db:
        db.execute("UPDATE outbox SET state='claimed',attempts=1")
    fresh, opener, _ = factory(folder=delivery._path.parent, lookup=lambda *_: True)
    assert fresh.process_pending()["counts"] == {"uncertain": 1} and not opener.calls


def test_concurrent_processors_cannot_post_the_same_event_twice(factory):
    first, opener_a, _ = factory()
    first.enqueue("concurrent", "Reminder", is_current=lambda: True)
    second, opener_b, _ = factory(folder=first._path.parent, lookup=lambda *_: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda delivery: delivery.process_pending(), (first, second)))
    assert len(opener_a.calls) + len(opener_b.calls) == 1
    assert rows(first)[0]["state"] == "provider_accepted"


def test_stale_processor_validation_failure_cannot_overwrite_accepted_result(factory):
    env = {**ENV, "DESKTOP_TELEGRAM_ENABLED": "0"}
    first, opener_a, now = factory(env={**env, "TWILIO_PERSONAL_AUDIO_URL": "https://audio.example/owned.wav"})
    # Legal queue text, but too large after escaping for dynamic TwiML. The
    # fixed-audio instance has a valid request for the same queued event.
    assert first.enqueue("concurrent-validation", "<" * 1000, call=True, is_current=lambda: True)
    selected, release = threading.Event(), threading.Event()
    def current(*_args):
        selected.set()
        assert release.wait(3)
        return True
    second, opener_b, _ = factory(env=env, folder=first._path.parent, lookup=current)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(second.process_pending)
        try:
            assert selected.wait(1)
            first.process_pending()
        finally:
            release.set()
        future.result(timeout=3)
    now[0] += 60  # Retry only the failed preflight, which never sent a POST.
    first.process_pending()
    assert len(opener_a.calls) == 1 and not opener_b.calls
    assert rows(first)[0]["state"] == "provider_accepted"


def test_new_instance_cannot_recover_another_live_processors_claim_as_orphan(factory):
    entered, release = threading.Event(), threading.Event()
    def response(request):
        entered.set()
        assert release.wait(3)
        return Response(request, {"ok": True, "result": {"message_id": 1, "chat": {"id": 123456789}}})
    first, opener_a, _ = factory(behavior=response)
    first.enqueue("live-claim", "Reminder", is_current=lambda: True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.process_pending)
        try:
            assert entered.wait(1)
            second, opener_b, _ = factory(folder=first._path.parent, lookup=lambda *_args: True)
            assert rows(first)[0]["state"] == "claimed"
            second.process_pending()
            assert not opener_b.calls
        finally:
            release.set()
        future.result(timeout=3)
    assert len(opener_a.calls) == 1 and rows(first)[0]["state"] == "provider_accepted"


def test_failure_before_durable_attempt_claim_is_not_an_ambiguous_post(factory, monkeypatch):
    delivery, opener, now = factory()
    delivery.enqueue("before-claim", "Reminder", is_current=lambda: True)
    def failed_request(_row):
        raise OSError("synthetic temporary preflight failure")
    with monkeypatch.context() as patch:
        patch.setattr(delivery, "_request", failed_request)
        delivery.process_pending()
    assert not opener.calls and rows(delivery)[0]["state"] == "pending"
    assert rows(delivery)[0]["attempts"] == 0
    delivery.process_pending()
    assert not opener.calls
    now[0] += 60
    assert delivery.process_pending()["counts"] == {"provider_accepted": 1}
    assert len(opener.calls) == 1


def test_failed_preflight_rows_do_not_starve_later_valid_events(factory, monkeypatch):
    delivery, opener, _ = factory()
    for index in range(5):
        delivery.enqueue(f"queued-{index}", "Reminder", is_current=lambda: True)
    request = delivery._request
    def preflight(row):
        if row["event_id"] != "queued-4":
            raise ValueError("synthetic invalid request")
        return request(row)
    monkeypatch.setattr(delivery, "_request", preflight)
    assert delivery.process_pending()["counts"] == {"pending": 5}
    assert delivery.process_pending()["counts"] == {"pending": 4, "provider_accepted": 1}
    assert len(opener.calls) == 1


def test_stale_state_or_attempt_update_cannot_overwrite_a_provider_result(factory):
    delivery, _opener, _ = factory()
    delivery.enqueue("fenced", "Reminder", is_current=lambda: True)
    delivery.process_pending()
    original = rows(delivery)[0]
    key = ("telegram", "fenced")
    assert not delivery._update(key, "uncertain", attempt=original["attempts"])
    assert not delivery._update(key, "discarded", expected_state="pending", attempt=0)
    assert rows(delivery)[0] == original
    with sqlite3.connect(delivery._path) as db:
        db.execute("UPDATE outbox SET state='claimed',attempts=2 WHERE event_id='fenced'")
    assert not delivery._update(key, "uncertain", attempt=1)
    assert rows(delivery)[0]["state"] == "claimed"


def test_processor_lease_is_cross_process_and_crash_recovery_cannot_repost(factory):
    delivery, opener, _ = factory()
    delivery.enqueue("cross-process", "Reminder", is_current=lambda: True)
    with sqlite3.connect(delivery._path) as db:
        db.execute("UPDATE outbox SET state='claimed',attempts=1")
    code = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from desktop.phone_delivery import PhoneDelivery
delivery = PhoneDelivery(Path(sys.argv[2]), environ={})
with delivery._processor_lease() as acquired:
    print('locked' if acquired else 'busy', flush=True)
    sys.stdin.buffer.read(1)
"""
    # Windows venv python.exe is a launcher with a separate interpreter child;
    # terminate the actual lock owner, not that launcher's wrapper process.
    python = getattr(sys, "_base_executable", sys.executable)
    process = subprocess.Popen([python, "-I", "-c", code,
        str(Path(__file__).resolve().parents[1]), str(delivery._path.parent)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        assert pool.submit(process.stdout.readline).result(timeout=5).strip() == b"locked"
        observer, observer_opener, _ = factory(folder=delivery._path.parent, lookup=lambda *_args: True)
        assert rows(delivery)[0]["state"] == "claimed"
        observer.process_pending()
        assert not observer_opener.calls and rows(delivery)[0]["state"] == "claimed"
    finally:
        if process.poll() is None:
            process.terminate()  # Only the test-owned, credential-free helper.
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        pool.shutdown(wait=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    assert delivery.process_pending()["counts"] == {"uncertain": 1}
    assert not opener.calls


def test_ambiguous_timeout_never_retries_or_claims_delivered_and_stores_no_error_details(factory):
    def timeout(request):
        raise TimeoutError("token=" + BOT + " private phone=" + TO)
    delivery, opener, now = factory(behavior=timeout)
    delivery.enqueue("timeout", "Reminder", is_current=lambda: True)
    assert delivery.process_pending()["counts"] == {"uncertain": 1}
    now[0] += 500
    delivery.process_pending()
    assert len(opener.calls) == 1 and rows(delivery)[0]["error_class"] == "TimeoutError"
    raw = delivery._path.read_bytes()
    assert BOT.encode() not in raw and SECRET.encode() not in raw and TO.encode() not in raw


def test_only_explicit_telegram_rate_limit_rejection_retries_three_times_with_backoff(factory):
    def behavior(request):
        return Response(request, {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}, code=429)
    delivery, opener, now = factory(behavior=behavior)
    delivery.enqueue("rate-limited", "Reminder", is_current=lambda: True)
    assert delivery.process_pending()["counts"] == {"pending": 1}
    assert rows(delivery)[0]["attempts"] == 1
    now[0] += 59
    delivery.process_pending()
    assert len(opener.calls) == 1
    now[0] += 1
    delivery.process_pending()
    assert len(opener.calls) == 2 and rows(delivery)[0]["attempts"] == 2
    now[0] += 119
    delivery.process_pending()
    assert len(opener.calls) == 2
    now[0] += 1
    assert delivery.process_pending()["counts"] == {"failed": 1}
    assert len(opener.calls) == 3 and not delivery._guards


def test_provider_response_without_matching_ids_is_uncertain_not_accepted(factory):
    delivery, _, _ = factory(behavior=lambda request: Response(request, {"ok": True, "result": {"message_id": 12, "chat": {"id": 999}}}))
    delivery.enqueue("wrong-recipient-result", "Reminder", is_current=lambda: True)
    assert delivery.process_pending()["counts"] == {"uncertain": 1}


@pytest.mark.parametrize("kind", ["redirect", "oversized", "not-json"])
def test_redirects_response_limits_and_wrong_content_type_never_retry(factory, kind):
    def behavior(request):
        if kind == "redirect":
            return Response(request, {}, code=302, url="https://attacker.example/")
        if kind == "oversized":
            return Response(request, b"x" * 16385)
        return Response(request, b"not-json", content_type="text/html")
    delivery, opener, now = factory(behavior=behavior)
    delivery.enqueue("bad-response", "Reminder", is_current=lambda: True)
    assert delivery.process_pending()["counts"] == {"uncertain": 1}
    now[0] += 60
    delivery.process_pending()
    assert len(opener.calls) == 1
    assert _NoRedirect().redirect_request(None, None, None, None, None, None) is None


def test_calls_obey_quiet_hours_daily_cap_and_uncertain_call_is_never_retried(factory):
    def timeout(request):
        raise TimeoutError("unknown POST outcome")
    delivery, opener, now = factory(env={**ENV, "DESKTOP_TELEGRAM_ENABLED": "0"}, behavior=timeout)
    now[0] = datetime.fromisoformat("2026-10-28T07:59:00+05:00").timestamp()
    delivery.enqueue("first", "Morning", call=True, is_current=lambda: True)
    assert delivery.process_pending()["counts"] == {"pending": 1} and not opener.calls
    now[0] += 60
    assert delivery.process_pending()["counts"] == {"uncertain": 1}
    delivery.enqueue("second", "No second call today", call=True, is_current=lambda: True)
    assert delivery.process_pending()["counts"] == {"daily_limit": 1, "uncertain": 1}
    assert len(opener.calls) == 1


def test_2230_call_is_deferred_to_next_morning_and_events_expire_at_24_hours(factory):
    delivery, opener, now = factory(env={**ENV, "DESKTOP_TELEGRAM_ENABLED": "0"})
    now[0] = datetime.fromisoformat("2026-10-28T22:30:00+05:00").timestamp()
    delivery.enqueue("night", "Tomorrow morning", call=True, is_current=lambda: True)
    assert delivery.process_pending()["counts"] == {"pending": 1} and not opener.calls
    now[0] = datetime.fromisoformat("2026-10-29T08:00:00+05:00").timestamp()
    assert delivery.process_pending()["counts"] == {"provider_accepted": 1} and len(opener.calls) == 1
    delivery.enqueue("expire", "Old plan", call=True, is_current=lambda: True)
    now[0] += 86400
    delivery.process_pending()
    assert rows(delivery)[0]["state"] == "discarded" and len(opener.calls) == 1


def test_no_secrets_or_raw_provider_data_in_status_or_console(factory, capsys):
    delivery, _, _ = factory()
    delivery.enqueue("safe", "Public synthetic reminder", is_current=lambda: True)
    status = json.dumps(delivery.process_pending())
    output = capsys.readouterr()
    for secret in (BOT, SECRET, TO, FROM):
        assert secret not in status + output.out + output.err
        assert secret.encode() not in delivery._path.read_bytes()
