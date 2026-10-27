"""Finite Windows action allowlist; callers cannot pass shell/code/URLs."""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import uuid
from pathlib import Path


ALLOWED_ACTIONS = frozenset({
    "calculator", "notepad", "explorer", "youtube", "browser", "volume_up",
    "volume_down", "volume_mute", "volume_unmute", "minimize", "minimize_all", "lock",
    "shutdown", "restart", "hibernate", "cancel_shutdown", "maximize", "maximize_youtube",
    "music_open", "music_play", "music_morning",
})
RISKY_ACTIONS = frozenset({"lock", "shutdown", "restart", "hibernate"})


class _AudioEndpoint:
    """Windows SDK IMMDevice/IAudioEndpointVolume, released on the same thread.

    Slots and signatures follow Microsoft's endpointvolume.h/mmdeviceapi.h.
    Reading back actual mute/level avoids treating a media-key toggle as mute.
    """
    def __enter__(self):
        if os.name != "nt":
            raise RuntimeError("CoreAudio requires Windows")
        self.ole = ctypes.WinDLL("ole32")
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.ole.CoInitializeEx.restype = ctypes.c_long
        result = self.ole.CoInitializeEx(None, 0)
        self.owned = result >= 0
        self.objects = []
        if not self.owned and result & 0xFFFFFFFF != 0x80010106:
            self.check(result)
        try:
            clsid = self.guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
            enumerator_iid = self.guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
            self.ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            self.ole.CoCreateInstance.restype = ctypes.c_long
            enumerator = ctypes.c_void_p()
            self.check(self.ole.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                                ctypes.byref(enumerator_iid), ctypes.byref(enumerator)))
            self.objects.append(enumerator)
            device = ctypes.c_void_p()
            self.check(self.invoke(enumerator, 4, (ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                                   0, 1, ctypes.byref(device)))
            self.objects.append(device)
            endpoint_iid = self.guid("5CDF2C82-841E-4546-9722-0CF74078229A")
            self.endpoint = ctypes.c_void_p()
            self.check(self.invoke(device, 3, (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                                   ctypes.byref(endpoint_iid), 23, None, ctypes.byref(self.endpoint)))
            self.objects.append(self.endpoint)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    @staticmethod
    def guid(value):
        return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)

    @staticmethod
    def invoke(pointer, index, types, *args):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])(pointer, *args)

    @staticmethod
    def check(result):
        if result < 0:
            raise OSError("Windows audio endpoint operation failed")

    def read(self):
        level, muted = ctypes.c_float(), ctypes.c_int()
        self.check(self.invoke(self.endpoint, 9, (ctypes.POINTER(ctypes.c_float),), ctypes.byref(level)))
        self.check(self.invoke(self.endpoint, 15, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(muted)))
        return {"level": round(level.value * 100), "muted": bool(muted.value)}

    def set_mute(self, muted):
        self.check(self.invoke(self.endpoint, 14, (ctypes.c_int, ctypes.c_void_p), int(muted), None))

    def set_level(self, value):
        self.check(self.invoke(self.endpoint, 7, (ctypes.c_float, ctypes.c_void_p), ctypes.c_float(value), None))

    def __exit__(self, *_):
        for pointer in reversed(self.objects):
            self.invoke(pointer, 2, ())
        if self.owned:
            self.ole.CoUninitialize()


def read_audio_status() -> dict:
    """Read-only diagnostic, never changes a volume/mute setting."""
    with _AudioEndpoint() as endpoint:
        return endpoint.read()


def _maximize_window(user32, *, youtube_only=False) -> str:
    """Finite window geometry action; never sends guessed browser/video keys.

    Window class and title identify a candidate, not a verified web origin.
    Captions are inspected transiently and are not returned, logged or stored.
    https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-enumwindows
    """
    for name in ("GetForegroundWindow", "ShowWindow", "IsZoomed", "SetForegroundWindow"):
        method = getattr(user32, name)
        method.restype = ctypes.c_void_p if name == "GetForegroundWindow" else ctypes.c_int
        method.argtypes = [] if name == "GetForegroundWindow" else [ctypes.c_void_p, ctypes.c_int] if name == "ShowWindow" else [ctypes.c_void_p]

    def youtube_candidate(window):
        if not user32.IsWindowVisible(window):
            return False
        class_name = ctypes.create_unicode_buffer(256)
        if not user32.GetClassNameW(window, class_name, len(class_name)):
            return False
        if class_name.value not in {"Chrome_WidgetWin_1", "MozillaWindowClass"}:
            return False
        title = ctypes.create_unicode_buffer(513)
        if not user32.GetWindowTextW(window, title, len(title)):
            return False
        return bool(re.search(r"\byoutube\b|\bютуб\b", title.value, re.IGNORECASE))

    if youtube_only:
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.IsWindowVisible.restype = ctypes.c_int
        for name in ("GetClassNameW", "GetWindowTextW"):
            method = getattr(user32, name)
            method.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            method.restype = ctypes.c_int
        candidates = []
        inspected, limited = 0, False
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)

        @callback_type
        def inspect(window, _parameter):
            nonlocal inspected, limited
            inspected += 1
            if inspected > 512:
                limited = True
                return 0
            if youtube_candidate(window):
                candidates.append(window)
            return 1

        user32.EnumWindows.argtypes = [callback_type, ctypes.c_ssize_t]
        user32.EnumWindows.restype = ctypes.c_int
        if not user32.EnumWindows(inspect, 0) or limited:
            return "Не удалось полностью проверить окна. Выбери нужное окно и скажи «разверни окно»."
        if not candidates:
            return "Не нашёл видимое окно браузера с «YouTube» в заголовке. Открой нужную вкладку и повтори команду."
        if len(candidates) != 1:
            return "Нашёл несколько окон браузера с «YouTube» в заголовке. Выбери нужное и скажи «разверни окно»."
        window = candidates[0]
        # Recheck after enumeration in case the selected browser changed tabs
        # or closed before the geometry action.
        if not youtube_candidate(window):
            return "Выбранное окно изменилось. Разворачивание не выполнено; повтори команду."
    else:
        window = user32.GetForegroundWindow()
        if not window:
            return "Не нашёл активное окно для разворачивания."
    # ShowWindow returns prior visibility, not operation success. Read back
    # IsZoomed before claiming a maximized window.
    user32.ShowWindow(window, 3)
    if not user32.IsZoomed(window):
        return "Windows не подтвердила разворачивание окна. Проверь его состояние."
    user32.SetForegroundWindow(window)
    target = "окно браузера с «YouTube» в заголовке" if youtube_only else "активное окно"
    result = f"Развернул {target}."
    if user32.GetForegroundWindow() != window:
        result += " Вывод на передний план Windows не подтвердила."
    return result


def execute_windows_action(action: str) -> str:
    if action not in ALLOWED_ACTIONS:
        raise ValueError("Unsupported desktop action")
    if os.name != "nt":
        raise RuntimeError("Windows action unavailable on this system")
    buffer = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not count or count >= len(buffer):
        raise OSError("System directory unavailable")
    system = Path(buffer.value)
    if action in {"music_open", "music_play", "music_morning"}:
        from desktop.media import execute_music_action
        return execute_music_action(action, system)
    if action in {"calculator", "notepad", "explorer"}:
        executable = system.parent / "explorer.exe" if action == "explorer" else system / {"calculator": "calc.exe", "notepad": "notepad.exe"}[action]
        subprocess.Popen([str(executable)], shell=False, close_fds=True)
        return {"calculator": "Запустил калькулятор.", "notepad": "Запустил блокнот.", "explorer": "Открыл проводник."}[action]
    if action in {"youtube", "browser"}:
        os.startfile("https://www.youtube.com" if action == "youtube" else "https://www.google.com")
        return "Передал открытие YouTube браузеру." if action == "youtube" else "Передал открытие браузеру."
    user32 = ctypes.windll.user32
    if action in {"maximize", "maximize_youtube"}:
        return _maximize_window(user32, youtube_only=action == "maximize_youtube")
    if action in {"volume_up", "volume_down", "volume_mute", "volume_unmute"}:
        with _AudioEndpoint() as endpoint:
            before = endpoint.read()
            if action in {"volume_mute", "volume_unmute"}:
                endpoint.set_mute(action == "volume_mute")
            else:
                delta = 5 if action == "volume_up" else -5
                endpoint.set_level(max(0, min(100, before["level"] + delta)) / 100)
            after = endpoint.read()
        if action in {"volume_mute", "volume_unmute"}:
            if after["muted"] != (action == "volume_mute"):
                raise OSError("Mute setting not confirmed")
            return "Звук выключен." if after["muted"] else "Звук включен."
        return f"Громкость {after['level']} процентов." + (" Звук пока выключен; скажи «включи звук»." if after["muted"] else "")
    if action == "minimize":
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        window = user32.GetForegroundWindow()
        if not window:
            raise OSError("No foreground window")
        user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        if not user32.ShowWindowAsync(window, 6):
            raise OSError("Window did not accept minimize")
        return "Команда сворачивания отправлена активному окну."
    if action == "minimize_all":
        for key, flags in ((0x5B, 0), (0x4D, 0), (0x4D, 2), (0x5B, 2)):
            user32.keybd_event(key, 0, flags, 0)
        return "Команда свернуть все окна отправлена Windows."
    if action == "lock":
        if not user32.LockWorkStation():
            raise OSError("LockWorkStation failed")
        return "Windows приняла команду блокировки."
    arguments = {
        "shutdown": ["/s", "/t", "30"], "restart": ["/r", "/t", "30"],
        "hibernate": ["/h"], "cancel_shutdown": ["/a"],
    }[action]
    subprocess.run([str(system / "shutdown.exe"), *arguments], shell=False, check=True,
                   timeout=15, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if action == "cancel_shutdown":
        return "Windows подтвердила отмену запланированного выключения или перезагрузки."
    if action == "hibernate":
        return "Windows приняла команду гибернации."
    return "Windows запланировала " + ("выключение" if action == "shutdown" else "перезагрузку") + " через 30 секунд. Отмена: «отмени выключение»."
