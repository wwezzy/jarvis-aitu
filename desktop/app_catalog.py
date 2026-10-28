"""Explicit owner approval of local GUI executables, pinned to their bytes.

Only trusted local settings should call register_app(approval=True). A model,
remote request, chat phrase, scan or imported document cannot approve apps.
There are no arguments, shell commands, URLs, shortcuts or launch templates.
An updated executable requires a fresh explicit approval; it is never run on
the strength of an old path alone. This is an allowlist, not an app sandbox.
"""
from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import time
from contextlib import closing, contextmanager

from desktop.native_runtime import powershell_environment
from desktop.project_catalog import _folder, _identifier, _key, _name, _require_authorization, _system_directory


MAX_APPS = 100
MAX_EXE_BYTES = 512 * 1024 * 1024
_BLOCKED = frozenset({
    "cmd", "command", "powershell", "powershell_ise", "pwsh", "wscript", "cscript", "mshta", "rundll32", "regsvr32",
    "bash", "sh", "dash", "zsh", "fish", "wsl", "wslhost", "git-bash", "mintty", "busybox", "conhost", "wt", "windowsterminal", "terminal",
    "node", "nodejs", "deno", "bun", "ruby", "perl", "php", "java", "javaw", "dotnet", "csi", "fsi", "csc", "vbc", "msbuild",
    "cmake", "make", "ninja", "gcc", "clang", "rustc", "cargo", "jupyter", "ipython", "py", "pyw",
    "reg", "regedit", "sc", "schtasks", "wmic", "msiexec", "installutil", "certutil", "curl", "wget", "bitsadmin",
    "ssh", "sftp", "ftp", "net", "netsh", "taskkill", "shutdown", "format", "diskpart", "bcdedit", "vssadmin",
    "setup", "installer", "update", "updater", "uninstall",
})


def _blocked(filename):
    stem = ntpath.basename(filename).casefold().removesuffix(".exe")
    return stem in _BLOCKED or bool(re.fullmatch(r"(?:pythonw?|pypyw?)(?:[0-9]+(?:\.[0-9]+)*)?t?(?:_d)?|unins\d*", stem))


def _executable(raw):
    value = os.fspath(raw)
    if (not isinstance(value, str) or len(value) > 2048 or "\x00" in value or "://" in value
            or value.startswith(("\\\\", "//"))):
        raise ValueError("Choose a local executable file")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.suffix.casefold() != ".exe" or _blocked(path.name):
        raise ValueError("Shells, runtimes, scripts and launch arguments cannot be approved")
    _folder(path.parent)
    if path.resolve(strict=True) != path or not path.is_file() or path.is_symlink():
        raise ValueError("Choose an existing local executable, not a link")
    return path


def _fingerprint(path):
    """Bounded PE format check and SHA, without importing or executing a file."""
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not 128 <= before.st_size <= MAX_EXE_BYTES:
            raise ValueError("Executable size is outside the supported range")
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("Not a Windows executable")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= min(1024 * 1024, before.st_size - 26):
            raise ValueError("Invalid executable header")
        stream.seek(offset)
        coff, magic_bytes = stream.read(24), stream.read(2)
        if len(coff) != 24 or len(magic_bytes) != 2:
            raise ValueError("Executable changed while reading its header")
        signature, machine, _sections, _stamp, _table, _symbols, optional_size, characteristics = struct.unpack("<4sHHIIIHH", coff)
        magic = struct.unpack("<H", magic_bytes)[0]
        if (signature != b"PE\x00\x00" or machine not in {0x14C, 0x8664, 0xAA64} or optional_size < 2
                or offset + 24 + optional_size > before.st_size or magic not in {0x10B, 0x20B}
                or not characteristics & 2 or characteristics & 0x2000):
            raise ValueError("Not a supported Windows application")
        stream.seek(0)
        digest, read = hashlib.sha256(), 0
        while True:
            chunk = stream.read(min(1024 * 1024, before.st_size + 1 - read))
            if not chunk:
                break
            read += len(chunk)
            if read > before.st_size:
                raise ValueError("Executable grew while checking it")
            digest.update(chunk)
        if read != before.st_size:
            raise ValueError("Executable changed while checking it")
        after = os.fstat(stream.fileno())
    current = path.stat()
    def identity(item):
        return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise ValueError("Executable changed while checking it")
    return digest.hexdigest(), current.st_size


def _original_filename(path):
    """Read Windows version resource; a renamed known runtime is still denied."""
    if os.name != "nt":
        raise RuntimeError("Native executable approval requires Windows")
    version = ctypes.WinDLL("version.dll", use_last_error=True, winmode=0x800)
    version.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
    version.GetFileVersionInfoSizeW.restype = ctypes.c_uint
    version.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = ctypes.c_int
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
    version.VerQueryValueW.restype = ctypes.c_int
    ignored = ctypes.c_uint()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size:
        return None
    if size > 1024 * 1024:
        raise ValueError("Executable version information is too large")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise OSError("Cannot read executable identity")
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    translations = [(0x0409, 0x04B0), (0x0409, 0x04E4)]
    if version.VerQueryValueW(buffer, r"\VarFileInfo\Translation", ctypes.byref(pointer), ctypes.byref(length)) and 4 <= length.value <= 256:
        values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))
        translations = [(values[index], values[index + 1]) for index in range(0, min(length.value // 2, 16) - 1, 2)] + translations
    for language, page in translations:
        query = f"\\StringFileInfo\\{language:04x}{page:04x}\\OriginalFilename"
        if version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)) and pointer.value and 1 <= length.value <= 512:
            original = ctypes.wstring_at(pointer, length.value).rstrip("\x00")
            if original:
                return original
    return None


class AppCatalog:
    """Per-call SQLite handles; no registrations or application scans by default.

    Optional inspector(path)->original_filename and executor(path)->None are
    trusted native/test adapters, never supplied from text or imported data.
    ``resolve`` is exact; suggestions never authorize a launch. ``apps`` does
    not claim registered bytes are current; open_app verifies them again.
    """

    def __init__(self, state_dir: Path, *, executor=None, inspector=None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / "app_catalog.db"
        self.executor = executor
        self.inspector = inspector or _original_filename
        with self._connection() as database:
            if database.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise RuntimeError("App catalog is newer than this application")
            database.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS approved_apps (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, sha256 TEXT NOT NULL CHECK(length(sha256)=64),
                    size INTEGER NOT NULL, approved_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approved_app_aliases (
                    app_id INTEGER NOT NULL REFERENCES approved_apps(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL, normalized TEXT NOT NULL,
                    PRIMARY KEY(app_id,normalized)
                );
                CREATE INDEX IF NOT EXISTS approved_app_alias_lookup ON approved_app_aliases(normalized);
                CREATE TABLE IF NOT EXISTS app_sequence (
                    id INTEGER PRIMARY KEY CHECK(id=1), next_id INTEGER NOT NULL CHECK(next_id>0)
                );
                INSERT OR IGNORE INTO app_sequence SELECT 1,COALESCE(MAX(id),0)+1 FROM approved_apps;
                UPDATE app_sequence SET next_id=max(next_id,COALESCE((SELECT MAX(id)+1 FROM approved_apps),1)) WHERE id=1;
                PRAGMA user_version=2;
                COMMIT;
            """)

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as database:
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys=ON")
            with database:
                yield database

    def register_app(self, path, alias, *, approval=False, approval_guard=None):
        if approval is not True:
            raise PermissionError("Approve executable selection in local owner settings")
        def require_current_approval():
            if approval_guard is None:
                return
            try:
                allowed = callable(approval_guard) and approval_guard() is True
            except Exception:
                allowed = False
            if not allowed:
                raise PermissionError("Owner approval is no longer active")
        require_current_approval()
        title, key = _name(alias), _key(alias)
        executable = _executable(path)
        digest, size = _fingerprint(executable)
        original = self.inspector(executable)
        if original is not None and (not isinstance(original, str) or not original or len(original) > 512 or _blocked(original)):
            raise ValueError("Shells and runtimes cannot be registered by renaming them")
        if _fingerprint(executable) != (digest, size):
            raise ValueError("Executable changed during approval")
        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            require_current_approval()
            row = database.execute("SELECT id FROM approved_apps WHERE path=?", (str(executable),)).fetchone()
            if row is None:
                if database.execute("SELECT count(*) FROM approved_apps").fetchone()[0] >= MAX_APPS:
                    raise ValueError("At most 100 applications may be approved")
                app_id = database.execute("SELECT next_id FROM app_sequence WHERE id=1").fetchone()[0]
                database.execute("UPDATE app_sequence SET next_id=next_id+1 WHERE id=1")
                database.execute("INSERT INTO approved_apps(id,path,name,sha256,size,approved_at) VALUES(?,?,?,?,?,?)", (app_id, str(executable), title, digest, size, time.time()))
            else:
                app_id = row["id"]
                database.execute("UPDATE approved_apps SET sha256=?,size=?,approved_at=? WHERE id=?", (digest, size, time.time(), app_id))
            count = database.execute("SELECT count(*) FROM approved_app_aliases WHERE app_id=?", (app_id,)).fetchone()[0]
            if count >= 10 and not database.execute("SELECT 1 FROM approved_app_aliases WHERE app_id=? AND normalized=?", (app_id, key)).fetchone():
                raise ValueError("At most 10 aliases per app")
            database.execute("INSERT OR IGNORE INTO approved_app_aliases VALUES(?,?,?)", (app_id, title, key))
            require_current_approval()  # A revocation rolls back all permission changes.
        return next(app for app in self.apps() if app["id"] == app_id)

    def rename(self, app_id, name, *, approval_guard=None):
        _require_authorization(approval_guard)
        title = _name(name)
        with self._connection() as database:
            if not database.execute("UPDATE approved_apps SET name=? WHERE id=?", (title, _identifier(app_id))).rowcount:
                raise ValueError("App not found")
            _require_authorization(approval_guard)
        return next(app for app in self.apps() if app["id"] == app_id)

    def set_aliases(self, app_id, aliases, *, approval_guard=None):
        _require_authorization(approval_guard)
        app_id = _identifier(app_id)
        if not isinstance(aliases, (list, tuple)) or len(aliases) > 10:
            raise ValueError("Provide at most 10 app aliases")
        values = {_key(alias): _name(alias) for alias in aliases}
        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            _require_authorization(approval_guard)
            if not database.execute("SELECT 1 FROM approved_apps WHERE id=?", (app_id,)).fetchone():
                raise ValueError("App not found")
            database.execute("DELETE FROM approved_app_aliases WHERE app_id=?", (app_id,))
            database.executemany("INSERT INTO approved_app_aliases VALUES(?,?,?)", ((app_id, alias, key) for key, alias in values.items()))
            _require_authorization(approval_guard)

    def reapprove(self, app_id, *, approval=False, approval_guard=None):
        app_id = _identifier(app_id)
        if approval is not True:
            raise PermissionError("Approve executable selection in local owner settings")
        _require_authorization(approval_guard)
        with self._connection() as database:
            row = database.execute("SELECT path FROM approved_apps WHERE id=?", (app_id,)).fetchone()
        if row is None:
            raise ValueError("App no longer exists")
        path = row["path"]
        executable = _executable(path)
        digest, size = _fingerprint(executable)
        original = self.inspector(executable)
        if original is not None and (not isinstance(original, str) or not original or len(original) > 512 or _blocked(original)):
            raise ValueError("Shells and runtimes cannot be registered by renaming them")
        if _fingerprint(executable) != (digest, size):
            raise ValueError("Executable changed during approval")
        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            _require_authorization(approval_guard)
            updated = database.execute("UPDATE approved_apps SET sha256=?,size=?,approved_at=? WHERE id=? AND path=?", (digest, size, time.time(), app_id, path)).rowcount
            if not updated:
                raise ValueError("App changed or was removed during approval")
            _require_authorization(approval_guard)
        return next(app for app in self.apps() if app["id"] == app_id)

    def apps(self):
        with self._connection() as database:
            rows = database.execute("SELECT * FROM approved_apps ORDER BY id LIMIT 100").fetchall()
            aliases = database.execute("SELECT * FROM approved_app_aliases ORDER BY alias LIMIT 1000").fetchall()
        result = []
        for row in rows:
            app = dict(row)
            app["aliases"] = [alias["alias"] for alias in aliases if alias["app_id"] == app["id"]]
            app["state"] = "registered"  # Hash is verified at dispatch, never assumed from a list.
            result.append(app)
        return result

    def resolve(self, name):
        key = _key(name)
        apps = self.apps()
        matches = [app for app in apps if key in {_key(app["name"]), *(_key(alias) for alias in app["aliases"])}]
        if len(matches) == 1:
            return {"status": "found", "app": matches[0], "suggestions": []}
        suggestions = matches or [app for app in apps if any(key in _key(candidate) for candidate in (app["name"], *app["aliases"]))][:5]
        return {"status": "ambiguous" if matches else "missing", "app": None, "suggestions": suggestions[:5]}

    def remove(self, app_id):
        with self._connection() as database:
            return bool(database.execute("DELETE FROM approved_apps WHERE id=?", (_identifier(app_id),)).rowcount)

    def open_app(self, name, *, executor=None, authorization_guard=None):
        try:
            _require_authorization(authorization_guard)
        except PermissionError:
            return {"status": "denied", "app": None, "suggestions": [], "message": "Доступ владельца истёк или отозван. Подтверди владельца заново."}
        result = self.resolve(name)
        if result["status"] != "found":
            result["message"] = ("Нашёл несколько приложений. Уточни уникальный псевдоним." if result["status"] == "ambiguous" else
                                 "Приложение не добавлено. Выбери его файл через настройки и подтверди доступ.")
            return result
        return self._open_registered(result, executor, authorization_guard)

    def open_app_id(self, app_id, *, executor=None, authorization_guard=None):
        app_id = _identifier(app_id)
        try:
            _require_authorization(authorization_guard)
        except PermissionError:
            return {"status": "denied", "app": None, "suggestions": [], "message": "Доступ владельца истёк или отозван. Подтверди владельца заново."}
        app = next((app for app in self.apps() if app["id"] == app_id), None)
        if app is None:
            return {"status": "missing", "app": None, "suggestions": [], "message": "Это приложение удалено из каталога. Обнови список и выбери его заново."}
        return self._open_registered({"status": "found", "app": app, "suggestions": []}, executor, authorization_guard)

    def _open_registered(self, result, executor, authorization_guard):
        app = result["app"]
        def current_access():
            _require_authorization(authorization_guard)
            with self._connection() as database:
                row = database.execute("SELECT path,sha256,size FROM approved_apps WHERE id=?", (app["id"],)).fetchone()
            return (row is not None and row["path"] == app["path"] and row["sha256"] == app["sha256"]
                    and row["size"] == app["size"] and _executable(row["path"]) == Path(app["path"]))
        try:
            executable = _executable(app["path"])
            fingerprint = _fingerprint(executable)
            _require_authorization(current_access)
            if fingerprint != (app["sha256"], app["size"]):
                return {**result, "status": "changed", "message": "Файл приложения изменился. Выбери и подтверди его заново в настройках; сейчас я его не запускаю."}
            custom_executor = executor or self.executor
            if custom_executor is not None:
                custom_executor(executable)
            else:
                _native_launch(executable, authorization_guard=current_access)
        except PermissionError:
            return {**result, "status": "denied", "message": "Доступ к приложению изменился или отозван. Обнови список и при необходимости подтверди владельца."}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, struct.error):
            return {**result, "status": "failed", "message": "Не удалось запустить приложение. Проверь файл и повторно подтверди его в настройках."}
        return {**result, "status": "requested", "message": f"Запускаю «{app['name']}»."}


def _native_launch(executable, *, authorization_guard=None):
    system = _system_directory()
    _require_authorization(authorization_guard)
    subprocess.Popen([str(executable)], shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, cwd=str(system), env=powershell_environment(system))
