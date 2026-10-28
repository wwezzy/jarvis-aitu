"""Explicit Windows Hello consent and an ephemeral owner session.

Hello verifies the currently signed-in Windows user through Windows' own UI
(face, fingerprint or PIN); it does not identify whoever spoke a command.
There are no camera calls, biometric templates, credentials or persistent grants.

Win32 HWND interop and enums follow Microsoft's UserConsentVerifier docs:
https://learn.microsoft.com/uwp/api/windows.security.credentials.ui.userconsentverifier
https://learn.microsoft.com/windows/win32/api/userconsentverifierinterop/
GUIDs/ABI: microsoft/windows-rs, Windows/Security/Credentials/UI/mod.rs.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import math
import os
import sys
import threading
import time
import uuid


_CLASS = "Windows.Security.Credentials.UI.UserConsentVerifier"
_STATICS = "af4f3f91-564c-4ddc-b8b5-973447627c65"
_INTEROP = "39e050c3-4e74-441a-8dc0-b81104df949c"
_ASYNC_INFO = "00000036-0000-0000-c000-000000000046"
# Microsoft's WinRT parameterized-type UUIDv5 algorithm, not a new app IID.
_RESULT_SIGNATURE = ("pinterface({9fc2b0bb-e446-44e2-aa61-9cab8f636af2};"
                     "enum(Windows.Security.Credentials.UI.UserConsentVerificationResult;i4))")
_RESULT_IID = str(uuid.uuid5(uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee"), _RESULT_SIGNATURE))
_MESSAGE = "Подтвердите владельца Jarvis для голосовых команд и жестов."
_CALL = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_POINTER = ctypes.c_void_p
_INT = ctypes.c_int32
_DWORD = ctypes.c_uint32
_MESSAGES = {
    "verified": "Владелец подтверждён через Windows Hello.",
    "available": "Windows Hello доступен. Способ проверки выбирает Windows: лицо, отпечаток или PIN.",
    "device_missing": "Устройство проверки Windows Hello недоступно.",
    "not_configured": "Для этой учётной записи Windows Hello ещё не настроен.",
    "policy_disabled": "Windows Hello отключён политикой Windows.",
    "busy": "Windows Hello занят. Повторите проверку позже.",
    "retries_exhausted": "Windows Hello остановил проверку после неудачных попыток.",
    "canceled": "Проверка владельца отменена; доступ не предоставлен.",
    "timeout": "Время проверки владельца истекло; доступ не предоставлен.",
    "window_invalid": "Откройте окно Jarvis и подтвердите владельца кнопкой в приложении.",
    "unsupported": "Эта проверка владельца требует Windows 11 или новее.",
    "unavailable": "Windows Hello недоступен; доступ не предоставлен.",
    "disabled": "Защита голосовых команд и жестов владельцем выключена.",
    "session_inactive": "Сеанс Windows заблокирован или неактивен; подтвердите владельца заново.",
    "stale": "Проверка больше не актуальна; подтвердите владельца заново.",
    "invalid_request": "Некорректный запрос проверки владельца; доступ не предоставлен.",
}


@dataclass(frozen=True)
class AuthResult:
    success: bool
    reason: str
    message: str


def _result(reason):
    return AuthResult(reason in {"verified", "available"}, reason, _MESSAGES[reason])


class _Canceled(RuntimeError):
    pass


def _cancelled(cancel):
    if cancel is None:
        return False
    try:
        return bool(cancel() if callable(cancel) else cancel.is_set())
    except Exception:
        return True


class _GUID(ctypes.Structure):
    _fields_ = [("data1", _DWORD), ("data2", ctypes.c_uint16), ("data3", ctypes.c_uint16),
                ("data4", ctypes.c_ubyte * 8)]


def _guid(value):
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _dll(name):
    # System32-only DLL lookup; never the app directory or PATH.
    return ctypes.WinDLL(name, winmode=0x800)


def _function(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype, function.argtypes = result, arguments
    return function


def _method(pointer, index, *arguments):
    if not pointer or not pointer.value:
        raise OSError("native_unavailable")
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_POINTER))).contents
    return _CALL(_INT, _POINTER, *arguments)(table[index])


def _checked(value):
    if value < 0:
        raise OSError("native_unavailable")


def _release(pointer):
    if pointer and pointer.value:
        _method(pointer, 2)(pointer)


def _supported():
    return sys.platform == "win32" and sys.getwindowsversion().build >= 22000


def _valid_window(hwnd, *, foreground=True):
    if type(hwnd) is not int or not 0 < hwnd < 1 << (ctypes.sizeof(_POINTER) * 8):
        return False
    user = _dll("user32.dll")
    if not _function(user, "IsWindow", _INT, _POINTER)(hwnd):
        return False
    pid = _DWORD()
    _function(user, "GetWindowThreadProcessId", _DWORD, _POINTER, ctypes.POINTER(_DWORD))(hwnd, ctypes.byref(pid))
    if pid.value != os.getpid():
        return False
    if not foreground:
        return True
    if not _function(user, "IsWindowVisible", _INT, _POINTER)(hwnd):
        return False
    active = _function(user, "GetForegroundWindow", _POINTER)()
    ancestor = _function(user, "GetAncestor", _POINTER, _POINTER, _DWORD)
    return bool(active and ancestor(active, 2) == ancestor(hwnd, 2))


class _NativeHello:
    """Small ABI adapter, instantiated only by an explicit check or verify."""

    def __enter__(self):
        self.combase = _dll("combase.dll")
        _checked(_function(self.combase, "RoInitialize", _INT, _DWORD)(1))
        return self

    def __exit__(self, *_):
        _function(self.combase, "RoUninitialize", None)()

    @contextmanager
    def _string(self, value):
        handle = _POINTER()
        _checked(_function(self.combase, "WindowsCreateString", _INT, ctypes.c_wchar_p,
                           _DWORD, ctypes.POINTER(_POINTER))(value, len(value), ctypes.byref(handle)))
        try:
            yield handle
        finally:
            _function(self.combase, "WindowsDeleteString", _INT, _POINTER)(handle)

    @contextmanager
    def _factory(self, interface):
        factory = _POINTER()
        with self._string(_CLASS) as name:
            _checked(_function(self.combase, "RoGetActivationFactory", _INT, _POINTER,
                               ctypes.POINTER(_GUID), ctypes.POINTER(_POINTER))(
                                   name, ctypes.byref(_guid(interface)), ctypes.byref(factory)))
        try:
            yield factory
        finally:
            _release(factory)

    def _wait(self, operation, timeout, cancel):
        info, status = _POINTER(), _INT(0)
        try:
            _checked(_method(operation, 0, ctypes.POINTER(_GUID), ctypes.POINTER(_POINTER))(
                operation, ctypes.byref(_guid(_ASYNC_INFO)), ctypes.byref(info)))
            deadline = time.monotonic() + timeout
            while True:
                if _cancelled(cancel):
                    _method(info, 9)(info)  # IAsyncInfo.Cancel
                    raise _Canceled()
                if time.monotonic() >= deadline:
                    _method(info, 9)(info)
                    raise TimeoutError()
                _checked(_method(info, 7, ctypes.POINTER(_INT))(info, ctypes.byref(status)))
                if status.value == 1:  # AsyncStatus.Completed
                    result = _INT(-1)
                    _checked(_method(operation, 8, ctypes.POINTER(_INT))(operation, ctypes.byref(result)))
                    if _cancelled(cancel):
                        raise _Canceled()
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    return result.value
                if status.value == 2:
                    raise _Canceled()
                if status.value != 0:
                    raise OSError("native_unavailable")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _method(info, 9)(info)
                    raise TimeoutError()
                time.sleep(min(0.05, remaining))
        finally:
            if info.value and status.value in {1, 2, 3}:
                _method(info, 10)(info)  # IAsyncInfo.Close, only after completion
            _release(info)
            _release(operation)

    def availability(self, timeout):
        operation = _POINTER()
        with self._factory(_STATICS) as factory:
            _checked(_method(factory, 6, ctypes.POINTER(_POINTER))(factory, ctypes.byref(operation)))
        return self._wait(operation, timeout, None)

    def verify(self, hwnd, cancel, timeout):
        if not _valid_window(hwnd):
            return "window_invalid"
        operation = _POINTER()
        with self._factory(_INTEROP) as factory, self._string(_MESSAGE) as message:
            _checked(_method(factory, 6, _POINTER, _POINTER, ctypes.POINTER(_GUID), ctypes.POINTER(_POINTER))(
                factory, hwnd, message, ctypes.byref(_guid(_RESULT_IID)), ctypes.byref(operation)))
        return self._wait(operation, timeout, cancel)


class WindowsHello:
    def __init__(self, *, _native_factory=None):
        self._native_factory = _native_factory or _NativeHello

    def _run(self, *, hwnd=None, cancel=None, timeout=5):
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0.1 <= timeout <= 90:
            return _result("invalid_request")
        if not _supported():
            return _result("unsupported")
        if _cancelled(cancel):
            return _result("canceled")
        try:
            with self._native_factory() as native:
                code = native.availability(min(timeout, 10)) if hwnd is None else native.verify(hwnd, cancel, timeout)
            if code == "window_invalid":
                return _result(code)
            if type(code) is not int:
                return _result("unavailable")
            reasons = {0: "available" if hwnd is None else "verified", 1: "device_missing", 2: "not_configured",
                       3: "policy_disabled", 4: "busy", 5: "retries_exhausted", 6: "canceled"}
            return _result(reasons.get(code, "unavailable"))
        except _Canceled:
            return _result("canceled")
        except TimeoutError:
            return _result("timeout")
        except Exception:
            return _result("unavailable")

    def availability(self, timeout=5):
        """Passive: never displays Hello, enrolls or opens a camera."""
        return self._run(timeout=timeout)

    def verify(self, hwnd, cancel=None, timeout=90):
        """Displays Windows UI. Call only after an explicit app-button action."""
        if hwnd is None:
            return _result("window_invalid")
        return self._run(hwnd=hwnd, cancel=cancel, timeout=timeout)


def windows_session_active():
    """Read-only local-console/default-input-desktop check; any failure denies."""
    if not _supported():
        return False
    try:
        kernel, user = _dll("kernel32.dll"), _dll("user32.dll")
        session = _DWORD()
        if not _function(kernel, "ProcessIdToSessionId", _INT, _DWORD, ctypes.POINTER(_DWORD))(
                os.getpid(), ctypes.byref(session)):
            return False
        active = _function(kernel, "WTSGetActiveConsoleSessionId", _DWORD)()
        if active == 0xFFFFFFFF or active != session.value:
            return False
        desktop = _function(user, "OpenInputDesktop", _POINTER, _DWORD, _INT, _DWORD)(0, 0, 1)
        if not desktop:
            return False
        try:
            name, needed = ctypes.create_unicode_buffer(128), _DWORD()
            valid = _function(user, "GetUserObjectInformationW", _INT, _POINTER, _INT,
                              _POINTER, _DWORD, ctypes.POINTER(_DWORD))(
                                  desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
            return bool(valid and 0 < needed.value <= ctypes.sizeof(name) and name.value.casefold() == "default")
        finally:
            _function(user, "CloseDesktop", _INT, _POINTER)(desktop)
    except Exception:
        return False


def _session_notifications(hwnd, enabled):
    if not _supported():
        return False
    try:
        if not _valid_window(hwnd, foreground=False):
            return False
        library = _dll("wtsapi32.dll")
        if enabled:
            return bool(_function(library, "WTSRegisterSessionNotification", _INT, _POINTER, _DWORD)(hwnd, 0))
        return bool(_function(library, "WTSUnRegisterSessionNotification", _INT, _POINTER)(hwnd))
    except Exception:
        return False


def register_session_notifications(hwnd):
    return _session_notifications(hwnd, True)


def unregister_session_notifications(hwnd):
    return _session_notifications(hwnd, False)


class OwnerSession:
    """Memory-only grant. UI must restrict disabling an enabled policy too."""

    def __init__(self, enabled=False, ttl_seconds=300, *, clock=time.monotonic, verifier=None, session_check=None):
        if type(enabled) is not bool or type(ttl_seconds) not in {int, float} or not math.isfinite(ttl_seconds) or not 1 <= ttl_seconds <= 900:
            raise ValueError("owner_session_invalid")
        self.enabled, self.ttl_seconds = enabled, ttl_seconds
        self._clock, self._verifier = clock, verifier or WindowsHello()
        self._session_check = session_check or windows_session_active
        self._lock, self._generation = threading.RLock(), 0
        self._verified_at = self._deadline = None
        self._reason = "verification_required" if enabled else "disabled"

    def _clear(self, reason):
        self._generation += 1
        self._verified_at = self._deadline = None
        self._reason = reason

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("owner_session_invalid")
        with self._lock:
            self.enabled = enabled
            self._clear("verification_required" if enabled else "disabled")

    def revoke(self):
        with self._lock:
            self._clear("verification_required" if self.enabled else "disabled")

    def handle_session_change(self, event):
        if type(event) is int and event in {2, 4, 6, 7, 8, 11}:
            with self._lock:
                self._clear("session_inactive")

    def _active(self):
        try:
            return self._session_check() is True
        except Exception:
            return False

    def _now(self):
        try:
            now = self._clock()
            return now if type(now) in {float, int} and math.isfinite(now) else None
        except Exception:
            return None

    def verify(self, hwnd, cancel=None, timeout=90):
        with self._lock:
            self._clear("verification_required")
            generation = self._generation
            if not self.enabled:
                return _result("disabled")
        try:
            result = self._verifier.verify(hwnd, cancel=cancel, timeout=timeout)
        except Exception:
            result = _result("unavailable")
        with self._lock:
            if generation != self._generation or not self.enabled:
                return _result("stale")
            if _cancelled(cancel):
                self._clear("canceled")
                return _result("canceled")
            if not isinstance(result, AuthResult) or result.success is not True or result.reason != "verified":
                reason = result.reason if isinstance(result, AuthResult) and result.reason in _MESSAGES and result.success is False else "unavailable"
                self._clear(reason)
                return _result(reason)
            now = self._now()
            if now is None or not self._active():
                self._clear("session_inactive")
                return _result("session_inactive")
            self._verified_at, self._deadline, self._reason = now, now + self.ttl_seconds, "verified"
            return _result("verified")

    def allowed(self):
        with self._lock:
            if not self.enabled:
                return True
            if self._deadline is None:
                return False
            now = self._now()
            if now is None or not self._verified_at <= now < self._deadline:
                self._clear("verification_expired")
                return False
            if not self._active():
                self._clear("session_inactive")
                return False
            return True

    def status(self):
        with self._lock:
            verified = self.enabled and self.allowed()
            now = self._now() if verified else None
            if verified and (now is None or not self._verified_at <= now < self._deadline):
                self._clear("verification_expired")
                verified = False
            remaining = max(0, math.ceil(self._deadline - now)) if verified else 0
            return {"enabled": self.enabled, "verified": bool(verified), "remaining_seconds": remaining,
                    "reason": self._reason, "method": "windows_hello", "speaker_identity_verified": False}
