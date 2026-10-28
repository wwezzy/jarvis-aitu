"""Owner gating and fake native ABI; never displays actual Windows Hello UI."""
import ctypes
from contextlib import contextmanager
from types import SimpleNamespace
import uuid

import pytest

from desktop import owner_auth as auth


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def hello(monkeypatch):
    calls = []
    native = SimpleNamespace(availability_code=0, verify_code=0)

    class Native:
        def __enter__(self):
            calls.append("open")
            return self

        def __exit__(self, *_):
            calls.append("close")

        def availability(self, timeout):
            calls.append(("availability", timeout))
            return native.availability_code

        def verify(self, hwnd, cancel, timeout):
            calls.append(("verify", hwnd, timeout))
            if isinstance(native.verify_code, Exception):
                raise native.verify_code
            return native.verify_code

    monkeypatch.setattr(auth, "_supported", lambda: True)
    return auth.WindowsHello(_native_factory=Native), native, calls


def test_availability_is_passive_and_not_owner_verification(hello):
    verifier, _, calls = hello
    result = verifier.availability()
    assert result == auth.AuthResult(True, "available", auth._MESSAGES["available"])
    assert calls == ["open", ("availability", 5), "close"]
    session = auth.OwnerSession(True, verifier=SimpleNamespace(verify=lambda *_a, **_kw: result), session_check=lambda: True)
    assert not session.verify(123).success and not session.allowed()


@pytest.mark.parametrize("code,reason", [(0, "verified"), (1, "device_missing"), (2, "not_configured"),
    (3, "policy_disabled"), (4, "busy"), (5, "retries_exhausted"), (6, "canceled"),
    (77, "unavailable"), (False, "unavailable"), (None, "unavailable")])
def test_only_native_integer_verified_zero_succeeds(hello, code, reason):
    verifier, native, calls = hello
    native.verify_code = code
    result = verifier.verify(123)
    assert result.reason == reason
    assert result.success is (reason == "verified")
    assert calls == ["open", ("verify", 123, 90), "close"]


@pytest.mark.parametrize("error,reason", [(auth._Canceled(), "canceled"), (TimeoutError("private"), "timeout"),
    (OSError("private path"), "unavailable")])
def test_native_failure_is_closed_and_private_details_never_returned(hello, error, reason):
    verifier, native, _ = hello
    native.verify_code = error
    result = verifier.verify(123)
    assert not result.success and result.reason == reason
    assert "private" not in repr(result)


@pytest.mark.parametrize("timeout", [True, -1, 0, 91, float("nan"), "90"])
def test_bad_timeouts_never_call_native_hello(hello, timeout):
    verifier, _, calls = hello
    assert verifier.verify(123, timeout=timeout).reason == "invalid_request"
    assert not calls


def test_unsupported_cancelled_or_missing_window_never_open_native_ui(hello, monkeypatch):
    verifier, _, calls = hello
    assert verifier.verify(None).reason == "window_invalid"
    assert verifier.verify(123, cancel=lambda: True).reason == "canceled"
    monkeypatch.setattr(auth, "_supported", lambda: False)
    assert verifier.verify(123).reason == "unsupported"
    assert not calls


def test_guid_bytes_and_generic_result_iid_follow_winrt_metadata():
    assert ctypes.sizeof(auth._GUID) == 16
    assert bytes(auth._guid(auth._STATICS)) == uuid.UUID("af4f3f91-564c-4ddc-b8b5-973447627c65").bytes_le
    assert auth._RESULT_IID == "fd596ffd-2318-558f-9dbe-d21df43764a5"


def fake_async(monkeypatch, *, statuses=(1,), result=0):
    """Actual ctypes COM vtables, with all callbacks retained until release."""
    calls, keepalive = [], []

    def object_with(methods):
        table = (auth._POINTER * 11)()
        for index, (arguments, function) in methods.items():
            callback = auth._CALL(auth._INT, auth._POINTER, *arguments)(function)
            keepalive.append(callback)
            table[index] = ctypes.cast(callback, auth._POINTER)
        instance = (ctypes.POINTER(auth._POINTER) * 1)(ctypes.cast(table, ctypes.POINTER(auth._POINTER)))
        keepalive.extend((table, instance))
        return ctypes.cast(instance, auth._POINTER)

    states = iter(statuses)
    last = statuses[-1]

    def get_status(_, output):
        calls.append("status")
        output[0] = next(states, last)
        return 0

    info = object_with({2: ((), lambda _: calls.append("info_release") or 0),
        7: ((ctypes.POINTER(auth._INT),), get_status),
        9: ((), lambda _: calls.append("cancel") or 0),
        10: ((), lambda _: calls.append("close") or 0)})

    def query(_, iid, output):
        assert bytes(iid.contents) == uuid.UUID(auth._ASYNC_INFO).bytes_le
        output[0] = info.value
        return 0

    def results(_, output):
        calls.append("results")
        output[0] = result
        return 0

    operation = object_with({0: ((ctypes.POINTER(auth._GUID), ctypes.POINTER(auth._POINTER)), query),
        2: ((), lambda _: calls.append("operation_release") or 0),
        8: ((ctypes.POINTER(auth._INT),), results)})
    clock = Clock()
    monkeypatch.setattr(auth.time, "monotonic", clock)
    monkeypatch.setattr(auth.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))
    return operation, calls, keepalive


def test_native_async_abi_uses_results_only_after_completed_and_releases(monkeypatch):
    operation, calls, keepalive = fake_async(monkeypatch, statuses=(0, 1), result=0)
    assert auth._NativeHello()._wait(operation, 1, None) == 0
    assert calls == ["status", "status", "results", "close", "info_release", "operation_release"]
    assert keepalive


@pytest.mark.parametrize("mode", ["deadline", "canceled"])
def test_native_pending_operation_has_bounded_deadline_and_cancel_never_grants(monkeypatch, mode):
    operation, calls, keepalive = fake_async(monkeypatch, statuses=(0,))
    with pytest.raises(TimeoutError if mode == "deadline" else auth._Canceled):
        auth._NativeHello()._wait(operation, 0.11, (lambda: True) if mode == "canceled" else None)
    assert "cancel" in calls and "results" not in calls
    assert calls[-2:] == ["info_release", "operation_release"] and keepalive


@pytest.mark.parametrize("state", [2, 3])
def test_native_canceled_or_error_async_status_cannot_grant(monkeypatch, state):
    operation, calls, keepalive = fake_async(monkeypatch, statuses=(state,))
    with pytest.raises(auth._Canceled if state == 2 else OSError):
        auth._NativeHello()._wait(operation, 1, None)
    assert "results" not in calls and "close" in calls and keepalive


@pytest.mark.parametrize("mode", ["late-result", "cancel-after-result"])
def test_completed_native_result_cannot_grant_after_deadline_or_cancellation(monkeypatch, mode):
    operation, calls, keepalive = fake_async(monkeypatch)
    clock_values = iter([100.0, 100.0, 102.0])
    if mode == "late-result":
        monkeypatch.setattr(auth.time, "monotonic", lambda: next(clock_values))
    cancel_values = iter([False, True])
    with pytest.raises(TimeoutError if mode == "late-result" else auth._Canceled):
        auth._NativeHello()._wait(operation, 1,
            (lambda: next(cancel_values)) if mode == "cancel-after-result" else None)
    assert "results" in calls and calls[-2:] == ["info_release", "operation_release"] and keepalive


def test_native_verification_uses_hwnd_interop_fixed_message_and_typed_result_iid(monkeypatch):
    calls = []

    def request(_, hwnd, message, iid, output):
        calls.append((hwnd, message, bytes(iid.contents)))
        output[0] = 789
        return 0

    callback = auth._CALL(auth._INT, auth._POINTER, auth._POINTER, auth._POINTER,
        ctypes.POINTER(auth._GUID), ctypes.POINTER(auth._POINTER))(request)
    table = (auth._POINTER * 7)()
    table[6] = ctypes.cast(callback, auth._POINTER)
    instance = (ctypes.POINTER(auth._POINTER) * 1)(ctypes.cast(table, ctypes.POINTER(auth._POINTER)))
    pointer = ctypes.cast(instance, auth._POINTER)
    native = auth._NativeHello()

    @contextmanager
    def factory(interface):
        assert interface == auth._INTEROP
        yield pointer

    @contextmanager
    def message(text):
        assert text == auth._MESSAGE
        yield auth._POINTER(456)

    monkeypatch.setattr(auth, "_valid_window", lambda hwnd: hwnd == 123)
    monkeypatch.setattr(native, "_factory", factory)
    monkeypatch.setattr(native, "_string", message)
    monkeypatch.setattr(native, "_wait", lambda operation, timeout, cancel:
        0 if operation.value == 789 and timeout == 90 and cancel is None else pytest.fail("ABI argument mismatch"))
    assert native.verify(123, None, 90) == 0
    assert calls == [(123, 456, uuid.UUID(auth._RESULT_IID).bytes_le)]


@pytest.mark.parametrize("hwnd,pid,visible,active,expected", [(0, 10, 1, 123, False),
    (True, 10, 1, 123, False), (123, 999, 1, 123, False), (123, 10, 0, 123, False),
    (123, 10, 1, 456, False), (123, 10, 1, 123, True)])
def test_hwnd_must_be_visible_foreground_window_of_current_process(monkeypatch, hwnd, pid, visible, active, expected):
    def process(_, output):
        ctypes.cast(output, ctypes.POINTER(auth._DWORD))[0] = pid
        return 1

    user = SimpleNamespace(IsWindow=lambda _: 1, GetWindowThreadProcessId=process,
        IsWindowVisible=lambda _: visible, GetForegroundWindow=lambda: active, GetAncestor=lambda value, _: value)
    monkeypatch.setattr(auth, "_dll", lambda _: user)
    monkeypatch.setattr(auth.os, "getpid", lambda: 10)
    assert auth._valid_window(hwnd) is expected


def test_invalid_native_window_is_rejected_before_creating_dialog(monkeypatch):
    monkeypatch.setattr(auth, "_valid_window", lambda _: False)
    native = auth._NativeHello()
    monkeypatch.setattr(native, "_factory", lambda _: pytest.fail("Must not create verification UI"))
    assert native.verify(123, None, 90) == "window_invalid"


def owner_session(**kwargs):
    clock = Clock()
    verifier = SimpleNamespace(verify=lambda *_a, **_kw: auth._result("verified"))
    session = auth.OwnerSession(True, 10, clock=clock, verifier=verifier, session_check=lambda: True, **kwargs)
    return session, clock, verifier


def test_disabled_policy_never_prompts_and_enabled_starts_unverified():
    session = auth.OwnerSession(verifier=SimpleNamespace(verify=lambda *_a, **_kw: pytest.fail("No consent requested")))
    assert session.allowed() and not session.status()["verified"]
    assert session.verify(123).reason == "disabled"
    session.set_enabled(True)
    assert not session.allowed() and session.status()["reason"] == "verification_required"


@pytest.mark.parametrize("ttl", [0, -1, 901, True, float("inf"), float("nan"), "10"])
def test_owner_ttl_is_finite_and_at_most_fifteen_minutes(ttl):
    with pytest.raises(ValueError, match="owner_session_invalid"):
        auth.OwnerSession(ttl_seconds=ttl)


def test_verified_owner_ttl_uses_monotonic_clock_is_not_persisted_and_expires():
    session, clock, _ = owner_session()
    assert session.verify(123).success and session.allowed()
    assert session.status() == {"enabled": True, "verified": True, "remaining_seconds": 10,
        "reason": "verified", "method": "windows_hello", "speaker_identity_verified": False}
    clock.now += 10
    assert not session.allowed() and session.status()["reason"] == "verification_expired"
    assert not auth.OwnerSession(True).allowed()


@pytest.mark.parametrize("bad_time", [99.0, float("nan"), None])
def test_clock_rollback_or_failure_revokes_instead_of_extending_grant(bad_time):
    session, clock, _ = owner_session()
    assert session.verify(123).success
    clock.now = bad_time
    assert not session.allowed() and session.status()["remaining_seconds"] == 0


@pytest.mark.parametrize("event", [2, 4, 6, 7, 8, 11])
def test_lock_unlock_logout_or_disconnect_requires_new_hello(event):
    session, _, _ = owner_session()
    assert session.verify(123).success
    session.handle_session_change(event)
    assert not session.allowed()


def test_passive_locked_session_revokes_and_unlock_alone_cannot_restore():
    session, _, _ = owner_session()
    assert session.verify(123).success
    session._session_check = lambda: False
    assert not session.allowed()
    session._session_check = lambda: True
    assert not session.allowed()


def test_cancellation_failed_verification_and_unavailability_revoke_previous_grant():
    session, _, verifier = owner_session()
    assert session.verify(123).success
    verifier.verify = lambda *_a, **_kw: auth._result("canceled")
    assert session.verify(123).reason == "canceled" and not session.allowed()
    verifier.verify = lambda *_a, **_kw: auth.AuthResult(False, "private error", "secret")
    assert session.verify(123) == auth._result("unavailable")
    assert "private" not in repr(session.status())


@pytest.mark.parametrize("mutation", ["lock", "disable", "revoke"])
def test_inflight_verification_cannot_regrant_after_revocation_or_policy_change(mutation):
    session, _, verifier = owner_session()

    def verify(*_a, **_kw):
        if mutation == "lock":
            session.handle_session_change(7)
        elif mutation == "disable":
            session.set_enabled(False)
        else:
            session.revoke()
        return auth._result("verified")

    verifier.verify = verify
    assert session.verify(123).reason == "stale"
    assert not session.status()["verified"]


def test_session_query_failure_and_invalid_result_never_grant():
    session, _, verifier = owner_session()
    session._session_check = lambda: (_ for _ in ()).throw(OSError("private"))
    assert session.verify(123).reason == "session_inactive" and not session.allowed()
    verifier.verify = lambda *_a, **_kw: True
    assert session.verify(123).reason == "unavailable"


def test_wts_registration_is_own_session_only_and_failures_are_closed(monkeypatch):
    calls = []
    native = SimpleNamespace(WTSRegisterSessionNotification=lambda hwnd, flags: calls.append((hwnd, flags)) or 1,
        WTSUnRegisterSessionNotification=lambda hwnd: calls.append((hwnd, "remove")) or 1)
    monkeypatch.setattr(auth, "_supported", lambda: True)
    monkeypatch.setattr(auth, "_valid_window", lambda hwnd, **_kw: hwnd == 123)
    monkeypatch.setattr(auth, "_dll", lambda _: native)
    assert auth.register_session_notifications(123)
    assert auth.unregister_session_notifications(123)
    assert not auth.register_session_notifications(456)
    assert calls == [(123, 0), (123, "remove")]
