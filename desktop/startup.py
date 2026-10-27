"""Explicit, current-user Windows startup for a verified local installation.

Only the application's marked Task Scheduler entry is created or removed.
No task is started now, no password is stored, and no elevation is requested.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading


TASK_NAME = "JarvisDesktopLocal"
OWNERSHIP_MARKER = "jarvis-aitu desktop startup: 7b1adc2c-6bf1-4e43-b4e8-42a337e0c1a9"
_LOCK = threading.Lock()
_ENABLED = "Автозапуск включён: Jarvis откроется после входа в Windows."
_DISABLED = "Автозапуск выключен."
_UNVERIFIED = "Не удалось проверить установку Jarvis. Переустановите приложение."
_CONFLICT = "Имя автозапуска занято другой задачей. Она не изменена."
_UNAVAILABLE = "Windows не разрешила изменить автозапуск. Проверьте права текущего пользователя."


@dataclass(frozen=True, slots=True)
class StartupResult:
    success: bool
    message: str


@dataclass(frozen=True, slots=True)
class _LaunchSpec:
    executable: str
    arguments: str
    working_directory: str


def _is_windows() -> bool:
    return sys.platform == "win32"


def _local_path(raw: str | Path) -> Path:
    value = str(raw)
    if (not value or "\x00" in value or "://" in value
            or value.startswith(("\\\\", "//"))):
        raise ValueError("local_path_required")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("absolute_path_required")
    resolved = path.resolve(strict=True)
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("local_path_required")
    return resolved


def _matches_hash(path: Path, expected: object, maximum: int) -> bool:
    if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)
            or not path.is_file() or not 0 < path.stat().st_size <= maximum):
        return False
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == expected.lower()


def _launch_spec() -> _LaunchSpec | None:
    try:
        if getattr(sys, "frozen", False):
            executable = _local_path(sys.executable)
            if executable.name.casefold() != "jarvisdesktop.exe" or not executable.is_file():
                return None
            return _LaunchSpec(str(executable), "", str(executable.parent))

        # The source checkout is intentionally not a persistent launch target.
        module = _local_path(__file__)
        root = _local_path(module.parents[2])
        if module != root / "app" / "desktop" / "startup.py":
            return None
        manifest_path = _local_path(root / "installation.json")
        if not manifest_path.is_relative_to(root) or manifest_path.stat().st_size > 65536:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if not isinstance(manifest, dict) or manifest.get("kind") != "isolated-source-runtime":
            return None
        executable = _local_path(root / "runtime" / "pythonw.exe")
        launcher = _local_path(root / "launcher.pyw")
        application = _local_path(root / "app")
        if (not all(path.is_relative_to(root) for path in (executable, launcher, application))
                or executable.name.casefold() != "pythonw.exe"
                or launcher.name.casefold() != "launcher.pyw" or not application.is_dir()
                or not _matches_hash(executable, manifest.get("pythonwSha256"), 64 * 1024 * 1024)
                or not _matches_hash(launcher, manifest.get("launcherSha256"), 1024 * 1024)):
            return None
        return _LaunchSpec(str(executable), subprocess.list2cmdline(["-I", str(launcher)]), str(application))
    except (OSError, ValueError, TypeError, IndexError, RuntimeError):
        return None


@contextmanager
def _scheduler_session():
    # COM must be initialized on the UI worker thread that performs the change.
    import pythoncom
    import win32api
    import win32con
    import win32security
    import win32com.client

    pythoncom.CoInitialize()
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.ConvertSidToStringSid(
                win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        finally:
            token.Close()
        if not isinstance(sid, str) or not re.fullmatch(r"S-1-5(?:-\d+)+", sid):
            raise ValueError("current_user_sid_unavailable")
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        yield scheduler, sid
    finally:
        pythoncom.CoUninitialize()


def _existing_task(folder):
    try:
        return folder.GetTask(TASK_NAME)
    except Exception as error:
        # Access denied or a damaged scheduler must never be mistaken for absence.
        code = getattr(error, "hresult", None)
        if isinstance(code, int) and (code & 0xFFFFFFFF) in (0x80070002, 0x80070003):
            return None
        raise


def set_startup_enabled(enabled: bool) -> StartupResult:
    """Change only Jarvis's owned logon task; return a fixed, safe RU message."""
    if type(enabled) is not bool:
        return StartupResult(False, "Не удалось изменить автозапуск: неверное значение настройки.")
    if not _is_windows():
        return StartupResult(False, "Автозапуск доступен только в Windows.")
    launch = _launch_spec() if enabled else None
    if enabled and launch is None:
        return StartupResult(False, _UNVERIFIED)
    try:
        with _LOCK, _scheduler_session() as (scheduler, sid):
            folder = scheduler.GetFolder("\\")
            existing = _existing_task(folder)
            if existing is not None:
                definition = existing.Definition
                if (definition.RegistrationInfo.Description != OWNERSHIP_MARKER
                        or definition.Principal.UserId != sid):
                    return StartupResult(False, _CONFLICT)
            if not enabled:
                if existing is not None:
                    folder.DeleteTask(TASK_NAME, 0)
                return StartupResult(True, _DISABLED)
            definition = scheduler.NewTask(0)
            definition.RegistrationInfo.Description = OWNERSHIP_MARKER
            definition.Principal.UserId = sid
            definition.Principal.LogonType = 3  # TASK_LOGON_INTERACTIVE_TOKEN; no password.
            definition.Principal.RunLevel = 0  # TASK_RUNLEVEL_LUA; current user's rights.
            trigger = definition.Triggers.Create(9)  # TASK_TRIGGER_LOGON.
            trigger.UserId = sid
            trigger.Delay = "PT5S"
            trigger.Enabled = True
            settings = definition.Settings
            settings.Enabled = True
            settings.StartWhenAvailable = True
            settings.RunOnlyIfNetworkAvailable = False
            settings.RunOnlyIfIdle = False
            settings.DisallowStartIfOnBatteries = False
            settings.StopIfGoingOnBatteries = False
            settings.ExecutionTimeLimit = "PT0S"
            settings.MultipleInstances = 2  # TASK_INSTANCES_IGNORE_NEW.
            settings.WakeToRun = False
            action = definition.Actions.Create(0)  # TASK_ACTION_EXEC; no command shell.
            action.Path = launch.executable
            action.Arguments = launch.arguments
            action.WorkingDirectory = launch.working_directory
            # Create-only closes the absent-task overwrite race. Update is used
            # only after the exact ownership marker and current SID matched.
            folder.RegisterTaskDefinition(TASK_NAME, definition, 2 if existing is None else 4, sid, "", 3)
            return StartupResult(True, _ENABLED)
    except Exception:
        return StartupResult(False, _UNAVAILABLE)
