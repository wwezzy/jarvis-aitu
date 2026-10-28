"""Only fake window APIs; never focus/close an actual window or play audio."""
from dataclasses import replace
import hashlib
import struct
from types import SimpleNamespace

import pytest

from desktop import computer_controls as module


class FakeNative:
    def __init__(self, windows):
        self.items = {item.hwnd: item for item in windows}
        self.current = windows[0].hwnd if windows else 0
        self.calls = []
        self.state = "observed"
        self.before_dispatch = lambda: None

    def foreground(self):
        return self.items.get(self.current)

    def windows(self):
        return list(self.items.values())

    def inspect(self, hwnd):
        return self.items.get(hwnd)

    def perform(self, operation, window, *, authorization_guard):
        self.before_dispatch()
        if self.inspect(window.hwnd) != window:
            return "changed"
        if authorization_guard() is not True:
            return "denied"
        self.calls.append((operation, window))
        return "requested" if operation == "close" else self.state


def window(number=1, executable="C:/Approved/Editor.exe"):
    return module.WindowRef(number, number + 100, number * 100000, executable, "EditorWindow")


@pytest.mark.parametrize("text,expected", [
    ("Джарвис, переключись на Google Chrome.", ("focus", "google chrome")),
    ("можешь сфокусируй приложение Редактор", ("focus", "редактор")),
    ("закрой текущее окно", ("close", None)),
    ("Please minimize current window", ("minimize", None)),
    ("restore app Research and Notes", ("restore", "research and notes")),
    ("разверни окно Редактор", ("maximize", "редактор")),
])
def test_only_original_whole_command_grammar(text, expected):
    assert module.parse_command(text) == expected


@pytest.mark.parametrize("text", [
    '"закрой текущее окно"', "не закрой текущее окно", "он сказал закрой текущее окно",
    "запомни закрой текущее окно", "умеешь ли ты закрыть окно", "как закрыть окно",
    "закрой Chrome и открой Блокнот", "close Editor and delete files", "focus C:/Unknown/app.exe",
    "close https://example.invalid", "close --force", "close Editor;shutdown", "close Editor\x00", "close `Editor`",
])
def test_quoted_reported_compound_and_arbitrary_targets_are_inert(text):
    assert module.parse_command(text) is None


@pytest.mark.parametrize("operation", ["focus", "restore", "minimize", "maximize"])
def test_current_window_operations_use_captured_identity_and_observed_state(operation):
    native = FakeNative([window()])
    controls = module.ComputerControls(native=native)
    result = controls.request(operation)
    assert result.status == "observed" and result.confirmation_id is None
    assert native.calls == [(operation, window())]
    native.state = "requested"
    assert controls.request(operation).status == "requested"


def test_close_never_dispatches_until_one_use_confirmation_and_retains_original_window():
    native = FakeNative([window(), window(2)])
    controls = module.ComputerControls(native=native)
    proposal = controls.request("close")
    assert proposal.status == "confirmation_required" and len(proposal.confirmation_id) == 32
    assert native.calls == []
    native.current = 2  # Clicking a confirmation can itself change foreground.
    result = controls.confirm(proposal.confirmation_id)
    assert result.status == "requested" and "запрос" in result.text.casefold()
    assert native.calls == [("close", window())]
    assert controls.confirm(proposal.confirmation_id) is None


@pytest.mark.parametrize("change", ["pid", "created", "executable", "missing"])
def test_reused_or_changed_hwnd_cannot_receive_old_close(change):
    original = window()
    native = FakeNative([original])
    controls = module.ComputerControls(native=native)
    token = controls.request("close").confirmation_id
    if change == "missing":
        native.items.clear()
    else:
        native.items[1] = replace(original, **{change: {"pid": 700, "created": 999999, "executable": "C:/Different.exe"}[change]})
    assert controls.confirm(token).status == "changed"
    assert native.calls == []


def test_cancel_timeout_shutdown_and_capacity_never_leave_replayable_close():
    native, now = FakeNative([window()]), [100.0]
    controls = module.ComputerControls(native=native, clock=lambda: now[0])
    canceled = controls.request("close").confirmation_id
    assert controls.cancel(canceled).status == "canceled"
    assert controls.confirm(canceled) is None
    expired = controls.request("close").confirmation_id
    now[0] += 60
    assert controls.confirm(expired).status == "expired"
    tokens = [controls.request("close").confirmation_id for _ in range(20)]
    assert controls.request("close").status == "denied"
    controls.close()
    assert all(controls.confirm(token) is None for token in tokens)
    assert controls.request("focus").status == "denied" and native.calls == []


def test_owner_revocation_before_final_native_boundary_and_after_close_proposal():
    native, allowed = FakeNative([window()]), [True]
    controls = module.ComputerControls(native=native)
    proposal = controls.request("close", authorization_guard=lambda: allowed[0])
    allowed[0] = False
    # Omitting a new guard cannot bypass the original request's owner guard.
    assert controls.confirm(proposal.confirmation_id).status == "denied"
    allowed[0] = True
    native.before_dispatch = lambda: allowed.__setitem__(0, False)
    assert controls.request("focus", authorization_guard=lambda: allowed[0]).status == "denied"
    assert native.calls == []


def executable(tmp_path):
    path = tmp_path / "Editor.exe"
    raw = bytearray(256)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 64)
    struct.pack_into("<4sHHIIIHHH", raw, 64, b"PE\0\0", 0x8664, 1, 0, 0, 0, 112, 2, 0x20B)
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


class FakeCatalog:
    def __init__(self, record):
        self.record = record
        self.status = "found"

    def resolve(self, _name):
        return {"status": self.status, "app": self.record, "suggestions": []}


def test_named_target_exact_catalog_hash_and_id_are_rechecked_at_dispatch(tmp_path):
    path, digest = executable(tmp_path)
    record = {"id": 1, "path": str(path), "sha256": digest, "size": 256}
    catalog = FakeCatalog(record)
    native = FakeNative([window(executable=str(path))])
    controls = module.ComputerControls(catalog, native=native)
    assert controls.request("focus", "Editor").status == "observed"
    native.calls.clear()
    native.before_dispatch = lambda: catalog.record.update(id=2)
    assert controls.request("focus", "Editor").status == "denied" and not native.calls
    native.before_dispatch = lambda: None
    path.write_bytes(b"different executable")
    assert controls.request("focus", "Editor").status in {"denied", "unavailable"} and not native.calls


def test_ambiguous_catalog_and_multiple_windows_never_guess(tmp_path):
    path, digest = executable(tmp_path)
    catalog = FakeCatalog({"id": 1, "path": str(path), "sha256": digest, "size": 256})
    native = FakeNative([window(executable=str(path)), window(2, str(path))])
    controls = module.ComputerControls(catalog, native=native)
    assert controls.request("focus", "Editor").status == "ambiguous"
    catalog.status = "ambiguous"
    assert controls.request("close", "Editor").status == "ambiguous"
    assert not native.calls


def test_missing_builtin_and_native_failure_are_truthful_without_private_details(monkeypatch):
    monkeypatch.setattr(module, "_builtin_target", lambda _alias: None)
    controls = module.ComputerControls(native=FakeNative([]))
    assert controls.request("focus", "Unknown").status == "missing"
    assert controls.request("focus").status == "missing"
    assert controls.request("kill").status == "unsupported"
    def fail():
        raise OSError("private title/path")
    controls._native.foreground = fail
    result = controls.request("focus")
    assert result.status == "unavailable" and "private" not in result.text


def test_constructor_and_parser_never_load_native_or_touch_os(monkeypatch):
    def denied():
        pytest.fail("Native discovery occurred before an actual request")
    monkeypatch.setattr(module, "NativeWindows", denied)
    controls = module.ComputerControls()
    assert controls._native is None
    assert module.parse_command("focus Chrome") == ("focus", "chrome")
    controls.close()


def test_native_close_uses_only_wm_close_and_does_not_claim_window_closed():
    native = object.__new__(module.NativeWindows)
    native.inspect = lambda _hwnd: window()
    calls = []
    native.user = SimpleNamespace(PostMessageW=lambda *arguments: calls.append(arguments) or 1)
    assert native.perform("close", window(), authorization_guard=lambda: True) == "requested"
    assert calls == [(1, 0x10, 0, 0)]
    assert native.perform("close", window(), authorization_guard=lambda: False) == "denied"
    assert len(calls) == 1


def test_native_focus_does_not_claim_success_when_windows_refuses_foreground():
    native = object.__new__(module.NativeWindows)
    native.inspect = lambda _hwnd: window()
    calls = []
    native.user = SimpleNamespace(IsIconic=lambda _hwnd: False,
        SetForegroundWindow=lambda hwnd: calls.append(hwnd) or 0, GetForegroundWindow=lambda: 9)
    assert native.perform("focus", window(), authorization_guard=lambda: True) == "requested"
    assert calls == [1]


def test_native_window_identity_is_rechecked_after_expensive_authorization():
    native = object.__new__(module.NativeWindows)
    current, calls = [window()], []
    native.inspect = lambda _hwnd: current[0]
    native.user = SimpleNamespace(PostMessageW=lambda *arguments: calls.append(arguments) or 1)
    def changed_during_hash_check():
        current[0] = replace(window(), created=1234567)
        return True
    assert native.perform("close", window(), authorization_guard=changed_during_hash_check) == "changed"
    assert calls == []
