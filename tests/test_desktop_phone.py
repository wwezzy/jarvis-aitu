"""Disposable loopback HTTP/PWA tests; no real phone, tunnel or personal data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
from http.client import HTTPConnection, HTTPResponse
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time

import pytest

from desktop.organizer import LocalOrganizer
from desktop.store import LocalStore
from desktop import phone


TOKEN = "Synthetic_PhoneTest_0123456789_AbCdEfGhIjKlMn"
ENV = {"JARVIS_PHONE_ENABLED": "1", "JARVIS_PHONE_TOKEN": TOKEN}
TITLE = '<img src="x" onerror="window.phoneAttacked=true"> Моя цель'


@pytest.fixture
def local_phone(tmp_path):
    store = LocalStore(tmp_path / "organizer")
    organizer = LocalOrganizer(store, clock=lambda: datetime.fromisoformat("2026-10-27T09:00:00+05:00").timestamp())
    adapter = phone.OrganizerPhoneAdapter(organizer)
    server = phone.PhoneServer(adapter, tmp_path / "transport", port=0, environ=ENV)
    assert server.start()["running"]
    yield server, organizer
    server.stop()
    store.close()


def request(server, method="GET", path="/api/plan", *, payload=None, raw=None, token=TOKEN,
            headers=None, key="synthetic-request-1"):
    connection = HTTPConnection("127.0.0.1", server._server.server_port, timeout=4)
    values = {} if token is None else {"Authorization": "Bearer " + token}
    if payload is not None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if raw is not None:
        values.update({"Content-Type": "application/json", "Idempotency-Key": key})
    values.update(headers or {})
    try:
        connection.request(method, path, body=raw, headers=values)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def decoded(result):
    return json.loads(result[2])


@pytest.mark.parametrize("environment", [{}, {"JARVIS_PHONE_ENABLED": "1"},
    {"JARVIS_PHONE_TOKEN": TOKEN}, {"JARVIS_PHONE_ENABLED": "1", "JARVIS_PHONE_TOKEN": "a" * 40},
    {**ENV, "JARVIS_PHONE_ORIGIN": "http://public.example"},
    {**ENV, "JARVIS_PHONE_ORIGIN": "https://user:password@example.com"},
    {**ENV, "JARVIS_PHONE_ORIGIN": "https://example.com/?token=secret"}])
def test_incomplete_or_unsafe_config_stays_disabled_without_files_or_socket(tmp_path, environment):
    server = phone.PhoneServer(object(), tmp_path / "absent", environ=environment)
    assert server.start()["enabled"] is False
    assert server.status()["running"] is False and server.status()["url"] is None
    assert not (tmp_path / "absent").exists()
    server.stop()


def test_only_loopback_listener_and_no_unconfigured_delivery_claim(local_phone):
    server, _ = local_phone
    assert server._server.socket.getsockname()[0] == "127.0.0.1"
    status = server.status()
    assert status["transport"] == "loopback_only"
    assert status["delivery"]["push"] == status["delivery"]["phone_calls"] == "not_configured"
    assert TOKEN not in json.dumps(status)


def test_read_requires_bearer_and_has_private_json_headers(local_phone):
    server, organizer = local_phone
    organizer.add_item(TITLE, "2026-10-27")
    assert request(server, token=None)[0] == 401
    assert request(server, token="wrong-token")[0] == 401
    response = request(server)
    assert response[0] == 200
    assert response[1]["Cache-Control"] == "no-store"
    assert response[1]["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in response[1]
    assert "Set-Cookie" not in response[1]
    assert "preferences" not in decoded(response)
    item = decoded(response)["today"]["items"][0]
    assert item["title"] == TITLE and item["status"] == "unknown" and item["provenance"] is None


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {"Origin": "null"},
                                      {"Host": "evil.example"}, {"Host": "localhost.attacker.example"}])
def test_forged_host_or_origin_rejected_even_with_valid_token(local_phone, headers):
    server, organizer = local_phone
    result = request(server, "POST", "/api/plan", payload={"date_key": "2026-10-27", "title": "Must not save"}, headers=headers)
    assert result[0] == 403 and not organizer.day("2026-10-27")["items"]


def rejected_headers(server, length):
    return (f"POST /api/plan HTTP/1.1\r\nHost: 127.0.0.1:{server._server.server_port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\nOrigin: null\r\nContent-Type: application/json\r\n"
            f"Content-Length: {length}\r\nIdempotency-Key: rejected-body\r\n\r\n").encode("ascii")


def test_rejected_split_post_drains_raw_body_without_parsing_or_dispatch(local_phone, monkeypatch):
    server, organizer = local_phone
    raw = b'{"date_key":"2026-10-27","title":"Must never be parsed"}'

    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected input must not be parsed or dispatched")

    with monkeypatch.context() as patch:
        patch.setattr(phone.json, "loads", forbidden)
        patch.setattr(server._store, "claim", forbidden)
        with socket.create_connection(("127.0.0.1", server._server.server_port), timeout=3) as connection:
            connection.sendall(rejected_headers(server, len(raw)))
            # A separate write exercises the Windows unread-body reset race.
            time.sleep(0.04)
            connection.sendall(raw)
            response = HTTPResponse(connection)
            response.begin()
            assert response.status == 403
            assert response.read() == b'{"error":"origin_refused"}'
            response.close()
    assert not organizer.day("2026-10-27")["items"]


def test_rejected_truncated_body_returns_complete_error_without_waiting_for_timeout(local_phone):
    server, organizer = local_phone
    with socket.create_connection(("127.0.0.1", server._server.server_port), timeout=3) as connection:
        started = time.monotonic()
        connection.sendall(rejected_headers(server, phone.MAX_BODY) + b"{truncated")
        connection.shutdown(socket.SHUT_WR)
        response = HTTPResponse(connection)
        response.begin()
        assert response.status == 403
        assert response.read() == b'{"error":"origin_refused"}'
        response.close()
        assert time.monotonic() - started < 1.25
    assert not organizer.day("2026-10-27")["items"]


def test_rejected_drip_feed_has_one_absolute_deadline_not_per_byte_timeout(local_phone):
    server, organizer = local_phone
    stop = threading.Event()
    with socket.create_connection(("127.0.0.1", server._server.server_port), timeout=3) as connection:
        started = time.monotonic()
        connection.sendall(rejected_headers(server, phone.MAX_BODY))

        def drip():
            while not stop.wait(0.03):
                try:
                    connection.sendall(b"x")
                except OSError:
                    return

        writer = threading.Thread(target=drip, daemon=True)
        writer.start()
        try:
            response = HTTPResponse(connection)
            response.begin()
            assert response.status == 403
            assert response.read() == b'{"error":"origin_refused"}'
            response.close()
            elapsed = time.monotonic() - started
            assert phone._REJECT_DRAIN_SECONDS * 0.7 <= elapsed < 1.25
        finally:
            stop.set()
            writer.join(timeout=1)
    assert not writer.is_alive()
    assert not organizer.day("2026-10-27")["items"]


def test_exact_configured_https_proxy_origin_allowed_without_claiming_verified_transport(local_phone):
    server, _ = local_phone
    server.origin = "https://jarvis.private.ts.net"
    assert request(server, headers={"Host": "jarvis.private.ts.net", "Origin": server.origin})[0] == 200
    assert request(server, headers={"Host": "jarvis.private.ts.net", "Origin": server.origin + ".evil"})[0] == 403
    assert server.status()["transport"] == "https_proxy_not_verified"


def test_duplicate_host_headers_refused(local_phone):
    server, _ = local_phone
    connection = HTTPConnection("127.0.0.1", server._server.server_port, timeout=3)
    try:
        connection.putrequest("GET", "/api/plan", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{server._server.server_port}")
        connection.putheader("Host", "evil.example")
        connection.putheader("Authorization", "Bearer " + TOKEN)
        connection.endheaders()
        assert connection.getresponse().status == 400
    finally:
        connection.close()


def test_global_auth_failure_and_request_caps_do_not_depend_on_forwarded_ip(local_phone):
    server, _ = local_phone
    server._gate = phone._RateGate(maximum=20, auth_failures=2)
    assert request(server, token="wrong", headers={"X-Forwarded-For": "1.1.1.1"})[0] == 401
    assert request(server, token="wrong", headers={"X-Forwarded-For": "2.2.2.2"})[0] == 401
    assert request(server, token="wrong")[0] == 429
    assert request(server)[0] == 200
    server._gate = phone._RateGate(maximum=1)
    assert request(server)[0] == 200
    assert request(server)[0] == 429


@pytest.mark.parametrize("path", ["/api/plan?token=never-query", "/index.html?token=never-query",
                                 "http://evil.example/api/plan", "/../phone.py", "/%2e%2e/phone.py"])
def test_url_credentials_absolute_targets_and_traversal_do_not_expose_files(local_phone, path):
    server, _ = local_phone
    assert request(server, path=path)[0] in (400, 403, 404)


@pytest.mark.parametrize("body", [b'{"title":"first","title":"second","date_key":"2026-10-27"}',
                                 b'{"title":NaN,"date_key":"2026-10-27"}', b'[]', b'{invalid}',
                                 b'{"title":"\\ud800","date_key":"2026-10-27"}'])
def test_strict_json_rejects_duplicates_nan_surrogates_and_non_objects(local_phone, body):
    server, organizer = local_phone
    assert request(server, "POST", "/api/plan", raw=body)[0] == 400
    assert not organizer.day("2026-10-27")["items"]


def test_request_body_header_content_type_and_idempotency_limits(local_phone):
    server, _ = local_phone
    assert request(server, "POST", "/api/plan", raw=b"x" * 8193)[0] == 413
    assert request(server, "POST", "/api/plan", raw=b"{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert request(server, "POST", "/api/plan", raw=b"{}", key="x" * 65)[0] == 400
    assert request(server, "POST", "/api/plan", raw=b"{}", headers={"Content-Encoding": "gzip"})[0] == 400
    assert request(server, headers={"X-Oversized": "x" * 17000})[0] == 431
    assert request(server, "OPTIONS")[0] == 405


def test_report_plan_and_gym_write_real_synthetic_organizer_without_os_or_llm(local_phone):
    server, organizer = local_phone
    response = request(server, "POST", "/api/plan", payload={"date_key": "2026-10-27", "title": TITLE}, key="plan-1")
    assert response[0] == 200
    item_id = decoded(response)["item_id"]
    result = request(server, "POST", "/api/report", payload={"item_id": item_id, "status": "done"}, key="report-1")
    assert decoded(result)["provenance"] == "self_report"
    item = organizer.day("2026-10-27")["items"][0]
    assert item["status"] == "done" and item["title"] == TITLE
    assert request(server, "POST", "/api/gym", payload={"weekdays": [1, 3, 6]}, key="gym-1")[0] == 200
    assert organizer.gym_settings()["time"] is None
    assert organizer.gym_settings()["days"] == [1, 3, 6]
    assert request(server, "POST", "/api/gym", payload={"weekdays": [], "time": None}, key="gym-2")[0] == 200
    assert organizer.gym_settings()["enabled"] is False


def test_phone_gym_form_preserves_duration_authored_on_desktop(local_phone):
    server, organizer = local_phone
    organizer.set_weekly_gym([1, 3], time="18:00", minutes=45)
    response = request(server, "POST", "/api/gym", payload={"weekdays": [1, 3, 6], "time": None})
    assert response[0] == 200
    assert organizer.gym_settings()["minutes"] == decoded(response)["minutes"] == 45
    assert organizer.gym_settings()["time"] is None


@pytest.mark.parametrize("path,payload", [("/api/plan", {"date_key": "2026-02-30", "title": "Test"}),
    ("/api/plan", {"date_key": "2026-10-27", "title": "Test", "command": "shutdown"}),
    ("/api/report", {"item_id": "goal:1", "status": "auto_done"}),
    ("/api/gym", {"weekdays": [True]}), ("/api/gym", {"weekdays": [1, 1]}),
    ("/api/gym", {"weekdays": [1], "time": "25:90"}), ("/api/gym", {"weekdays": [1], "execute": True})])
def test_closed_schema_rejects_commands_bad_dates_and_inferred_status(local_phone, path, payload):
    server, organizer = local_phone
    assert request(server, "POST", path, payload=payload)[0] == 400
    assert not organizer.day("2026-10-27")["items"]


def test_retries_and_restart_replay_one_durable_write_without_plaintext_credentials(local_phone):
    server, organizer = local_phone
    payload = {"date_key": "2026-10-27", "title": "One synthetic task"}
    first = request(server, "POST", "/api/plan", payload=payload, key="durable-retry")
    second = request(server, "POST", "/api/plan", payload=payload, key="durable-retry")
    assert first[0] == second[0] == 200 and first[2] == second[2]
    assert second[1]["Idempotency-Replayed"] == "true"
    reordered = request(server, "POST", "/api/plan", payload={"title": payload["title"], "date_key": payload["date_key"]}, key="durable-retry")
    assert reordered[0] == 200 and reordered[2] == first[2]
    server.stop()
    assert server.start()["running"]
    third = request(server, "POST", "/api/plan", payload=payload, key="durable-retry")
    assert third[2] == first[2] and len(organizer.day("2026-10-27")["items"]) == 1
    data = server._store.path.read_bytes()
    assert TOKEN.encode() not in data and b"durable-retry" not in data
    with sqlite3.connect(server._store.path) as db:
        assert db.execute("SELECT token_hash FROM phone_claims").fetchone()[0] == hashlib.sha256(TOKEN.encode()).hexdigest()


def test_same_key_with_changed_request_refuses_second_write(local_phone):
    server, organizer = local_phone
    payload = {"date_key": "2026-10-27", "title": "First"}
    assert request(server, "POST", "/api/plan", payload=payload)[0] == 200
    payload["title"] = "Second"
    assert decoded(request(server, "POST", "/api/plan", payload=payload))["error"] == "idempotency_conflict"
    assert len(organizer.day("2026-10-27")["items"]) == 1


def test_crash_after_mutation_before_response_is_not_repeated(local_phone, monkeypatch):
    server, organizer = local_phone
    original = server._store.finish
    monkeypatch.setattr(server._store, "finish", lambda *_: (_ for _ in ()).throw(OSError("private disk error")))
    payload = {"date_key": "2026-10-27", "title": "Possibly completed"}
    assert request(server, "POST", "/api/plan", payload=payload)[0] == 503
    monkeypatch.setattr(server._store, "finish", original)
    retry = request(server, "POST", "/api/plan", payload=payload)
    assert retry[0] == 409 and decoded(retry)["error"] == "result_uncertain"
    assert len(organizer.day("2026-10-27")["items"]) == 1


def test_concurrent_duplicate_attempts_execute_at_most_once(local_phone):
    server, organizer = local_phone
    payload = {"date_key": "2026-10-27", "title": "Concurrent task"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: request(server, "POST", "/api/plan", payload=payload), range(4)))
    assert all(result[0] in (200, 409) for result in results)
    assert len(organizer.day("2026-10-27")["items"]) == 1


def test_errors_and_http_access_never_log_content_or_token(local_phone, monkeypatch, capsys):
    server, _ = local_phone
    monkeypatch.setattr(server.api, "snapshot", lambda: (_ for _ in ()).throw(RuntimeError("private diary")))
    result = request(server)
    assert result[0] == 503 and b"private diary" not in result[2]
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err and "private diary" not in captured.out + captured.err


def test_public_static_shell_has_csp_and_no_private_plan_or_external_scripts(local_phone):
    server, organizer = local_phone
    organizer.add_item(TITLE, "2026-10-27")
    for path in ("/", "/app.js", "/style.css", "/manifest.webmanifest", "/sw.js"):
        response = request(server, path=path, token=None)
        assert response[0] == 200
        assert "script-src 'self'" in response[1]["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in response[1]["Content-Security-Policy"]
        assert TITLE.encode() not in response[2] and TOKEN.encode() not in response[2]
    script = (Path(phone.__file__).parent / "phone_web" / "app.js").read_text(encoding="utf-8")
    assert ".innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "location.hash" in script and "history.replaceState" in script


@pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="Dedicated browser CI enables Chromium")
async def test_phone_browser_mobile_xss_reports_plan_gym_and_static_only_offline_cache(local_phone):
    from playwright.async_api import async_playwright, expect

    server, organizer = local_phone
    organizer.add_item(TITLE, "2026-10-27")
    base = server.status()["url"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=os.getenv("TEST_BROWSER_EXECUTABLE") or None)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            async def local_only(route):
                if route.request.url.startswith(base + "/"):
                    await route.continue_()
                else:
                    await route.abort()
            await page.route("**/*", local_only)
            await page.goto(base + "/#token=" + TOKEN)
            await expect(page.locator("#connection")).to_have_text("Подключено к твоему ноутбуку")
            assert "#" not in page.url and "?" not in page.url
            await expect(page.locator("#items h3")).to_have_text(TITLE)
            assert await page.locator("#items img").count() == 0
            assert await page.evaluate("window.phoneAttacked === undefined")
            await page.get_by_role("button", name="Готово", exact=True).click()
            await expect(page.locator("#items .item-status")).to_contain_text("Выполнено")
            await page.locator("#plan-date").fill("2026-10-28")
            await page.locator("#plan-title").fill("Synthetic tomorrow task")
            await page.get_by_role("button", name="Добавить в план").click()
            await expect(page.locator("#notice")).to_have_text("Новое дело добавлено.")
            await page.get_by_role("button", name="Завтра", exact=True).click()
            await expect(page.locator("#items")).to_contain_text("Synthetic tomorrow task")
            for value in ("1", "3", "6"):
                await page.locator(f'input[name="weekday"][value="{value}"]').check(force=True)
            await page.get_by_role("button", name="Сохранить дни зала").click()
            await expect(page.locator("#notice")).to_have_text("Дни зала сохранены.")
            assert organizer.gym_settings()["days"] == [1, 3, 6]
            await page.evaluate("navigator.serviceWorker.ready")
            await page.evaluate("async (token) => { await fetch('/style.css', {headers:{Authorization:'Bearer '+token}, cache:'no-store'}); }", TOKEN)
            cached = await page.evaluate("async () => { const keys=await caches.keys(); const rows=[]; for(const key of keys){for(const r of await (await caches.open(key)).keys()){rows.push({url:r.url,auth:r.headers.get('Authorization')});}} return rows; }")
            assert cached and all("/api/" not in row["url"] and row["auth"] is None for row in cached)
            server.stop()
            await page.reload()
            await expect(page.locator("#connection")).to_have_text("Связь с ноутбуком не подтверждена")
            assert await page.locator("#items h3").count() == 0
            assert not errors
        finally:
            await browser.close()
