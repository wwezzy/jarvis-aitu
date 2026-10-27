"""Startup tests never register, delete or start a real Windows task."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from desktop import startup


SID = "S-1-5-21-123-456-789-1001"
_NATIVE_SESSION = startup._scheduler_session


class SchedulerError(Exception):
    def __init__(self, hresult=0x80070002):
        self.hresult = hresult
        super().__init__("private Windows details must not reach the UI")


class Collection:
    def __init__(self):
        self.items = []

    def Create(self, kind):
        item = SimpleNamespace(Type=kind)
        self.items.append(item)
        return item


def definition(description=startup.OWNERSHIP_MARKER, sid=SID):
    return SimpleNamespace(
        RegistrationInfo=SimpleNamespace(Description=description),
        Principal=SimpleNamespace(UserId=sid), Settings=SimpleNamespace(),
        Triggers=Collection(), Actions=Collection())


class FakeScheduler:
    def __init__(self, existing=None, get_error=None, register_error=None):
        self.existing = existing
        self.get_error = get_error
        self.register_error = register_error
        self.lookups, self.registered, self.deleted, self.new = [], [], [], []
        self.connected = False

    def Connect(self):
        self.connected = True

    def GetFolder(self, name):
        assert name == "\\"
        return self

    def GetTask(self, name):
        self.lookups.append(name)
        if self.get_error:
            raise self.get_error
        if self.existing is None:
            raise SchedulerError()
        return SimpleNamespace(Definition=self.existing)

    def NewTask(self, flags):
        assert flags == 0
        task = definition()
        self.new.append(task)
        return task

    def RegisterTaskDefinition(self, *arguments):
        if self.register_error:
            raise self.register_error
        self.registered.append(arguments)

    def DeleteTask(self, *arguments):
        self.deleted.append(arguments)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    scheduler = FakeScheduler()
    spec = startup._LaunchSpec(str(tmp_path / "pythonw.exe"), '-I "C:\\Local App\\launcher.pyw"', str(tmp_path))
    closed = []

    @contextmanager
    def session():
        try:
            yield scheduler, SID
        finally:
            closed.append(True)

    monkeypatch.setattr(startup, "_is_windows", lambda: True)
    monkeypatch.setattr(startup, "_launch_spec", lambda: spec)
    monkeypatch.setattr(startup, "_scheduler_session", session)
    return scheduler, spec, closed


def test_enable_interactive_current_user_without_admin_network_battery_or_runtime_limit(harness):
    scheduler, spec, closed = harness
    result = startup.set_startup_enabled(True)
    assert result.success and result.message == startup._ENABLED
    assert scheduler.lookups == ["JarvisDesktopLocal"]
    name, task, flags, sid, password, logon = scheduler.registered[0]
    assert (name, flags, sid, password, logon) == (startup.TASK_NAME, 2, SID, "", 3)
    assert task.RegistrationInfo.Description == startup.OWNERSHIP_MARKER
    assert vars(task.Principal) == {"UserId": SID, "LogonType": 3, "RunLevel": 0}
    assert vars(task.Triggers.items[0]) == {"Type": 9, "UserId": SID, "Delay": "PT5S", "Enabled": True}
    assert vars(task.Settings) == {
        "Enabled": True, "StartWhenAvailable": True, "RunOnlyIfNetworkAvailable": False,
        "RunOnlyIfIdle": False, "DisallowStartIfOnBatteries": False,
        "StopIfGoingOnBatteries": False, "ExecutionTimeLimit": "PT0S",
        "MultipleInstances": 2, "WakeToRun": False}
    assert vars(task.Actions.items[0]) == {"Type": 0, "Path": spec.executable,
                                         "Arguments": spec.arguments, "WorkingDirectory": spec.working_directory}
    assert not scheduler.deleted and closed == [True]


def test_update_only_matching_owned_task(harness):
    scheduler, _, _ = harness
    scheduler.existing = definition()
    assert startup.set_startup_enabled(True).success
    assert scheduler.registered[0][2] == 4
    assert not scheduler.deleted


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("description,sid", [("another application", SID),
                                               (startup.OWNERSHIP_MARKER, "S-1-5-21-999-1001")])
def test_foreign_task_is_never_modified(harness, enabled, description, sid):
    scheduler, _, _ = harness
    scheduler.existing = definition(description, sid)
    assert startup.set_startup_enabled(enabled) == startup.StartupResult(False, startup._CONFLICT)
    assert not scheduler.deleted and not scheduler.registered and not scheduler.new


def test_disable_removes_only_owned_task_and_never_verifies_or_runs_launcher(harness, monkeypatch):
    scheduler, _, _ = harness
    scheduler.existing = definition()
    monkeypatch.setattr(startup, "_launch_spec", lambda: pytest.fail("disabled must not load a launcher"))
    assert startup.set_startup_enabled(False) == startup.StartupResult(True, startup._DISABLED)
    assert scheduler.deleted == [("JarvisDesktopLocal", 0)]
    assert not scheduler.registered and not scheduler.new
    assert "JarvisPersonalAgent" not in scheduler.lookups


def test_disabling_absent_task_is_idempotent(harness):
    scheduler, _, _ = harness
    assert startup.set_startup_enabled(False).success
    assert not scheduler.deleted and not scheduler.registered


@pytest.mark.parametrize("error", [SchedulerError(0x80070005), RuntimeError("sensitive details")])
def test_get_task_errors_never_treated_as_missing_or_exposed(harness, error):
    scheduler, _, closed = harness
    scheduler.get_error = error
    result = startup.set_startup_enabled(True)
    assert result == startup.StartupResult(False, startup._UNAVAILABLE)
    assert not scheduler.new and not scheduler.registered and not scheduler.deleted
    assert closed == [True]


def test_permission_failure_returns_fixed_message_and_cleans_up(harness):
    scheduler, _, closed = harness
    scheduler.register_error = SchedulerError(0x80070005)
    assert startup.set_startup_enabled(True) == startup.StartupResult(False, startup._UNAVAILABLE)
    assert closed == [True]


@pytest.mark.parametrize("enabled", [1, 0, None, "true"])
def test_strict_boolean_and_unsupported_platform_do_no_com(harness, monkeypatch, enabled):
    scheduler, _, _ = harness
    assert not startup.set_startup_enabled(enabled).success
    monkeypatch.setattr(startup, "_is_windows", lambda: False)
    assert startup.set_startup_enabled(True).message == "Автозапуск доступен только в Windows."
    assert not scheduler.lookups


def test_unverified_installation_is_refused_before_com(harness, monkeypatch):
    scheduler, _, _ = harness
    monkeypatch.setattr(startup, "_launch_spec", lambda: None)
    assert startup.set_startup_enabled(True) == startup.StartupResult(False, startup._UNVERIFIED)
    assert not scheduler.lookups


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / "Local App"
    module = root / "app" / "desktop" / "startup.py"
    module.parent.mkdir(parents=True)
    module.write_text("# installed startup", encoding="utf-8")
    executable = root / "runtime" / "pythonw.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"setup-verified interpreter")
    launcher = root / "launcher.pyw"
    launcher.write_bytes(b"# fixed audited launcher")
    manifest = {"kind": "isolated-source-runtime", "pythonwSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "launcherSha256": hashlib.sha256(launcher.read_bytes()).hexdigest()}
    (root / "installation.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(startup, "__file__", str(module))
    monkeypatch.delattr(startup.sys, "frozen", raising=False)
    return root, executable, launcher, manifest


def test_verified_installation_pins_pythonw_launcher_and_fixed_isolated_arguments(installed):
    root, executable, launcher, _ = installed
    spec = startup._launch_spec()
    assert spec == startup._LaunchSpec(str(executable.resolve()), subprocess.list2cmdline(["-I", str(launcher.resolve())]),
                                      str((root / "app").resolve()))
    assert "-I " in spec.arguments and "--" not in spec.arguments


@pytest.mark.parametrize("change", ["launcher", "pythonw", "wrong_kind", "missing_hash", "non_object", "oversized"])
def test_changed_or_untrusted_installation_refused(installed, change):
    root, executable, launcher, manifest = installed
    if change == "launcher":
        launcher.write_bytes(b"changed launcher")
    elif change == "pythonw":
        executable.write_bytes(b"changed interpreter")
    elif change == "wrong_kind":
        manifest["kind"] = "external-command"
    elif change == "missing_hash":
        del manifest["launcherSha256"]
    elif change == "non_object":
        manifest = []
    if change == "oversized":
        (root / "installation.json").write_text(" " * 65537, encoding="utf-8")
    else:
        (root / "installation.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert startup._launch_spec() is None


def test_source_checkout_and_foreign_frozen_executable_refused(tmp_path, monkeypatch):
    module = tmp_path / "repo" / "desktop" / "startup.py"
    module.parent.mkdir(parents=True)
    module.write_text("# source", encoding="utf-8")
    monkeypatch.setattr(startup, "__file__", str(module))
    monkeypatch.delattr(startup.sys, "frozen", raising=False)
    assert startup._launch_spec() is None
    executable = tmp_path / "other.exe"
    executable.write_bytes(b"other application")
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(startup.sys, "executable", str(executable))
    assert startup._launch_spec() is None


def test_current_frozen_own_executable_has_no_arguments(tmp_path, monkeypatch):
    executable = tmp_path / "JarvisDesktop.exe"
    executable.write_bytes(b"currently running own application")
    monkeypatch.setattr(startup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(startup.sys, "executable", str(executable))
    assert startup._launch_spec() == startup._LaunchSpec(str(executable.resolve()), "", str(tmp_path.resolve()))


@pytest.mark.parametrize("raw", [r"\\server\share\pythonw.exe", "//server/share/pythonw.exe",
                                 "https://example.com/pythonw.exe", "relative/pythonw.exe", "bad\x00path"])
def test_network_url_relative_and_nul_paths_rejected_without_io(raw, monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda *_a, **_k: pytest.fail("unsafe path must not be resolved"))
    with pytest.raises(ValueError):
        startup._local_path(raw)


def test_native_com_adapter_initializes_worker_and_uses_current_token_sid(harness, monkeypatch):
    scheduler, _, _ = harness
    events = []
    token = SimpleNamespace(Close=lambda: events.append("token-closed"))
    modules = {
        "pythoncom": SimpleNamespace(CoInitialize=lambda: events.append("initialized"),
                                     CoUninitialize=lambda: events.append("uninitialized")),
        "win32api": SimpleNamespace(GetCurrentProcess=lambda: "current-process"),
        "win32con": SimpleNamespace(TOKEN_QUERY=8),
        "win32security": SimpleNamespace(
            OpenProcessToken=lambda process, access: token if (process, access) == ("current-process", 8) else None,
            TokenUser=1, GetTokenInformation=lambda value, kind: ("native-sid", 0),
            ConvertSidToStringSid=lambda value: SID if value == "native-sid" else ""),
    }
    package = ModuleType("win32com")
    package.__path__ = []
    client = ModuleType("win32com.client")
    client.Dispatch = lambda name: scheduler if name == "Schedule.Service" else pytest.fail("foreign COM")
    package.client = client
    modules.update({"win32com": package, "win32com.client": client})
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    # Use the actual adapter, replacing only its imported Windows dependencies.
    with _NATIVE_SESSION() as (actual_scheduler, actual_sid):
        assert actual_scheduler is scheduler and actual_sid == SID
        assert scheduler.connected
    assert events == ["initialized", "token-closed", "uninitialized"]
