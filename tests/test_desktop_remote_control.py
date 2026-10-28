"""Finite remote control with synthetic callbacks; no actual OS/network calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import struct
from types import SimpleNamespace

import pytest

from desktop import remote_control as remote


CONTROL = "SyntheticRemoteToken_0123456789_AbCdEfGhIjKlMn"
PHONE = "DifferentPhoneToken_0123456789_ZyXwVuTsRqPoNm"
ENV = {"JARVIS_REMOTE_ENABLED": "1", "JARVIS_REMOTE_TOKEN": CONTROL,
       "JARVIS_PHONE_TOKEN": PHONE, "JARVIS_PHONE_ORIGIN": "https://jarvis.private.ts.net"}
CORE_TOKEN = "b" * 32


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def controller(tmp_path):
    calls, clock, approval, env = [], Clock(), [True], dict(ENV)

    def action_handler(action, token=None, cancel=False):
        calls.append((action, token, cancel))
        return SimpleNamespace(text="Запрос отменён." if cancel else "Ответ разрешённого обработчика.",
            action=None if cancel else action,
            confirmation_id=CORE_TOKEN if action in remote.RISKY_ACTIONS and token is None else None)

    instance = remote.RemoteController(tmp_path / "remote", action_handler=action_handler,
        approved=lambda: approval[0], environ=env, clock=clock)
    return instance, calls, clock, approval, env


@pytest.mark.parametrize("changes", [{"JARVIS_REMOTE_ENABLED": None}, {"JARVIS_REMOTE_ENABLED": "0"},
    {"JARVIS_REMOTE_TOKEN": None}, {"JARVIS_REMOTE_TOKEN": "x" * 40}, {"JARVIS_REMOTE_TOKEN": PHONE},
    {"JARVIS_PHONE_TOKEN": None}, {"JARVIS_PHONE_ORIGIN": None},
    {"JARVIS_PHONE_ORIGIN": "http://jarvis.private.ts.net"},
    {"JARVIS_PHONE_ORIGIN": "https://jarvis.private.ts.net/"},
    {"JARVIS_PHONE_ORIGIN": "https://user:secret@jarvis.private.ts.net"},
    {"JARVIS_PHONE_ORIGIN": "https://jarvis.private.ts.net?secret=yes"},
    {"JARVIS_PHONE_ORIGIN": "https://jarvis.private.ts.net/#token"},
    {"JARVIS_PHONE_ORIGIN": "https://jarvis.private.ts.net:"},
    {"JARVIS_PHONE_ORIGIN": "https://jarvis..ts.net"}])
def test_absent_or_partial_configuration_is_off_without_files_or_callbacks(controller, changes):
    instance, calls, _, _, env = controller
    env.update(changes)
    assert not instance.authorize(CONTROL)
    assert instance.status()["reason"] == "not_configured"
    assert instance.snapshot()["actions"] == []
    assert instance.request({"kind": "action", "name": "shutdown"})["status"] == "not_configured"
    assert instance.confirm("a" * 32)["status"] == "not_configured"
    assert instance.cancel("a" * 32)["status"] == "not_configured"
    assert not instance.path.parent.exists() and not calls


def test_distinct_control_secret_and_current_owner_approval_are_both_required(controller):
    instance, calls, _, approval, _ = controller
    assert instance.authorize(CONTROL)
    for value in (PHONE, "bad-token", None, b"bytes", "x" * 10000):
        assert not instance.authorize(value)
    approval[0] = False
    assert not instance.authorize(CONTROL)
    assert instance.snapshot()["actions"] == []
    assert instance.request({"kind": "action", "name": "calculator"})["status"] == "approval_required"
    assert not calls and not instance.path.parent.exists()


def test_status_and_snapshot_expose_finite_names_not_secrets_or_paths(controller):
    instance, _, _, _, _ = controller
    snapshot = instance.snapshot()
    assert snapshot["available"] and snapshot["approved"] and snapshot["configured"]
    assert {item["name"] for item in snapshot["actions"]} == remote._ACTIONS
    assert next(item for item in snapshot["actions"] if item["name"] == "shutdown")["requires_confirmation"] is True
    assert CONTROL not in json.dumps(snapshot) and PHONE not in json.dumps(instance.status())
    assert snapshot["projects"] == snapshot["apps"] == []


@pytest.mark.parametrize("payload", [None, [], {}, {"kind": "action", "name": "calculator", "args": []},
    {"kind": "action", "name": "powershell"}, {"kind": "action", "name": "shutdown /s"},
    {"kind": "action", "name": ["calculator"]}, {"kind": "shell", "name": "calculator"},
    {"kind": "app", "name": "C:\\Windows\\cmd.exe"}, {"kind": "project", "name": "../secret"}])
def test_payload_is_closed_union_and_never_routes_free_text_or_paths(controller, payload):
    instance, calls, _, _, _ = controller
    assert not instance.request(payload)["ok"]
    assert not calls


def test_low_risk_action_calls_once_and_reports_literal_bounded_reply(controller):
    instance, calls, _, _, _ = controller
    result = instance.request({"kind": "action", "name": "calculator"})
    assert result == {"ok": True, "status": "reported", "text": "Ответ разрешённого обработчика.",
        "action": "calculator", "confirmation_id": None, "expires_in": 0}
    assert calls == [("calculator", None, False)] and not instance.path.exists()


def test_risky_action_proxy_is_channel_bound_and_sqlite_never_stores_tokens(controller):
    instance, calls, _, _, _ = controller
    result = instance.request({"kind": "action", "name": "shutdown"})
    token = result["confirmation_id"]
    assert result["status"] == "confirmation_required" and result["expires_in"] == 60
    assert token != CORE_TOKEN and len(token) == 32
    assert calls == [("shutdown", None, False)]
    assert instance.confirm(CORE_TOKEN)["status"] == "confirmation_refused"
    with sqlite3.connect(instance.path) as database:
        serialized = "\n".join(database.iterdump())
    assert all(value not in serialized for value in (token, CORE_TOKEN, CONTROL, PHONE))
    assert hashlib.sha256(token.encode()).hexdigest() in serialized
    assert hashlib.sha256(CONTROL.encode()).hexdigest() in serialized
    result = instance.confirm(token)
    assert result["ok"] and result["status"] == "reported"
    assert calls[-1] == ("shutdown", CORE_TOKEN, False)
    assert instance.confirm(token)["status"] == instance.cancel(token)["status"] == "confirmation_refused"
    assert len(calls) == 2


def test_cancel_consumes_once_and_only_for_its_bound_action(controller):
    instance, calls, _, _, _ = controller
    token = instance.request({"kind": "action", "name": "restart"})["confirmation_id"]
    assert instance.cancel(token)["status"] == "canceled"
    assert calls[-1] == ("restart", CORE_TOKEN, True)
    assert instance.confirm(token)["status"] == "confirmation_refused"
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["expiry", "rollback", "rotation", "approval"])
def test_expiry_clock_rollback_token_rotation_or_owner_revocation_cannot_reuse_challenge(controller, change):
    instance, calls, clock, approval, env = controller
    token = instance.request({"kind": "action", "name": "lock"})["confirmation_id"]
    if change == "expiry":
        clock.now += 60
    elif change == "rollback":
        clock.now -= 1
    elif change == "rotation":
        env["JARVIS_REMOTE_TOKEN"] = "RotatedOwnerToken_0123456789_XxYyZz_abcdef"
    else:
        approval[0] = False
        assert instance.confirm(token)["status"] == "approval_required"
        approval[0] = True
    assert not instance.confirm(token)["ok"]
    assert calls == [("lock", None, False)]


def test_process_restart_cannot_revive_unfinished_core_token(controller):
    instance, calls, clock, _, env = controller
    token = instance.request({"kind": "action", "name": "hibernate"})["confirmation_id"]
    restarted = remote.RemoteController(instance.state_dir, action_handler=lambda *_a, **_kw: pytest.fail("No persisted core token"),
        approved=lambda: True, environ=env, clock=clock)
    assert restarted.confirm(token)["status"] == "result_uncertain"
    assert instance.confirm(token)["status"] == "confirmation_refused"
    assert len(calls) == 1


def test_modified_persisted_expiry_cannot_extend_memory_bound_challenge_lifetime(controller):
    instance, calls, clock, _, _ = controller
    token = instance.request({"kind": "action", "name": "shutdown"})["confirmation_id"]
    clock.now += 61
    with sqlite3.connect(instance.path) as database:
        database.execute("UPDATE remote_challenges SET issued=?, expires=?", (clock.now, clock.now + 60))
    assert instance.confirm(token)["status"] == "result_uncertain"
    assert calls == [("shutdown", None, False)]


def test_concurrent_confirmations_durably_claim_before_one_callback(controller):
    instance, calls, _, _, _ = controller
    token = instance.request({"kind": "action", "name": "shutdown"})["confirmation_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: instance.confirm(token), range(4)))
    assert sum(result["ok"] for result in results) == 1
    assert calls == [("shutdown", None, False), ("shutdown", CORE_TOKEN, False)]


def test_uncertain_callback_is_not_retried_and_claim_was_committed_first(controller):
    instance, calls, _, _, _ = controller
    token = instance.request({"kind": "action", "name": "shutdown"})["confirmation_id"]

    def broken(action, token=None, cancel=False):
        with sqlite3.connect(instance.path) as database:
            assert database.execute("SELECT consumed FROM remote_challenges").fetchone()[0] == 1
        calls.append((action, token, cancel))
        raise OSError("private secret path")

    instance.action_handler = broken
    result = instance.confirm(token)
    assert result["status"] == "result_uncertain" and "private" not in json.dumps(result)
    assert instance.confirm(token)["status"] == "confirmation_refused"
    assert len(calls) == 2


@pytest.mark.parametrize("action,challenge", [("restart", CORE_TOKEN), ("shutdown", None), ("shutdown", "not-a-token")])
def test_risky_handler_must_propose_matching_action_and_valid_core_challenge(controller, action, challenge):
    instance, calls, _, _, _ = controller
    instance.action_handler = lambda name: calls.append(name) or {"action": action, "confirmation_id": challenge, "text": "Executed"}
    result = instance.request({"kind": "action", "name": "shutdown"})
    assert result["status"] == "result_uncertain" and not result["ok"]
    assert result["confirmation_id"] is None and calls == ["shutdown"]


def test_catalogs_use_exact_approved_aliases_editor_policy_and_never_expose_paths(controller):
    instance, calls, _, _, _ = controller
    row = {"name": "Мой проект", "aliases": ["алгоритмы"], "path": "C:\\Private\\secret", "available": True}
    opened = []
    instance.project_catalog = SimpleNamespace(projects=lambda: [row],
        resolve=lambda name: {"status": "found" if name in (row["name"], *row["aliases"]) else "ambiguous"},
        open_project=lambda name, editor, authorization_guard: opened.append((name, editor)) or {"status": "requested" if authorization_guard() else "denied", "project": row, "message": "Открываю проект."})
    instance.app_catalog = SimpleNamespace(apps=lambda: [{"name": "Редактор", "aliases": [], "path": "C:\\Secret.exe"}],
        resolve=lambda name: {"status": "found" if name == "Редактор" else "missing"},
        open_app=lambda name, authorization_guard: opened.append(name) or {"status": "requested" if authorization_guard() else "denied", "app": row, "message": "Запускаю редактор."})
    snapshot = instance.snapshot()
    assert snapshot["projects"] == [{"name": "Мой проект", "aliases": ["алгоритмы"]}]
    assert snapshot["apps"] == [{"name": "Редактор", "aliases": []}]
    assert "Private" not in json.dumps(snapshot) and "path" not in json.dumps(snapshot)
    assert instance.request({"kind": "project", "name": "алгоритмы"})["status"] == "requested"
    assert opened == [("алгоритмы", "explorer")]
    instance.project_editor = None
    assert instance.request({"kind": "project", "name": "Мой проект"})["ok"]
    assert opened[-1] == ("Мой проект", None)
    assert instance.request({"kind": "project", "name": "Мой"})["status"] == "alias_refused"
    assert instance.request({"kind": "app", "name": "Редактор"})["ok"]
    assert opened[-1] == "Редактор" and not calls


def test_approval_is_checked_again_after_resolving_alias(controller):
    instance, calls, _, approval, _ = controller

    def resolve(_):
        approval[0] = False
        return {"status": "found"}

    instance.app_catalog = SimpleNamespace(resolve=resolve, open_app=lambda _: pytest.fail("Owner revoked"))
    assert instance.request({"kind": "app", "name": "Редактор"})["status"] == "approval_required"
    assert not calls


def test_callback_output_projects_only_reply_fields_and_hides_private_paths(controller):
    instance, _, _, _, _ = controller
    instance.action_handler = lambda action: {"action": action, "text": "Error C:\\Private\\secret.env",
        "confirmation_id": None, "private": CONTROL}
    result = instance.request({"kind": "action", "name": "calculator"})
    assert result["text"] == remote._MESSAGES["reported"]
    assert CONTROL not in json.dumps(result) and "Private" not in json.dumps(result)


def test_confirmation_store_failure_never_exposes_a_usable_remote_challenge(controller, monkeypatch):
    instance, calls, _, _, _ = controller
    monkeypatch.setattr(instance, "_database", lambda: (_ for _ in ()).throw(OSError("secret")))
    result = instance.request({"kind": "action", "name": "shutdown"})
    assert result["status"] == "store_unavailable" and result["confirmation_id"] is None
    assert calls == [("shutdown", None, False)]


@pytest.mark.parametrize("field,value", [("JARVIS_PHONE_TOKEN", "RotatedPhoneToken_0123456789_QqWwEeRrTtYy"),
                                         ("JARVIS_PHONE_ORIGIN", "https://new.private.ts.net")])
def test_phone_secret_or_origin_rotation_invalidates_pending_challenge(controller, field, value):
    instance, calls, _, _, env = controller
    token = instance.request({"kind": "action", "name": "shutdown"})["confirmation_id"]
    env[field] = value
    assert instance.status()["available"]
    assert not instance.confirm(token)["ok"]
    assert instance.confirm(token)["status"] == "confirmation_refused"
    assert calls == [("shutdown", None, False)]


def test_remote_project_guard_rechecks_pairing_after_editor_verification(controller, tmp_path, monkeypatch):
    from desktop import project_catalog
    instance, calls, _, approval, _ = controller
    root = tmp_path / "Projects"
    folder = root / "Saved"
    folder.mkdir(parents=True)
    catalog = project_catalog.ProjectCatalog(tmp_path / "catalog")
    catalog.register_root(root)
    catalog.add_project(folder)
    instance.project_catalog, instance.project_editor = catalog, "idea"
    system = tmp_path / "Windows/System32"
    system.mkdir(parents=True)
    executable = tmp_path / "idea64.exe"
    executable.write_bytes(b"fixture never executed")
    monkeypatch.setattr(project_catalog, "_system_directory", lambda: system)
    def revoked(*_args):
        approval[0] = False
        return executable
    monkeypatch.setattr(project_catalog, "_verified_jetbrains", revoked)
    monkeypatch.setattr(project_catalog.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("Revoked remote launch"))
    result = instance.request({"kind": "project", "name": "Saved"})
    assert result["status"] == "approval_required" and not result["ok"] and calls == []


def test_remote_app_guard_rechecks_credential_identity_after_hashing(controller, tmp_path, monkeypatch):
    from desktop import app_catalog
    instance, calls, _, _, env = controller
    executable = tmp_path / "Useful.exe"
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    struct.pack_into("<4sHHIIIHH", data, 128, b"PE\x00\x00", 0x8664, 1, 0, 0, 0, 240, 2)
    struct.pack_into("<H", data, 152, 0x20B)
    executable.write_bytes(data)
    catalog = app_catalog.AppCatalog(tmp_path / "catalog", inspector=lambda _: "Useful.exe",
                                     executor=lambda *_: pytest.fail("Rotated credential launch"))
    catalog.register_app(executable, "Useful", approval=True)
    instance.app_catalog = catalog
    original = app_catalog._fingerprint
    def rotate(path):
        value = original(path)
        env["JARVIS_REMOTE_TOKEN"] = "RotatedRemoteToken_0123456789_QqWwEeRrTtYy"
        return value
    monkeypatch.setattr(app_catalog, "_fingerprint", rotate)
    result = instance.request({"kind": "app", "name": "Useful"})
    assert result["status"] == "approval_required" and not result["ok"] and calls == []


def test_remote_pairing_does_not_expire_with_transient_local_hello(controller):
    instance, _, _, approval, _ = controller
    local_hello = SimpleNamespace(allowed=lambda: False)
    opened = []
    def open_app(name, authorization_guard):
        assert local_hello.allowed() is False and authorization_guard() is True
        opened.append(name)
        return {"status": "requested", "message": "Запрос открытия передан."}
    instance.app_catalog = SimpleNamespace(resolve=lambda _: {"status": "found"}, open_app=open_app)
    assert approval[0] and instance.request({"kind": "app", "name": "Useful"})["status"] == "requested"
    assert opened == ["Useful"]


def test_removed_catalog_item_is_not_misreported_as_lost_remote_pairing(controller):
    instance, _, _, _, _ = controller
    instance.app_catalog = SimpleNamespace(resolve=lambda _: {"status": "found"},
        open_app=lambda _name, authorization_guard: {"status": "denied", "message": "Item removed"})
    result = instance.request({"kind": "app", "name": "Useful"})
    assert result["status"] == "alias_refused" and instance.status()["available"]


def test_presented_old_key_cannot_bind_itself_to_new_configuration_after_body_delay(controller):
    instance, calls, _, _, env = controller
    assert instance.authorize(CONTROL)
    env["JARVIS_REMOTE_TOKEN"] = "RotatedRemoteToken_0123456789_QqWwEeRrTtYy"
    result = instance.request({"kind": "action", "name": "calculator"}, control_token=CONTROL)
    assert result["status"] == "authorization_refused" and not result["ok"] and calls == []
    assert not instance.path.exists()


@pytest.mark.parametrize("operation", ["confirm", "cancel"])
def test_wrong_presented_key_cannot_consume_confirmation_or_call_handler(controller, operation):
    instance, calls, _, _, _ = controller
    token = instance.request({"kind": "action", "name": "shutdown"}, control_token=CONTROL)["confirmation_id"]
    assert getattr(instance, operation)(token, control_token=PHONE)["status"] == "authorization_refused"
    with sqlite3.connect(instance.path) as database:
        assert database.execute("SELECT consumed FROM remote_challenges").fetchone()[0] == 0
    assert calls == [("shutdown", None, False)]
    assert instance.confirm(token, control_token=CONTROL)["ok"]


@pytest.mark.parametrize("token", ["bad", b"bytes", True, [], PHONE])
def test_invalid_presented_remote_key_has_no_callbacks_or_writes(controller, token):
    instance, calls, _, _, _ = controller
    result = instance.request({"kind": "action", "name": "shutdown"}, control_token=token)
    assert result["status"] == "authorization_refused" and not instance.path.exists() and calls == []
