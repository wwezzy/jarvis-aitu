"""Real disposable loopback HTTP, mocked remote policy; no PC operations."""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import sqlite3
import socket
import threading
import time

import pytest

from desktop.phone import OrganizerPhoneAdapter, PhoneServer
from desktop.organizer import LocalOrganizer
from desktop.store import LocalStore


PHONE_KEY = "Synthetic_Phone_Secret_0123456789_AbCdEfGhIjKl"
CONTROL_KEY = "Different_Control_Secret_9876543210_ZyXwVuTsRq"
CORE_TOKEN = "c" * 32
XSS = '<img src="x" onerror="window.remoteAttacked=true"> Калькулятор'


class Remote:
    def __init__(self):
        self.approved = True
        self.auth_calls, self.calls = [], []
        self.effects = 0
        self.serial = 0
        self.pending = set()
        self.expires_in = 60
        self.before_mutation = lambda: None

    def authorize(self, value):
        self.auth_calls.append(value)
        return self.approved and value == CONTROL_KEY

    def snapshot(self):
        return {"configured": True, "approved": self.approved, "available": self.approved, "reason": "ready",
                "actions": [{"name": "calculator", "title": XSS, "requires_confirmation": False,
                             "private_path": r"C:\Private\never-show"},
                            {"name": "shutdown", "title": "Выключить", "requires_confirmation": True}],
                "projects": [{"name": "Мой проект", "aliases": ["project"], "path": "/home/private"}],
                "apps": [{"name": "IDE", "aliases": ["idea"]}], "core_token": CORE_TOKEN}

    @staticmethod
    def result(status="reported", *, action=None, challenge=None, expires=0):
        return {"ok": status in {"reported", "confirmation_required", "canceled"}, "status": status,
                "text": "Подтверди выключение." if challenge else "Результат получен от ноутбука.",
                "action": action, "confirmation_id": challenge, "expires_in": expires,
                "internal_core_token": CORE_TOKEN, "private_path": r"C:\Private\never-show"}

    def request(self, payload, *, control_token=None):
        self.before_mutation()
        self.calls.append(("request", dict(payload)))
        if payload == {"kind": "action", "name": "shutdown"}:
            self.serial += 1
            challenge = f"{self.serial:032x}"
            self.pending.add(challenge)
            return self.result("confirmation_required", action="shutdown", challenge=challenge, expires=self.expires_in)
        self.effects += 1
        return self.result(action=payload["name"] if payload["kind"] == "action" else None)

    def confirm(self, token, *, control_token=None):
        self.before_mutation()
        self.calls.append(("confirm", token))
        if token not in self.pending:
            return self.result("confirmation_refused")
        self.pending.remove(token)
        self.effects += 1
        return self.result(action="shutdown")

    def cancel(self, token, *, control_token=None):
        self.before_mutation()
        self.calls.append(("cancel", token))
        if token not in self.pending:
            return self.result("confirmation_refused")
        self.pending.remove(token)
        return self.result("canceled", action="shutdown")


@pytest.fixture
def remote_phone(tmp_path):
    store = LocalStore(tmp_path / "organizer")
    remote = Remote()
    server = PhoneServer(OrganizerPhoneAdapter(LocalOrganizer(store)), tmp_path / "phone", port=0,
        environ={"JARVIS_PHONE_ENABLED": "1", "JARVIS_PHONE_TOKEN": PHONE_KEY}, remote=remote)
    assert server.start()["running"]
    yield server, remote
    server.stop()
    store.close()


def request(server, method="GET", path="/api/control", *, payload=None, raw=None,
            bearer=PHONE_KEY, control=CONTROL_KEY, key="remote-operation", headers=None):
    values = {}
    if bearer is not None:
        values["Authorization"] = "Bearer " + bearer
    if control is not None:
        values["X-Jarvis-Control"] = control
    if payload is not None:
        raw = json.dumps(payload).encode("utf-8")
    if raw is not None:
        values.update({"Content-Type": "application/json", "Idempotency-Key": key})
    values.update(headers or {})
    connection = HTTPConnection("127.0.0.1", server._server.server_port, timeout=4)
    try:
        connection.request(method, path, body=raw, headers=values)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def decoded(response):
    return json.loads(response[2])


def test_control_requires_both_independent_credentials_and_current_approval(remote_phone):
    server, remote = remote_phone
    assert request(server, bearer=None)[0] == 401
    assert request(server, control=None)[0] == 403
    assert request(server, control="Wrong_Control_Key_0123456789_AbCdEfGhIjKl")[0] == 403
    assert request(server, bearer="wrong")[0] == 401
    assert request(server)[0] == 200
    remote.approved = False
    assert request(server)[0] == 403
    assert not remote.calls and remote.effects == 0


def test_unconfigured_control_does_not_break_plan_or_claim_remote_connection(remote_phone):
    server, _remote = remote_phone
    server.remote = None
    assert decoded(request(server)) == {"error": "control_unavailable"}
    assert request(server, path="/api/plan", control=None)[0] == 200


def test_snapshot_projects_only_finite_catalog_fields_not_paths_or_internal_tokens(remote_phone):
    server, _remote = remote_phone
    response = request(server)
    assert response[0] == 200 and response[1]["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in response[1]
    data = decoded(response)
    assert data["projects"] == [{"name": "Мой проект", "aliases": ["project"]}]
    assert data["actions"][0]["title"] == XSS  # Literal JSON data, not executed HTML.
    assert CORE_TOKEN not in response[2].decode() and "Private" not in response[2].decode()
    assert "path" not in data["projects"][0]


@pytest.mark.parametrize("payload", [
    {"kind": "shell", "name": "calculator"}, {"kind": "action", "name": "rm_all"},
    {"kind": "action", "name": "calculator", "text": "do anything"},
    {"kind": "project", "name": "../escape"}, {"kind": "app", "name": "C:\\secret.exe"},
    {"kind": "app", "name": "https://example.invalid"}, {"kind": "app", "name": "bad\nname"},
    {"kind": "app", "name": 3}, {"kind": "camera", "name": "enable"},
])
def test_closed_payloads_refuse_freeform_commands_paths_and_policy_edits(remote_phone, payload):
    server, remote = remote_phone
    response = request(server, "POST", payload=payload)
    assert response[0] == 400 and not remote.calls
    with sqlite3.connect(server._store.path) as db:
        assert db.execute("SELECT count(*) FROM phone_claims").fetchone()[0] == 0


def test_missing_second_auth_and_forged_origin_never_parse_or_dispatch(remote_phone):
    server, remote = remote_phone
    assert request(server, "POST", raw=b"not-json", control=None)[0] == 403
    assert request(server, "POST", payload={"kind": "action", "name": "calculator"},
                   headers={"Origin": "https://evil.example"})[0] == 403
    assert not remote.calls


def test_remote_mutation_is_claimed_before_call_and_replay_is_generic_without_reexecution(remote_phone):
    server, remote = remote_phone
    def check_claim():
        with sqlite3.connect(server._store.path) as db:
            assert db.execute("SELECT count(*) FROM phone_claims WHERE http_status IS NULL").fetchone()[0] == 1
    remote.before_mutation = check_claim
    payload = {"kind": "action", "name": "calculator"}
    first = request(server, "POST", payload=payload)
    assert first[0] == 200 and decoded(first)["ok"] is True and remote.effects == 1
    second = request(server, "POST", payload=payload)
    assert second[0] == 409 and decoded(second) == {"error": "result_uncertain"}
    assert second[1]["Idempotency-Replayed"] == "true" and len(remote.calls) == 1


def test_remote_challenge_is_ram_response_only_and_confirm_cancel_are_explicit_single_use(remote_phone):
    server, remote = remote_phone
    proposal = request(server, "POST", payload={"kind": "action", "name": "shutdown"})
    challenge = decoded(proposal)["confirmation_id"]
    assert len(challenge) == 32 and challenge != CORE_TOKEN and remote.effects == 0
    assert decoded(proposal)["expires_in"] == 60
    assert CORE_TOKEN not in proposal[2].decode() and "internal_core_token" not in decoded(proposal)
    with sqlite3.connect(server._store.path) as db:
        dump = "\n".join(db.iterdump())
        assert db.execute("SELECT response FROM phone_claims").fetchone()[0] == '{"error":"result_uncertain"}'
    assert all(secret not in dump for secret in (PHONE_KEY, CONTROL_KEY, CORE_TOKEN, challenge))
    assert request(server, "POST", "/api/control/confirm", payload={"token": challenge}, key="confirm-1")[0] == 200
    assert remote.effects == 1
    repeated = request(server, "POST", "/api/control/confirm", payload={"token": challenge}, key="confirm-1")
    assert repeated[0] == 409 and remote.effects == 1
    result = request(server, "POST", "/api/control/cancel", payload={"token": challenge}, key="cancel-after-use")
    assert decoded(result)["status"] == "confirmation_refused" and remote.effects == 1
    proposal2 = decoded(request(server, "POST", payload={"kind": "action", "name": "shutdown"}, key="proposal-2"))
    canceled = request(server, "POST", "/api/control/cancel", payload={"token": proposal2["confirmation_id"]}, key="cancel-2")
    assert decoded(canceled)["status"] == "canceled" and remote.effects == 1


@pytest.mark.parametrize("payload", [{"token": CORE_TOKEN, "action": "shutdown"}, {"token": "x" * 32},
                                      {"token": "a" * 31}, {"token": None}, {"core_token": CORE_TOKEN}])
def test_confirmation_endpoint_has_no_core_token_or_extra_action_channel(remote_phone, payload):
    server, remote = remote_phone
    assert request(server, "POST", "/api/control/confirm", payload=payload)[0] == 400
    assert not remote.calls


def test_response_store_failure_keeps_claim_and_never_blindly_repeats_action(remote_phone, monkeypatch):
    server, remote = remote_phone
    def fail(*_args):
        raise OSError("synthetic storage failure")
    monkeypatch.setattr(server._store, "finish", fail)
    payload = {"kind": "action", "name": "calculator"}
    assert request(server, "POST", payload=payload)[0] == 503
    assert request(server, "POST", payload=payload)[0] == 409
    assert remote.effects == 1 and len(remote.calls) == 1


def test_core_style_token_is_not_a_remote_confirmation_capability(remote_phone):
    server, remote = remote_phone
    response = request(server, "POST", "/api/control/confirm", payload={"token": CORE_TOKEN})
    assert response[0] == 200 and decoded(response)["status"] == "confirmation_refused"
    assert remote.effects == 0


def test_remote_result_containing_a_private_path_is_not_transmitted_or_cached(remote_phone):
    server, remote = remote_phone
    remote.request = lambda _payload, **_kwargs: {**Remote.result(action="calculator"), "text": r"C:\Private\never-show"}
    response = request(server, "POST", payload={"kind": "action", "name": "calculator"})
    assert response[0] == 503 and "Private" not in response[2].decode()


def test_duplicate_control_header_rejected_before_controller_auth(remote_phone):
    server, remote = remote_phone
    connection = HTTPConnection("127.0.0.1", server._server.server_port, timeout=4)
    try:
        connection.putrequest("GET", "/api/control")
        connection.putheader("Authorization", "Bearer " + PHONE_KEY)
        connection.putheader("X-Jarvis-Control", CONTROL_KEY)
        connection.putheader("X-Jarvis-Control", CONTROL_KEY)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()
    assert not remote.auth_calls


def test_pwa_second_key_is_manual_session_only_and_sw_never_caches_control_headers():
    root = Path(__file__).resolve().parents[1] / "desktop" / "phone_web"
    script, worker = (root / "app.js").read_text(encoding="utf-8"), (root / "sw.js").read_text(encoding="utf-8")
    assert 'sessionStorage.getItem(controlKeyName)' in script and 'fragment.get("control")' not in script
    assert "localStorage" not in script and ".innerHTML" not in script and "console.log" not in script
    assert 'headers["X-Jarvis-Control"]' in script and 'event.request.headers.has("X-Jarvis-Control")' in worker


def test_shutdown_waits_for_owned_inflight_adapter_and_prevents_early_restart(remote_phone):
    server, remote = remote_phone
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(4)
    remote.before_mutation = blocked
    def client():
        try:
            request(server, "POST", payload={"kind": "action", "name": "calculator"})
        except Exception:
            # The owned socket is deliberately interrupted on shutdown.
            pass
        finally:
            finished.set()
    thread = threading.Thread(target=client, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        server.stop()
        assert not server.shutdown_complete
        assert not server.start()["running"]
        assert finished.wait(2)
    finally:
        release.set()
        thread.join(3)
    deadline = time.monotonic() + 3
    while not server.shutdown_complete and time.monotonic() < deadline:
        time.sleep(.005)
    assert server.shutdown_complete and remote.effects == 1


def test_shutdown_interrupts_partial_headers_without_parsing_or_executing(remote_phone):
    server, remote = remote_phone
    connection = socket.create_connection(("127.0.0.1", server._server.server_port), timeout=2)
    connection.sendall(b"POST /api/control HTTP/1.1\r\nHost: ")
    active = server._server
    deadline = time.monotonic() + 2
    while active.drained and time.monotonic() < deadline:
        time.sleep(.005)
    assert not active.drained
    try:
        server.stop()
        # Winsock may finish an already-pending buffered recv only when its
        # existing three-second timeout expires, despite shutdown/close.
        deadline = time.monotonic() + 5
        while not server.shutdown_complete and time.monotonic() < deadline:
            time.sleep(.005)
        assert server.shutdown_complete and not remote.calls
    finally:
        connection.close()


@pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="Dedicated browser CI enables Chromium")
async def test_browser_mobile_remote_panel_keeps_keys_and_proxy_private_and_requires_explicit_confirmation(remote_phone):
    from playwright.async_api import async_playwright, expect

    server, remote = remote_phone
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
            await page.goto(base + "/#token=" + PHONE_KEY + "&control=" + CONTROL_KEY)
            await expect(page.locator("#workspace")).to_be_visible()
            assert "#" not in page.url and "?" not in page.url
            assert not remote.auth_calls  # URL input cannot set the second key.
            await page.locator("#control-key").fill(CONTROL_KEY)
            await page.get_by_role("button", name="Открыть управление", exact=True).click()
            await expect(page.locator("#control-workspace")).to_be_visible()
            await expect(page.locator("#control-actions")).to_contain_text(XSS)
            assert await page.locator("#control-actions img").count() == 0
            assert await page.evaluate("window.remoteAttacked === undefined")
            await page.get_by_role("button", name="Выключить · подтверждение", exact=True).click()
            await expect(page.locator("#control-confirmation")).to_be_visible()
            assert remote.effects == 0 and len(remote.calls) == 1
            await page.wait_for_timeout(300)
            assert remote.effects == 0 and len(remote.calls) == 1
            await page.get_by_role("button", name="Подтвердить действие", exact=True).click()
            await expect(page.locator("#control-confirmation")).to_be_hidden()
            await expect(page.locator("#control-notice")).to_have_text("Результат получен от ноутбука.")
            assert remote.effects == 1
            remote.expires_in = 1
            await page.get_by_role("button", name="Выключить · подтверждение", exact=True).click()
            await expect(page.locator("#control-confirmation")).to_be_visible()
            await expect(page.locator("#control-confirmation")).to_be_hidden(timeout=4000)
            assert remote.effects == 1 and len(remote.calls) == 3  # Timer never confirms or cancels.
            await expect(page.locator("#control-notice")).to_contain_text("Срок подтверждения истёк")
            remote.expires_in = 60
            await page.get_by_role("button", name="Выключить · подтверждение", exact=True).click()
            await expect(page.locator("#control-confirmation")).to_be_visible()
            await page.get_by_role("button", name="Отменить", exact=True).click()
            await expect(page.locator("#control-notice")).to_have_text("Результат получен от ноутбука.")
            assert remote.effects == 1 and remote.calls[-1][0] == "cancel"
            stored = await page.evaluate("JSON.stringify({...sessionStorage})")
            assert CORE_TOKEN not in stored and "00000000000000000000000000000001" not in stored
            assert await page.evaluate("Object.keys(localStorage).length") == 0
            await page.evaluate("navigator.serviceWorker.ready")
            await page.evaluate("async (key) => {await fetch('/style.css',{headers:{'X-Jarvis-Control':key},cache:'no-store'});}", CONTROL_KEY)
            cached = await page.evaluate("async()=>{const rows=[];for(const k of await caches.keys()){for(const r of await(await caches.open(k)).keys()){rows.push({url:r.url,key:r.headers.get('X-Jarvis-Control')});}}return rows;}")
            assert cached and all(row["key"] is None and "/api/" not in row["url"] for row in cached)
            await page.get_by_role("button", name="Закрыть управление", exact=True).click()
            assert await page.evaluate("sessionStorage.getItem('jarvis.phone.control')") is None
            assert await page.evaluate("sessionStorage.getItem('jarvis.phone.token')") == PHONE_KEY
            await page.locator("#control-key").fill(CONTROL_KEY)
            await page.get_by_role("button", name="Открыть управление", exact=True).click()
            await expect(page.locator("#control-workspace")).to_be_visible()
            remote.approved = False
            await page.locator("#control-actions button").first.click()
            await expect(page.locator("#control-workspace")).to_be_hidden()
            await expect(page.locator("#control-notice")).to_contain_text("Ключ управления или разрешение")
            assert await page.evaluate("sessionStorage.getItem('jarvis.phone.control')") is None
            assert await page.evaluate("sessionStorage.getItem('jarvis.phone.token')") == PHONE_KEY
            await expect(page.locator("#workspace")).to_be_visible()
            assert remote.effects == 1
            assert not errors
        finally:
            await browser.close()


# Browser-only session races below. The HTTP/controller tests above are shared
# with the parent task; every response/action here stays on disposable loopback.
@pytest.fixture
async def remote_browser(remote_phone):
    from playwright.async_api import async_playwright, expect

    server, remote = remote_phone
    base = server.status()["url"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=os.getenv("TEST_BROWSER_EXECUTABLE") or None)
        context = await browser.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        async def local_only(route):
            if route.request.url.startswith(base + "/"):
                await route.continue_()
            else:
                await route.abort()
        await page.route("**/*", local_only)
        try:
            await page.goto(base + "/#token=" + PHONE_KEY)
            await expect(page.locator("#workspace")).to_be_visible()
            yield page, base, remote
            assert not errors
        finally:
            await browser.close()


async def _browser_connect_control(page):
    from playwright.async_api import expect
    await page.locator("#control-key").fill(CONTROL_KEY)
    await page.get_by_role("button", name="Открыть управление", exact=True).click()
    await expect(page.locator("#control-workspace")).to_be_visible()


def _browser_marker(data, channel, title):
    if channel == "plan":
        return {**data, "today": {**data["today"], "items": [{"id": "goal:1", "title": title, "time": None, "status": "unknown"}]}}
    return {**data, "actions": [{"name": "calculator", "title": title, "requires_confirmation": False}], "projects": [], "apps": []}


@pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="Dedicated browser CI enables Chromium")
@pytest.mark.parametrize("channel,reconnect,outcome", [
    ("plan", False, "success"), ("control", False, "success"),
    ("plan", True, "success"), ("control", True, "success"),
    ("plan", True, "401"), ("control", True, "401"), ("control", True, "403"),
    ("plan", True, "network"), ("control", True, "network"),
])
async def test_browser_late_get_cannot_restore_disconnected_data_or_clear_repaired_session(remote_browser, channel, reconnect, outcome):
    import asyncio
    from playwright.async_api import expect

    page, base, remote = remote_browser
    if channel == "control":
        await _browser_connect_control(page)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []
    async def delayed(route):
        response = await route.fetch()
        data = await response.json()
        requests.append(route.request.headers)
        if len(requests) == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=8)
            try:
                if outcome == "network":
                    await route.abort("failed")
                elif outcome in {"401", "403"}:
                    await route.fulfill(status=int(outcome), json={"error": "authorization_required" if outcome == "401" else "control_authorization_required"})
                else:
                    await route.fulfill(response=response, json=_browser_marker(data, channel, "OLD_PRIVATE_RESPONSE"))
            finally:
                finished.set()
        else:
            await route.fulfill(response=response, json=_browser_marker(data, channel, "FRESH_PRIVATE_RESPONSE"))
    await page.route(base + "/api/" + channel, delayed)
    try:
        await page.locator("#refresh" if channel == "plan" else "#control-refresh").click()
        await asyncio.wait_for(entered.wait(), timeout=3)
        await page.locator("#disconnect" if channel == "plan" else "#control-disconnect").click()
        if reconnect:
            if channel == "plan":
                await page.locator("#token").fill(PHONE_KEY)
                await page.get_by_role("button", name="Подключиться", exact=True).click()
                await expect(page.locator("#items")).to_contain_text("FRESH_PRIVATE_RESPONSE")
            else:
                await _browser_connect_control(page)
                await expect(page.locator("#control-actions")).to_contain_text("FRESH_PRIVATE_RESPONSE")
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=3)
        await page.wait_for_timeout(100)
        assert "OLD_PRIVATE_RESPONSE" not in await page.locator("body").inner_text()
        if reconnect:
            await expect(page.locator("#workspace")).to_be_visible()
            assert await page.evaluate("sessionStorage.getItem('jarvis.phone.token')") == PHONE_KEY
            if channel == "control":
                await expect(page.locator("#control-workspace")).to_be_visible()
                assert await page.evaluate("sessionStorage.getItem('jarvis.phone.control')") == CONTROL_KEY
            await expect(page.locator("#notice" if channel == "plan" else "#control-notice")).not_to_have_class("notice error")
        elif channel == "plan":
            await expect(page.locator("#workspace")).to_be_hidden()
            assert await page.locator("#items").inner_text() == ""
            assert await page.evaluate("Object.keys(sessionStorage).length") == 0
        else:
            await expect(page.locator("#control-workspace")).to_be_hidden()
            assert await page.locator("#control-actions").inner_text() == ""
            assert await page.evaluate("sessionStorage.getItem('jarvis.phone.control')") is None
            assert await page.evaluate("sessionStorage.getItem('jarvis.phone.token')") == PHONE_KEY
        assert len(requests) == (2 if reconnect else 1) and remote.calls == []
        assert requests[0]["authorization"] == "Bearer " + PHONE_KEY
        if channel == "control":
            assert requests[0]["x-jarvis-control"] == CONTROL_KEY
    finally:
        release.set()


@pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="Dedicated browser CI enables Chromium")
@pytest.mark.parametrize("channel", ["plan", "control"])
async def test_browser_disconnect_while_json_body_pending_never_renders_private_data(remote_browser, channel):
    from playwright.async_api import expect

    page, _base, _remote = remote_browser
    if channel == "control":
        await _browser_connect_control(page)
    await page.evaluate("""(channel) => {
      const fetchOriginal = window.fetch;
      window.bodyPending = false;
      let first = true;
      window.fetch = async (...args) => {
        const response = await fetchOriginal(...args);
        if (first && args[0] === '/api/' + channel) {
          first = false;
          const jsonOriginal = response.json.bind(response);
          response.json = async () => {
            const data = await jsonOriginal();
            window.bodyPending = true;
            await new Promise(resolve => { window.releaseBody = resolve; });
            window.bodyReleased = true;
            return data;
          };
        }
        return response;
      };
    }""", channel)
    await page.locator("#refresh" if channel == "plan" else "#control-refresh").click()
    await page.wait_for_function("() => window.bodyPending === true")
    await page.locator("#disconnect" if channel == "plan" else "#control-disconnect").click()
    await page.evaluate("window.releaseBody()")
    await page.wait_for_function("() => window.bodyReleased === true")
    await page.wait_for_timeout(100)
    await expect(page.locator("#workspace" if channel == "plan" else "#control-workspace")).to_be_hidden()
    assert await page.locator("#items" if channel == "plan" else "#control-actions").inner_text() == ""
    assert await page.evaluate("(key) => sessionStorage.getItem(key)", "jarvis.phone.token" if channel == "plan" else "jarvis.phone.control") is None


@pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="Dedicated browser CI enables Chromium")
@pytest.mark.parametrize("channel", ["plan", "control"])
async def test_browser_old_post_cannot_clear_new_draft_spawn_confirmation_or_unlock_new_request(remote_browser, channel):
    import asyncio
    from playwright.async_api import expect

    page, base, remote = remote_browser
    if channel == "control":
        await _browser_connect_control(page)
    entered = [asyncio.Event(), asyncio.Event()]
    releases = [asyncio.Event(), asyncio.Event()]
    completed = [asyncio.Event(), asyncio.Event()]
    posts, get_count = [], []
    async def delayed(route):
        if route.request.method == "GET":
            get_count.append(True)
            await route.continue_()
            return
        index = len(posts)
        posts.append(route.request.post_data_json)
        response = await route.fetch()
        entered[index].set()
        try:
            await asyncio.wait_for(releases[index].wait(), timeout=8)
            await route.fulfill(response=response)
        finally:
            completed[index].set()
    await page.route(base + "/api/" + channel, delayed)
    async def submit(index):
        if channel == "control":
            await page.get_by_role("button", name="Выключить · подтверждение" if index == 0 else XSS, exact=True).click()
        else:
            await page.locator("#plan-title").fill("Запрос " + str(index))
            await page.get_by_role("button", name="Добавить в план", exact=True).click()
    try:
        await submit(0)
        await asyncio.wait_for(entered[0].wait(), timeout=3)
        await page.locator("#disconnect" if channel == "plan" else "#control-disconnect").click()
        await expect(page.locator("#notice" if channel == "plan" else "#control-notice")).to_contain_text("мог")
        if channel == "control":
            await _browser_connect_control(page)
        else:
            await page.locator("#token").fill(PHONE_KEY)
            await page.get_by_role("button", name="Подключиться", exact=True).click()
            await expect(page.locator("#workspace")).to_be_visible()
        await submit(1)
        await asyncio.wait_for(entered[1].wait(), timeout=3)
        await page.locator("#plan-title").fill("НОВЫЙ НЕСОХРАНЁННЫЙ ЧЕРНОВИК")
        before_gets = len(get_count)
        releases[0].set()
        await asyncio.wait_for(completed[0].wait(), timeout=3)
        await page.wait_for_timeout(100)
        await expect(page.get_by_role("button", name="Добавить в план", exact=True)).to_be_disabled()
        await expect(page.locator("#control-confirmation")).to_be_hidden()
        assert await page.locator("#plan-title").input_value() == "НОВЫЙ НЕСОХРАНЁННЫЙ ЧЕРНОВИК"
        assert len(get_count) == before_gets and len(posts) == 2
        releases[1].set()
        await asyncio.wait_for(completed[1].wait(), timeout=3)
        await expect(page.get_by_role("button", name="Добавить в план", exact=True)).to_be_enabled()
        await expect(page.locator("#control-confirmation")).to_be_hidden()
        assert len(posts) == 2
        if channel == "control":
            assert remote.effects == 1 and len(remote.calls) == 2
        assert await page.evaluate("sessionStorage.getItem('jarvis.phone.token')") == PHONE_KEY
    finally:
        for release in releases:
            release.set()
