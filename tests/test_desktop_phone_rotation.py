"""Real loopback transport/controller with synthetic credentials and no PC I/O.

Withhold complete POST bodies until after authentication, then rotate/revoke
configuration. All action callbacks record strings only; planner data lives in
pytest's private temporary directory. No external service or profile is used.
"""
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import threading
import time
from types import SimpleNamespace

import pytest

from desktop.core import Reply
from desktop.organizer import LocalOrganizer
from desktop.phone import OrganizerPhoneAdapter, PhoneServer
from desktop.remote_control import RemoteController
from desktop.store import LocalStore


PHONE = "Synthetic_Phone_Secret_0123456789_AbCdEfGhIjKl"
CONTROL = "Synthetic_Control_Old_0123456789_AbCdEfGhIjKl"
NEW_PHONE = "Synthetic_Phone_New_9876543210_ZyXwVuTsRqPo"
NEW_CONTROL = "Synthetic_Control_New_9876543210_ZyXwVuTsRqPo"
CORE_TOKEN = "b" * 32
ORIGIN = "https://synthetic.review.ts.net"


@pytest.fixture
def live(tmp_path):
    env = {"JARVIS_PHONE_ENABLED": "1", "JARVIS_PHONE_TOKEN": PHONE,
           "JARVIS_REMOTE_ENABLED": "1", "JARVIS_REMOTE_TOKEN": CONTROL,
           "JARVIS_PHONE_ORIGIN": ORIGIN}
    store, effects, approved, servers = LocalStore(tmp_path / "organizer"), [], [True], []
    organizer = LocalOrganizer(store)
    phone_auth, control_auth = threading.Event(), threading.Event()

    def action(name, token=None, cancel=False):
        if name == "shutdown" and token is None:
            return Reply("Synthetic proposal only.", action=name, confirmation_id=CORE_TOKEN)
        if token is not None:
            assert token == CORE_TOKEN
        if not cancel:
            effects.append(name)
        return Reply("Synthetic callback only.", action=name)

    controller = RemoteController(tmp_path / "phone", environ=env, approved=lambda: approved[0],
                                  action_handler=action)
    authorize = controller.authorize
    def observed_control(key):
        accepted = authorize(key)
        if accepted:
            control_auth.set()
        return accepted
    controller.authorize = observed_control

    def new_server():
        server = PhoneServer(OrganizerPhoneAdapter(organizer), tmp_path / "phone", port=0,
                             environ=env, remote=controller)
        handler_factory = server._handler
        def handler():
            value = handler_factory()
            original = value._authorized
            def observed_phone(connection):
                accepted = original(connection)
                if accepted:
                    phone_auth.set()
                return accepted
            value._authorized = observed_phone
            return value
        server._handler = handler
        assert server.start()["running"]
        servers.append(server)
        return server

    server = new_server()
    try:
        yield SimpleNamespace(server=server, env=env, approved=approved, effects=effects,
                              organizer=organizer, phone_auth=phone_auth, control_auth=control_auth,
                              controller=controller, new_server=new_server)
    finally:
        for owned in servers:
            owned.stop()
        deadline = time.monotonic() + 3
        while not all(owned.shutdown_complete for owned in servers) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert all(owned.shutdown_complete for owned in servers)
        store.close()


def get(server, path="/api/control", *, phone=PHONE, control=CONTROL):
    connection = HTTPConnection("127.0.0.1", server._server.server_port, timeout=3)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + phone, "X-Jarvis-Control": control})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@contextmanager
def withheld_post(live, path="/api/control", payload=None, *, key="pending-rotation"):
    """Both headers are verified while the authorized body is still absent."""
    control = path.startswith("/api/control")
    event = live.control_auth if control else live.phone_auth
    event.clear()
    connection = HTTPConnection("127.0.0.1", live.server._server.server_port, timeout=3)
    body = json.dumps(payload or {"kind": "action", "name": "calculator"}).encode()
    try:
        connection.putrequest("POST", path)
        connection.putheader("Authorization", "Bearer " + PHONE)
        if control:
            connection.putheader("X-Jarvis-Control", CONTROL)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("Idempotency-Key", key)
        connection.endheaders()
        assert event.wait(2), "Synthetic request was not authenticated before the boundary"
        def finish():
            connection.send(body)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        yield finish
    finally:
        connection.close()


def post(live, path, payload, *, key="fresh-operation"):
    with withheld_post(live, path, payload, key=key) as finish:
        return finish()


@pytest.mark.parametrize("field,value,status", [
    ("JARVIS_REMOTE_TOKEN", NEW_CONTROL, 403), ("JARVIS_PHONE_TOKEN", NEW_PHONE, 401),
    ("JARVIS_PHONE_ORIGIN", "https://changed.review.ts.net", 401),
    ("JARVIS_PHONE_ENABLED", "0", 401), ("JARVIS_REMOTE_ENABLED", "0", 403),
])
def test_old_authorized_pending_control_body_cannot_execute_after_rotation(live, field, value, status):
    with withheld_post(live) as finish:
        live.env[field] = value
        code, body = finish()
    assert code == status and body.get("error") in {"authorization_required", "control_authorization_required"}
    assert live.effects == []


def test_durable_pairing_revocation_blocks_a_previously_authenticated_body(live):
    with withheld_post(live) as finish:
        live.approved[0] = False
        status, _body = finish()
    assert status == 403 and not live.effects


@pytest.mark.parametrize("route", ["/api/control/confirm", "/api/control/cancel"])
def test_pending_confirmation_or_cancel_is_bound_to_its_presented_control_key(live, route):
    status, proposal = post(live, "/api/control", {"kind": "action", "name": "shutdown"}, key="proposal")
    challenge = proposal["confirmation_id"]
    assert status == 200 and challenge != CORE_TOKEN and live.effects == []
    with withheld_post(live, route, {"token": challenge}, key="pending-confirmation") as finish:
        live.env["JARVIS_REMOTE_TOKEN"] = NEW_CONTROL
        code, _body = finish()
    assert code == 403 and live.effects == []
    result = live.controller.confirm(challenge, control_token=NEW_CONTROL)
    assert result["status"] in {"confirmation_refused", "result_uncertain"} and not live.effects


@pytest.mark.parametrize("field,value", [("JARVIS_PHONE_TOKEN", NEW_PHONE),
                                       ("JARVIS_PHONE_ORIGIN", "https://changed.review.ts.net"),
                                       ("JARVIS_PHONE_ENABLED", "0")])
def test_previously_authenticated_plan_write_rechecks_live_phone_configuration(live, field, value):
    payload = {"date_key": live.organizer.day()["date"], "title": "Synthetic delayed plan item"}
    with withheld_post(live, "/api/plan", payload) as finish:
        live.env[field] = value
        status, _body = finish()
    assert status == 401 and live.organizer.day()["items"] == [] and not live.effects


@pytest.mark.parametrize("field,value", [("JARVIS_PHONE_TOKEN", NEW_PHONE),
                                       ("JARVIS_PHONE_ORIGIN", "https://changed.review.ts.net"),
                                       ("JARVIS_PHONE_ENABLED", "0")])
def test_new_requests_cannot_continue_using_an_old_phone_server_configuration(live, field, value):
    live.env[field] = value
    assert get(live.server, "/api/plan")[0] == 401
    assert get(live.server, "/api/plan", phone=NEW_PHONE)[0] == 401
    assert get(live.server)[0] == 401 and not live.effects


def test_control_key_rotation_preserves_planner_but_requires_new_control_key(live):
    live.env["JARVIS_REMOTE_TOKEN"] = NEW_CONTROL
    assert get(live.server, "/api/plan")[0] == 200
    assert get(live.server)[0] == 403
    assert get(live.server, control=NEW_CONTROL)[0] == 200
    assert not live.effects


def test_http_binds_presented_control_key_even_if_rotation_occurs_after_last_header_check(live):
    original = live.controller.request
    presented = []
    def rotate_at_controller_entry(payload, *, control_token=None):
        presented.append(control_token)
        live.env["JARVIS_REMOTE_TOKEN"] = NEW_CONTROL
        return original(payload, control_token=control_token)
    live.controller.request = rotate_at_controller_entry
    status, result = post(live, "/api/control", {"kind": "action", "name": "calculator"})
    assert status == 200 and result["ok"] is False and result["status"] == "authorization_refused"
    assert presented == [CONTROL] and not live.effects


def test_phone_token_rotation_requires_new_owned_server_and_rejects_old_bearer(live):
    live.env["JARVIS_PHONE_TOKEN"] = NEW_PHONE
    assert get(live.server, "/api/plan")[0] == 401
    live.server.stop()
    replacement = live.new_server()
    assert get(replacement, "/api/plan")[0] == 401
    assert get(replacement, "/api/plan", phone=NEW_PHONE)[0] == 200
    assert not live.effects


def test_nonblocking_admission_revocation_does_not_wait_for_provider_or_server_shutdown(live):
    serving, thread = live.server._server, live.server._thread
    live.server.prevent_requests()
    assert live.server._server is serving and thread.is_alive()  # Provider cleanup can still be busy.
    assert not live.server.shutdown_complete
    assert get(live.server, "/api/plan")[0] == 503
    assert get(live.server)[0] == 503
    assert live.organizer.day()["items"] == [] and not live.effects


@pytest.mark.parametrize("route", ["/api/control", "/api/plan"])
def test_admission_revocation_blocks_already_authenticated_bodies_while_cleanup_is_pending(live, route):
    payload = ({"kind": "action", "name": "calculator"} if route == "/api/control" else
               {"date_key": live.organizer.day()["date"], "title": "Synthetic delayed plan item"})
    with withheld_post(live, route, payload) as finish:
        live.server.prevent_requests()
        status, _body = finish()
    assert status == 503 and not live.effects and live.organizer.day()["items"] == []
