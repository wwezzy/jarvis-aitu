"""Fake provider responses only: no messages, phone calls or paid requests."""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sqlite3
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
    with sqlite3.connect(delivery._path) as db:
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
