"""Explicit private Tailscale Serve setup; never Funnel, login or elevation.

No process, installer, account or VPN changes occur on import/construction.
The owner must install/sign in to Tailscale on Windows and iPhone themselves.
Serve is invoked only by configure_serve(approved=True). Installer preparation
downloads data and verifies its vendor signature; it never runs that file.

Official contract: https://tailscale.com/docs/reference/tailscale-cli/serve
https://tailscale.com/docs/features/tailscale-serve
https://tailscale.com/docs/install/windows
"""
from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

from desktop.native_runtime import powershell_environment


INSTALLER_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
MAX_INSTALLER_BYTES = 100 * 1024 * 1024
MAX_OUTPUT = 512 * 1024
TARGET = "http://127.0.0.1:8767"
_MESSAGES = {
    "not_installed": "Установи Tailscale на Windows и iPhone, затем войди в одну личную сеть.",
    "untrusted_installation": "Не удалось подтвердить подпись Tailscale. Запуск не разрешён.",
    "login_required": "Войди в Tailscale через его значок Windows и подключи iPhone к той же сети.",
    "not_running": "Клиент Tailscale ещё не готов. Подключи его через значок Windows.",
    "response_invalid": "Не удалось безопасно прочитать состояние Tailscale.",
    "timeout": "Проверка Tailscale не завершилась вовремя. Настройки не подтверждены.",
    "ready": "Частный HTTPS Serve настроен для Jarvis. Подключение iPhone нужно проверить отдельно.",
    "serve_required": "Tailscale подключён. По кнопке можно настроить частный HTTPS Serve для Jarvis.",
    "approval_required": "Настройка Serve запускается только по явной кнопке владельца.",
    "serve_conflict": "Порт HTTPS443 уже используется другой конфигурацией. Автоматическая замена запрещена.",
    "needs_user_consent": "Tailscale требует согласия владельца на HTTPS. Открой страницу согласия и повтори настройку.",
    "permission_required": "Tailscale не подтвердил настройку. Проверь разрешения в его приложении; автоматического повышения прав нет.",
    "serve_unconfirmed": "Команда завершилась, но нужный частный HTTPS маршрут не подтверждён.",
    "download_failed": "Не удалось подготовить официальный установщик Tailscale.",
    "installer_ready": "Официальный установщик загружен и его подпись проверена. Установка требует отдельного действия владельца.",
}
_SIGNATURE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$path = $env:JARVIS_TAILSCALE_VERIFY_PATH
$signature = Get-AuthenticodeSignature -LiteralPath $path
$publisher = if ($signature.SignerCertificate) { $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) } else { '' }
$valid = $signature.Status -eq 'Valid' -and $publisher -in @('Tailscale Inc.', 'Tailscale Inc')
@{protocol=1;valid=[bool]$valid;sha256=if($valid){(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash}else{$null}} | ConvertTo-Json -Compress
"""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: bytes
    timed_out: bool = False
    overflow: bool = False


def _run_process(arguments, *, timeout, env, limit=MAX_OUTPUT):
    """Bound both time and pipe memory; terminate only this owned CLI child."""
    process = subprocess.Popen(arguments, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output, overflow = bytearray(), threading.Event()

    def read():
        try:
            while data := process.stdout.read1(min(4096, limit + 1 - len(output))):
                output.extend(data)
                if len(output) > limit:
                    overflow.set()
                    process.terminate()
                    break
        except OSError:
            overflow.set()

    reader = threading.Thread(target=read, name="jarvis-tailscale-output", daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    finally:
        reader.join(timeout=1)
        if reader.is_alive():
            overflow.set()
        else:
            process.stdout.close()
    return CommandResult(process.returncode if process.returncode is not None else -1,
                         bytes(output[:limit + 1]), timed_out, overflow.is_set())


def _system_directory():
    if os.name != "nt":
        raise OSError("windows_required")
    library = ctypes.WinDLL("kernel32.dll", winmode=0x800)
    function = library.GetSystemDirectoryW
    function.argtypes, function.restype = [ctypes.c_wchar_p, ctypes.c_uint], ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    count = function(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise OSError("system_directory_unavailable")
    return Path(buffer.value)


def _program_roots():
    if os.name != "nt":
        return []
    shell, ole = ctypes.WinDLL("shell32.dll", winmode=0x800), ctypes.WinDLL("ole32.dll", winmode=0x800)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes, ole.CoTaskMemFree.restype = [ctypes.c_void_p], None
    roots = []
    for value in ("905e63b6-c1bf-494e-b29c-65b732d3d21a", "7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e"):
        guid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)
        pointer = ctypes.c_void_p()
        if shell.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(pointer)) >= 0 and pointer.value:
            try:
                roots.append(Path(ctypes.wstring_at(pointer)).resolve())
            finally:
                ole.CoTaskMemFree(pointer)
    return list(dict.fromkeys(roots))


def _digest(path):
    if not path.is_file() or path.is_symlink() or path.resolve(strict=True) != path or not 128 <= path.stat().st_size <= MAX_INSTALLER_BYTES:
        raise ValueError("file_invalid")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        digest, received = hashlib.sha256(), 0
        while data := stream.read(min(65536, before.st_size + 1 - received)):
            received += len(data)
            if received > before.st_size:
                raise ValueError("binary_changed")
            digest.update(data)
        after = os.fstat(stream.fileno())
    current = path.stat()
    def identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
    if received != before.st_size or identity(before) != identity(after) or identity(after) != identity(current):
        raise ValueError("binary_changed")
    return digest.hexdigest()


def _approved(guard):
    if guard is None:
        return True
    try:
        return callable(guard) and guard() is True
    except Exception:
        return False


def _cancelled(event):
    try:
        return event is not None and event.is_set() is not False
    except Exception:
        return True


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _json(result):
    if not isinstance(result, CommandResult) or result.overflow or len(result.output) > MAX_OUTPUT:
        raise ValueError("response_invalid")
    if result.timed_out:
        raise TimeoutError()
    if result.returncode != 0:
        raise ValueError("response_invalid")
    value = json.loads(result.output.decode("utf-8-sig"), object_pairs_hook=_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    if not isinstance(value, dict):
        raise ValueError("response_invalid")
    return value


def _origin(dns):
    if not isinstance(dns, str) or not dns.isascii() or not 1 <= len(dns) <= 253:
        return None
    dns = dns.removesuffix(".")
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){2,}ts\.net", dns):
        return None
    return "https://" + dns


def _consent_url(output):
    if not isinstance(output, bytes) or len(output) > MAX_OUTPUT:
        return None
    for raw in re.findall(r"https://login\.tailscale\.com/[^\s<>\"']{1,1000}", output.decode("utf-8", errors="replace")):
        try:
            value = urlsplit(raw)
            if (value.scheme == "https" and value.netloc == "login.tailscale.com" and value.hostname == "login.tailscale.com"
                    and value.username is None and value.password is None and not value.fragment and "\\" not in raw):
                return raw
        except ValueError:
            pass
    return None


def _serve_state(value, origin):
    """Accept only our443 root proxy; never overwrite unrelated Serve/Funnel."""
    if not isinstance(value, dict):
        raise ValueError("serve_invalid")
    tcp, web, funnel = (value.get(key, {}) for key in ("TCP", "Web", "AllowFunnel"))
    tcp, web, funnel = ({} if item is None else item for item in (tcp, web, funnel))
    if not all(isinstance(item, dict) for item in (tcp, web, funnel)):
        raise ValueError("serve_invalid")
    if value.get("Foreground"):
        return "serve_conflict"
    hostname = urlsplit(origin).netloc + ":443"
    if any(not isinstance(key, str) for key in (*tcp, *web, *funnel)):
        raise ValueError("serve_invalid")
    if any(key.endswith(":443") and enabled is not False for key, enabled in funnel.items()):
        return "serve_conflict"
    hosts = [key for key in web if key.endswith(":443")]
    if "443" not in tcp and not hosts:
        return "serve_required"
    server = web.get(hostname)
    port = tcp.get("443")
    if (not isinstance(port, dict) or set(port) != {"HTTPS"} or port["HTTPS"] is not True
            or hosts != [hostname] or not isinstance(server, dict)
            or server.get("Handlers") != {"/": {"Proxy": TARGET}}):
        return "serve_conflict"
    return "ready"


class _OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not _official_download(newurl):
            raise ValueError("redirect_refused")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _official_download(url):
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == "https" and parsed.netloc in {"pkgs.tailscale.com", "dl.tailscale.com"}
                    and re.fullmatch(r"/stable/tailscale-setup-[A-Za-z0-9_.-]+\.exe", parsed.path)
                    and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


class RemoteSetup:
    def __init__(self, state_dir, *, runner=None):
        self.state_dir = Path(state_dir).resolve()
        self._runner = runner or _run_process

    def _signature(self, path):
        try:
            system = _system_directory()
            environment = powershell_environment(system)
            environment["JARVIS_TAILSCALE_VERIFY_PATH"] = str(path)
            encoded = base64.b64encode(_SIGNATURE_SCRIPT.encode("utf-16-le")).decode("ascii")
            response = self._runner([str(system / "WindowsPowerShell/v1.0/powershell.exe"), "-NoLogo", "-NoProfile",
                "-NonInteractive", "-EncodedCommand", encoded], timeout=8, env=environment, limit=8192)
            if len(response.output) > 8192:
                return None
            value = _json(response)
            expected = value.get("sha256")
            if (set(value) != {"protocol", "valid", "sha256"} or type(value["protocol"]) is not int or value["protocol"] != 1
                    or value["valid"] is not True or not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected)):
                return None
            return expected.lower() if _digest(path) == expected.lower() else None
        except Exception:
            return None

    def _discover(self):
        found = False
        for root in _program_roots():
            path = root / "Tailscale/tailscale.exe"
            if not path.is_file():
                continue
            found = True
            if path.parent.is_symlink() or getattr(path.parent, "is_junction", lambda: False)():
                continue
            digest = self._signature(path)
            if digest:
                return path, digest, "ready"
        return None, None, "untrusted_installation" if found else "not_installed"

    def _command(self, path, digest, arguments, timeout=5, *, approval_guard=None):
        if _digest(path) != digest:
            raise ValueError("binary_changed")
        if not _approved(approval_guard):
            raise PermissionError("approval_required")
        return self._runner([str(path), *arguments], timeout=timeout,
            env=powershell_environment(_system_directory()), limit=MAX_OUTPUT)

    @staticmethod
    def _status(reason, *, installed=False, logged_in=False, origin=None):
        return {"installed": installed, "logged_in": logged_in, "origin": origin, "serve_configured": reason == "ready",
                "reason": reason, "message": _MESSAGES[reason], "phone_connection_verified": False}

    def _inspect(self):
        path, digest, reason = self._discover()
        if path is None:
            return self._status(reason), None, None
        try:
            state = _json(self._command(path, digest, ["status", "--json"]))
            backend = state.get("BackendState")
            if backend in {"NeedsLogin", "NeedsMachineAuth"}:
                return self._status("login_required", installed=True), path, digest
            if backend != "Running":
                return self._status("not_running", installed=True), path, digest
            own = state.get("Self")
            origin = _origin(own.get("DNSName")) if isinstance(own, dict) else None
            if origin is None:
                return self._status("response_invalid", installed=True), path, digest
            serve = _json(self._command(path, digest, ["serve", "status", "--json"]))
            return self._status(_serve_state(serve, origin), installed=True, logged_in=True, origin=origin), path, digest
        except TimeoutError:
            return self._status("timeout", installed=True), path, digest
        except Exception:
            return self._status("response_invalid", installed=True), path, digest

    def status(self):
        """Read-only; never runs login, up, installer, Serve or opens a URL."""
        try:
            return self._inspect()[0]
        except Exception:
            return self._status("response_invalid")

    def configure_serve(self, approved=False, *, approval_guard=None):
        if approved is not True or not _approved(approval_guard):
            return self._status("approval_required")
        try:
            state, path, digest = self._inspect()
        except Exception:
            return self._status("response_invalid")
        if state["reason"] != "serve_required":
            return state
        stopped_reason = []
        try:
            if not _approved(approval_guard):
                return self._status("approval_required")
            def still_safe():
                if not _approved(approval_guard):
                    return False
                try:
                    latest = _json(self._command(path, digest, ["serve", "status", "--json"]))
                    reason = _serve_state(latest, state["origin"])
                    if reason != "serve_required":
                        stopped_reason.append(reason)
                        return False
                except TimeoutError:
                    stopped_reason.append("timeout")
                    return False
                except Exception:
                    stopped_reason.append("response_invalid")
                    return False
                return _approved(approval_guard)
            result = self._command(path, digest, ["serve", "--bg", "--https=443", TARGET], timeout=10,
                                   approval_guard=still_safe)
            if result.overflow or len(result.output) > MAX_OUTPUT:
                return self._status("response_invalid", installed=True, logged_in=True, origin=state["origin"])
            consent = _consent_url(result.output)
            if consent:
                return {**self._status("needs_user_consent", installed=True, logged_in=True, origin=state["origin"]),
                        "consent_url": consent}
            if result.timed_out:
                return self._status("timeout", installed=True, logged_in=True, origin=state["origin"])
            if result.returncode != 0:
                return self._status("permission_required", installed=True, logged_in=True, origin=state["origin"])
            verified = self.status()
            return verified if verified["serve_configured"] else self._status("serve_unconfirmed", installed=True,
                logged_in=True, origin=state["origin"])
        except PermissionError:
            return self._status(stopped_reason[-1], installed=True, logged_in=True, origin=state["origin"]) if stopped_reason else self._status("approval_required")
        except Exception:
            return self._status("serve_unconfirmed", installed=True, logged_in=True, origin=state["origin"])

    @staticmethod
    def installer_info():
        return {"url": INSTALLER_URL, "max_bytes": MAX_INSTALLER_BYTES, "requires_user_installation": True,
                "requires_windows_and_iphone_login": True, "runs_automatically": False}

    def verified_installer(self):
        """Recheck immediately before UI offers a separate user launch."""
        try:
            directory = (self.state_dir / "installers").resolve()
            if directory != self.state_dir.resolve() / "installers":
                return None
            path = directory / "tailscale-setup-latest.exe"
            digest = self._signature(path)
            return {"path": str(path), "sha256": digest} if digest else None
        except Exception:
            return None

    def download_installer(self, *, cancel_event=None):
        """Explicit fixed-URL, bounded, vendor-verified download, never launch."""
        partial = None
        try:
            if _cancelled(cancel_event):
                raise InterruptedError("download_canceled")
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = self.state_dir / "installers"
            directory.mkdir(exist_ok=True, mode=0o700)
            if directory.resolve() != self.state_dir.resolve() / "installers":
                raise ValueError("linked_directory")
            target = directory / "tailscale-setup-latest.exe"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _OfficialRedirect())
            request = urllib.request.Request(INSTALLER_URL, headers={"User-Agent": "Jarvis-Private-Setup", "Accept-Encoding": "identity"})
            if _cancelled(cancel_event):
                raise InterruptedError("download_canceled")
            descriptor, filename = tempfile.mkstemp(prefix="tailscale-", suffix=".pending.exe", dir=directory)
            partial = Path(filename)
            deadline = time.monotonic() + 180
            with os.fdopen(descriptor, "wb") as sink, opener.open(request, timeout=10) as response:
                if not _official_download(response.geturl()) or response.status != 200:
                    raise ValueError("download_origin_refused")
                length = response.headers.get("Content-Length")
                if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length) or not 128 <= int(length) <= MAX_INSTALLER_BYTES):
                    raise ValueError("download_size_refused")
                received = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or _cancelled(cancel_event):
                        raise TimeoutError("download_deadline")
                    stream = getattr(getattr(response, "fp", None), "raw", None)
                    sock = getattr(stream, "_sock", None)
                    if sock is not None:
                        sock.settimeout(min(1 if cancel_event is not None else 10, remaining))
                    data = response.read1(min(65536, MAX_INSTALLER_BYTES + 1 - received))
                    if time.monotonic() >= deadline or _cancelled(cancel_event):
                        raise TimeoutError("download_deadline")
                    if not data:
                        break
                    received += len(data)
                    if received > MAX_INSTALLER_BYTES:
                        raise ValueError("download_limit")
                    sink.write(data)
                if not 128 <= received <= MAX_INSTALLER_BYTES or length is not None and received != int(length):
                    raise ValueError("download_incomplete")
            if _cancelled(cancel_event):
                raise InterruptedError("download_canceled")
            digest = self._signature(partial)
            if digest is None:
                raise ValueError("signature_refused")
            if _cancelled(cancel_event):
                raise InterruptedError("download_canceled")
            partial.replace(target)
            partial = None
            return {"ok": True, "reason": "installer_ready", "message": _MESSAGES["installer_ready"],
                    "path": str(target.resolve()), "sha256": digest, "launched": False}
        except Exception:
            return {"ok": False, "reason": "download_failed", "message": _MESSAGES["download_failed"], "launched": False}
        finally:
            if partial is not None:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass


TailscaleSetup = RemoteSetup
