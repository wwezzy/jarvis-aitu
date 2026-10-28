"""Finite local window controls, independent of models and arbitrary keys.

Only the original-command router calls this adapter. Named windows must belong
to an exactly registered application or a verified built-in. Closing uses a
one-use, in-memory confirmation bound to the original process/window identity;
it sends WM_CLOSE, never kills a process or dismisses a save prompt.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import ntpath
import os
import re
import secrets
import threading
import time


OPERATIONS = frozenset({"focus", "restore", "minimize", "maximize", "close"})
MAX_WINDOWS = 512
CONFIRM_SECONDS = 60
_DENIED_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "ConsoleWindowClass", "#32770"}
_DENIED_PROCESSES = {"csrss.exe", "winlogon.exe", "lsass.exe", "services.exe", "svchost.exe", "smss.exe",
                     "wininit.exe", "dwm.exe", "consent.exe", "conhost.exe", "cmd.exe", "powershell.exe",
                     "pwsh.exe", "windowsterminal.exe"}


@dataclass(frozen=True)
class WindowRef:
    hwnd: int
    pid: int
    created: int
    executable: str
    class_name: str = ""


@dataclass(frozen=True)
class ControlResult:
    text: str
    status: str
    confirmation_id: str | None = None


def _allowed(guard):
    try:
        return guard is None or callable(guard) and guard() is True
    except Exception:
        return False


def _path_key(value):
    return ntpath.normcase(ntpath.normpath(str(value)))


def parse_command(original):
    """Pure whole-request grammar; returns (operation, exact app alias) or None.

    Unknown targets remain literal aliases; they are never guessed, searched in
    running-window captions, extracted from quotations or interpreted as paths.
    A model may suggest wording but cannot call this as an authorization step.
    """
    if not isinstance(original, str) or not original.strip() or len(original) > 300:
        return None
    value = re.sub(r"\s+", " ", original.casefold().replace("ё", "е").strip()).rstrip(".!?").strip()
    value = re.sub(r"^(?:джарвис|jarvis)[,\s]+", "", value)
    value = re.sub(r"^(?:пожалуйста|please)[,\s]+", "", value)
    value = re.sub(r"^(?:можешь(?: ты)?|can you|could you)[,\s]+", "", value)
    value = re.sub(r"[,\s]+(?:пожалуйста|please)$", "", value)
    match = re.fullmatch(
        r"(сфокусируй|переключись на|покажи окно|восстанови|сверни|разверни|закрой|"
        r"focus|switch to|restore|maximize|minimize|close)\s+(.+)", value)
    if not match:
        return None
    operation = {"сфокусируй": "focus", "переключись на": "focus", "покажи окно": "focus", "switch to": "focus",
                 "восстанови": "restore", "сверни": "minimize", "разверни": "maximize", "закрой": "close"}.get(match[1], match[1])
    target = match[2]
    if target in {"окно", "текущее окно", "активное окно", "current window", "active window", "the window", "window"}:
        return operation, None
    target = re.sub(r"^(?:окно|приложение|window|app|application)\s+", "", target)
    # Conjunctions inside a real app name are harmless; a second imperative is
    # not a second action and is never silently dropped from the target.
    if re.search(r"\b(?:и|затем|потом|and|then)\s+(?:открой|запусти|закрой|удали|выключи|open|run|close|delete|shutdown)\b", target):
        return None
    try:
        from desktop.project_catalog import _name
        return operation, _name(target)
    except ValueError:
        return None


class NativeWindows:
    """Lazy Win32 backend. No window captions, keystrokes or screen images."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows controls unavailable")
        from ctypes import wintypes as w
        self.user = ctypes.WinDLL("user32", use_last_error=True, winmode=0x800)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
        self.callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        signatures = {
            "GetForegroundWindow": (w.HWND, []), "IsWindow": (w.BOOL, [w.HWND]),
            "IsWindowVisible": (w.BOOL, [w.HWND]), "IsIconic": (w.BOOL, [w.HWND]), "IsZoomed": (w.BOOL, [w.HWND]),
            "GetWindowThreadProcessId": (w.DWORD, [w.HWND, ctypes.POINTER(w.DWORD)]),
            "GetClassNameW": (ctypes.c_int, [w.HWND, w.LPWSTR, ctypes.c_int]),
            "EnumWindows": (w.BOOL, [self.callback_type, w.LPARAM]),
            "ShowWindowAsync": (w.BOOL, [w.HWND, ctypes.c_int]),
            "SetForegroundWindow": (w.BOOL, [w.HWND]),
            "PostMessageW": (w.BOOL, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
        }
        for name, (result, arguments) in signatures.items():
            method = getattr(self.user, name)
            method.restype, method.argtypes = result, arguments
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = w.BOOL
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
        self.kernel.GetProcessTimes.restype = w.BOOL

    def inspect(self, hwnd):
        from ctypes import wintypes as w
        if not hwnd or not self.user.IsWindow(hwnd) or not self.user.IsWindowVisible(hwnd):
            return None
        pid = w.DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value <= 4 or pid.value == os.getpid():
            return None
        name = ctypes.create_unicode_buffer(256)
        if not self.user.GetClassNameW(hwnd, name, len(name)) or name.value in _DENIED_CLASSES:
            return None
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            size = w.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            times = [w.FILETIME() for _ in range(4)]
            if (not self.kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size))
                    or not self.kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times))):
                return None
            if (ntpath.basename(path.value).casefold() in _DENIED_PROCESSES
                    or path.value.startswith(("\\\\", "//")) or not ntpath.isabs(path.value)):
                return None
            created = times[0].dwLowDateTime | times[0].dwHighDateTime << 32
            return WindowRef(int(hwnd), pid.value, created, path.value, name.value)
        finally:
            self.kernel.CloseHandle(handle)

    def foreground(self):
        return self.inspect(self.user.GetForegroundWindow())

    def windows(self):
        windows, count, limited = [], 0, False
        @self.callback_type
        def collect(hwnd, _parameter):
            nonlocal count, limited
            count += 1
            if count > MAX_WINDOWS:
                limited = True
                return False
            item = self.inspect(hwnd)
            if item is not None:
                windows.append(item)
            return True
        if not self.user.EnumWindows(collect, 0) or limited:
            raise OSError("Window enumeration incomplete")
        return windows

    def perform(self, operation, window, *, authorization_guard):
        # Never retarget an action to whatever became foreground after a
        # confirmation dialog. HWND reuse alone cannot pass this identity test.
        if not _allowed(authorization_guard):
            return "denied"
        # Approval/hash verification can be relatively expensive. Re-read the
        # HWND identity afterwards, immediately before sending any message.
        if self.inspect(window.hwnd) != window:
            return "changed"
        if operation == "close":
            return "requested" if self.user.PostMessageW(window.hwnd, 0x10, 0, 0) else "denied"
        if operation == "focus":
            if self.user.IsIconic(window.hwnd):
                self.user.ShowWindowAsync(window.hwnd, 9)
            # The second check matters when restoring a minimized window took
            # time or owner/session revocation occurred between native calls.
            if not _allowed(authorization_guard) or self.inspect(window.hwnd) != window:
                return "changed"
            self.user.SetForegroundWindow(window.hwnd)
            return "observed" if self.user.GetForegroundWindow() == window.hwnd else "requested"
        mode = {"restore": 9, "minimize": 6, "maximize": 3}.get(operation)
        if mode is None:
            return "unsupported"
        if not self.user.ShowWindowAsync(window.hwnd, mode):
            return "denied"
        if self.inspect(window.hwnd) != window:
            return "changed"
        observed = (self.user.IsIconic(window.hwnd) if operation == "minimize" else self.user.IsZoomed(window.hwnd)
                    if operation == "maximize" else not self.user.IsIconic(window.hwnd) and not self.user.IsZoomed(window.hwnd))
        return "observed" if observed else "requested"


def _builtin_target(alias):
    if alias not in {"хром", "гугл хром", "google chrome", "chrome", "яндекс музыка", "яндекс музыку", "yandex music",
                     "проводник", "explorer", "блокнот", "notepad", "калькулятор", "calculator"}:
        return None
    from desktop.project_catalog import _system_directory
    from desktop.app_catalog import _fingerprint
    system = _system_directory()
    if alias in {"хром", "гугл хром", "google chrome", "chrome"}:
        from desktop.known_apps import verified_chrome
        executable = verified_chrome(system)
    elif alias in {"яндекс музыка", "яндекс музыку", "yandex music"}:
        from desktop.media import _native_music_executable
        executable = _native_music_executable()
    elif alias in {"проводник", "explorer", "блокнот", "notepad", "калькулятор", "calculator"}:
        executable = system.parent / "explorer.exe" if alias in {"проводник", "explorer"} else system / (
            "notepad.exe" if alias in {"блокнот", "notepad"} else "calc.exe")
    else:
        return None
    if executable is None:
        return None
    digest, size = _fingerprint(executable)
    return {"path": str(executable), "sha256": digest, "size": size, "id": None}


class ComputerControls:
    """Call only from trusted original-input routing, never from model output.

    Native/test backend: foreground()->WindowRef|None, windows()->list[WindowRef],
    inspect(hwnd)->WindowRef|None, perform(op,ref,authorization_guard=...)->state.
    Unknown confirmation tokens return None so Core can retain its power policy.
    """
    def __init__(self, app_catalog=None, *, native=None, clock=None):
        self.apps = app_catalog
        self._native = native
        self._clock = clock or time.monotonic
        self._closed = threading.Event()
        self._lock = threading.RLock()
        self._pending = {}

    def _backend(self):
        if self._native is None:
            self._native = NativeWindows()
        return self._native

    def _proof(self, target):
        from desktop.project_catalog import _key
        alias = _key(target)
        if self.apps is not None:
            result = self.apps.resolve(alias)
            if result["status"] == "ambiguous":
                return None, "ambiguous"
            if result["status"] == "found":
                return {**result["app"], "alias": alias}, None
        proof = _builtin_target(alias)
        return (proof, None) if proof else (None, "missing")

    def _authorized(self, guard, window, proof):
        if self._closed.is_set() or not _allowed(guard):
            return False
        if proof is not None:
            from desktop.app_catalog import _executable, _fingerprint
            if proof["id"] is not None:
                latest = self.apps.resolve(proof["alias"])
                if latest["status"] != "found" or any(latest["app"].get(key) != proof.get(key) for key in ("id", "path", "sha256", "size")):
                    return False
            path = _executable(proof["path"])
            if _path_key(window.executable) != _path_key(path) or _fingerprint(path) != (proof["sha256"], proof["size"]):
                return False
        return not self._closed.is_set() and _allowed(guard)

    def _perform(self, operation, window, proof, guard):
        try:
            def allowed():
                try:
                    return self._authorized(guard, window, proof)
                except (OSError, ValueError, TypeError):
                    return False
            if not allowed():
                return ControlResult("Доступ изменился или приложение обновлено. Действие не выполнено.", "denied")
            state = self._backend().perform(operation, window, authorization_guard=allowed)
            if state == "observed":
                return ControlResult({"focus": "Переключился на нужное окно.", "restore": "Восстановил окно.",
                                      "minimize": "Свернул окно.", "maximize": "Развернул окно."}[operation], state)
            if state == "requested":
                return ControlResult("Запросил закрытие окна. Если есть несохранённая работа, ответь на запрос самого приложения."
                    if operation == "close" else "Передал команду окну. Windows пока не подтвердила результат.", state)
            return ControlResult("Окно или доступ изменились. Действие не подтверждено; повтори запрос.", state)
        except (OSError, ValueError, TypeError, RuntimeError):
            return ControlResult("Управление этим окном сейчас недоступно. Действие не подтверждено.", "unavailable")

    def request(self, operation, target=None, *, authorization_guard=None):
        if not isinstance(operation, str) or operation not in OPERATIONS:
            return ControlResult("Такое управление окном не поддерживается.", "unsupported")
        if self._closed.is_set() or not _allowed(authorization_guard):
            return ControlResult("Требуется действующее разрешение владельца.", "denied")
        try:
            proof = None
            if target is None:
                window = self._backend().foreground()
            else:
                proof, error = self._proof(target)
                if error:
                    return ControlResult("Укажи единственное точное имя разрешённого приложения." if error == "ambiguous"
                        else "Не нашёл разрешённое приложение с таким именем. Добавь его в разделе «Управление».", error)
                choices = [window for window in self._backend().windows() if _path_key(window.executable) == _path_key(proof["path"])]
                if len(choices) > 1:
                    return ControlResult("У приложения несколько окон. Выбери нужное и скажи «текущее окно».", "ambiguous")
                window = choices[0] if choices else None
            if window is None:
                return ControlResult("Не нашёл подходящее открытое окно. Открой его и повтори запрос.", "missing")
            if not self._authorized(authorization_guard, window, proof):
                return ControlResult("Доступ изменился или приложение обновлено. Действие не выполнено.", "denied")
            if operation != "close":
                return self._perform(operation, window, proof, authorization_guard)
            with self._lock:
                now = self._clock()
                self._pending = {key: value for key, value in self._pending.items() if now < value[0]}
                if len(self._pending) >= 20 or self._closed.is_set():
                    return ControlResult("Слишком много ожидающих запросов закрытия.", "denied")
                token = secrets.token_hex(16)
                self._pending[token] = (now + CONFIRM_SECONDS, window, proof, authorization_guard)
            return ControlResult("Подтверди закрытие выбранного окна кнопкой в течение 60 секунд. Сохрани важные изменения.", "confirmation_required", token)
        except (OSError, ValueError, TypeError, RuntimeError):
            return ControlResult("Не удалось безопасно определить нужное окно.", "unavailable")

    def confirm(self, token, *, authorization_guard=None):
        if not isinstance(token, str):
            return None
        with self._lock:
            pending = self._pending.pop(token, None)
        if pending is None:
            return None
        until, window, proof, original_guard = pending
        if self._closed.is_set() or self._clock() >= until:
            return ControlResult("Подтверждение закрытия истекло. Запроси действие заново.", "expired")
        return self._perform("close", window, proof, lambda: _allowed(original_guard) and _allowed(authorization_guard))

    def cancel(self, token):
        if not isinstance(token, str):
            return None
        with self._lock:
            pending = self._pending.pop(token, None)
        return ControlResult("Запрос закрытия отменён.", "canceled") if pending is not None else None

    def close(self):
        self._closed.set()
        with self._lock:
            self._pending.clear()
