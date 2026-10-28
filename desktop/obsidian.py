"""Explicit local Obsidian vault, with create-only Markdown and finite URIs.

Version-1 profile configuration is {version:1,vault:absolute_path,vault_id:hex}.
Only a trusted local folder selection may configure it. No plugin, frontmatter,
Markdown link, note instruction, clipboard, callback or model is interpreted.
Search checks bounded file-name/preview projections, not the whole filesystem.
Missing/moved vaults, links, malformed config and failed writes fail closed;
an existing note is never replaced. No network/service starts on import.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import unicodedata
from urllib.parse import urlencode, quote


MAX_CONFIG_BYTES = 8192
MAX_CONTENT_CHARS = 20000
PREVIEW_CHARS = 2000
PREVIEW_BYTES = 16384
MAX_ENTRIES = 2000
MAX_NOTES = 200
MAX_DEPTH = 4
_EXCLUDED = {"node_modules", "venv", "__pycache__"}


def _allowed(guard):
    try:
        return guard is None or callable(guard) and guard() is True
    except Exception:
        return False


def _directory(value, *, create=False):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw or len(raw) > 2048 or "\x00" in raw
            or raw.startswith(("\\\\", "//")) or "://" in raw):
        raise ValueError("Local folder required")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Absolute folder required")
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("Linked folder refused")
    if path.resolve() != path:
        raise ValueError("Redirected folder refused")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return _directory(path)
    if not path.is_dir():
        raise ValueError("Existing folder required")
    return path


def _identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ValueError("Linked/nonregular file refused")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _title(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise ValueError("Title must contain 1..100 characters")
    value = unicodedata.normalize("NFC", value)
    if (not value.strip() or value != value.strip() or value.startswith(".") or value.endswith((".", " "))
            or any(unicodedata.category(char).startswith("C") or char in '/\\:*?"<>|' for char in value)
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])", value.split(".", 1)[0], re.I)):
        raise ValueError("Use a title, not a Windows path or reserved name")
    return value


def _bounded_limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Invalid result limit")
    return value


def _failed(status, message):
    return {"status": status, "message": message}


def _discard_temporary(path, identity):
    """Clean only our own temp inode; a failed cleanup never exposes a path."""
    if path is None or identity is None:
        return
    try:
        _directory(path.parent)
        current = path.lstat()
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except (OSError, ValueError):
        # A broken/full/unavailable filesystem can retain a private temp file.
        # Never delete another file or walk redirected folders to repair it.
        pass


class ObsidianVault:
    """No I/O in constructor; only explicit methods read or change local data.

    IDs identify a relative file in this configured vault, not arbitrary paths.
    Renaming a note changes its ID. A different vault configuration cannot reuse
    a previous vault's note IDs. All result text is literal, never a command.
    """
    def __init__(self, state_dir, *, opener=None):
        self.state_dir = Path(state_dir)
        self.opener = opener
        self._lock = threading.RLock()
        self._closed = threading.Event()

    def _config_path(self, *, create=False):
        return _directory(self.state_dir, create=create) / "obsidian.json"

    def _configuration(self):
        if self._closed.is_set():
            raise ValueError("Adapter closed")
        path = self._config_path()
        before = _identity(path.lstat())
        if before[2] > MAX_CONFIG_BYTES:
            raise ValueError("Oversized configuration")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as source:
            if _identity(os.fstat(source.fileno())) != before:
                raise ValueError("Configuration changed")
            raw = source.read(MAX_CONFIG_BYTES + 1)
            after = _identity(os.fstat(source.fileno()))
        if before != after or after != _identity(path.lstat()) or len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("Configuration changed")
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, dict) or set(value) != {"version", "vault", "vault_id"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["vault_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["vault_id"])):
            raise ValueError("Unsupported vault configuration")
        root = _directory(value["vault"])
        home = Path.home().resolve()
        if root == Path(root.anchor) or root == home or root in home.parents:
            raise ValueError("Choose one vault, not the home folder/drive")
        return value, root, before

    def _current(self, configuration, guard=None):
        if self._closed.is_set() or not _allowed(guard):
            return False
        try:
            latest, _root, _identity_value = self._configuration()
            return latest == configuration and not self._closed.is_set() and _allowed(guard)
        except (OSError, ValueError, TypeError):
            return False

    def status(self):
        try:
            configuration, _root, _info = self._configuration()
            return {"status": "configured", "configured": True, "vault": configuration["vault"],
                    "message": "Obsidian подключён к выбранной папке. Поиск проверяет имена и краткие фрагменты заметок."}
        except FileNotFoundError:
            return {"status": "not_configured", "configured": False, "message": "Выбери папку хранилища Obsidian в настройках."}
        except (OSError, ValueError, TypeError):
            return {"status": "unavailable", "configured": False, "message": "Папка Obsidian или её настройка недоступна. Выбери существующее локальное хранилище заново."}

    def configure_vault(self, path, *, approved=False, authorization_guard=None):
        if approved is not True or self._closed.is_set() or not _allowed(authorization_guard):
            return _failed("refused", "Выбор папки Obsidian должен подтвердить владелец в настройках.")
        temporary, temporary_identity = None, None
        try:
            root = _directory(path)
            home = Path.home().resolve()
            if root == Path(root.anchor) or root == home or root in home.parents:
                raise ValueError("Whole-home vault refused")
            with self._lock:
                target = self._config_path(create=True)
                try:
                    old_identity = _identity(target.lstat())
                except FileNotFoundError:
                    old_identity = None
                # An existing valid same-vault selection keeps stable note IDs.
                try:
                    existing, _root, _info = self._configuration()
                except (OSError, ValueError, TypeError):
                    existing = None
                identifier = existing["vault_id"] if existing and existing["vault"] == str(root) else secrets.token_hex(16)
                configuration = {"version": 1, "vault": str(root), "vault_id": identifier}
                raw = (json.dumps(configuration, ensure_ascii=False) + "\n").encode("utf-8")
                if len(raw) > MAX_CONFIG_BYTES:
                    raise ValueError("Configuration too large")
                descriptor, name = tempfile.mkstemp(prefix=".obsidian.", suffix=".tmp", dir=self.state_dir)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as output:
                    info = os.fstat(output.fileno())
                    temporary_identity = info.st_dev, info.st_ino
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                _directory(root)
                _directory(self.state_dir)
                try:
                    current_identity = _identity(target.lstat())
                except FileNotFoundError:
                    current_identity = None
                if current_identity != old_identity or self._closed.is_set() or not _allowed(authorization_guard):
                    raise ValueError("Vault selection changed")
                os.replace(temporary, target)
            return _failed("configured", "Подключил выбранную папку Obsidian. Существующие заметки Jarvis сохранены отдельно.")
        except (OSError, ValueError, TypeError):
            return _failed("refused", "Не удалось безопасно подключить папку. Выбери существующее локальное хранилище без ссылок и перенаправлений.")
        finally:
            _discard_temporary(temporary, temporary_identity)

    @staticmethod
    def _note_id(configuration, relative):
        return hashlib.sha256((configuration["vault_id"] + "\0" + relative).encode("utf-8")).hexdigest()[:24]

    def _notes(self, configuration, root):
        result, pending, inspected = [], [(root, 0)], 0
        while pending and inspected < MAX_ENTRIES and len(result) < MAX_NOTES:
            directory, depth = pending.pop()
            _directory(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    inspected += 1
                    if inspected > MAX_ENTRIES or len(result) >= MAX_NOTES:
                        break
                    if entry.name.startswith(".") or entry.name.casefold() in _EXCLUDED or entry.is_symlink():
                        continue
                    path = Path(entry.path)
                    # Windows DirEntry.stat caches zero st_nlink/st_ino values;
                    # lstat performs the real identity/link-count query.
                    info = path.lstat()
                    if getattr(info, "st_file_attributes", 0) & (getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) | getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if depth < MAX_DEPTH:
                            pending.append((path, depth + 1))
                    elif stat.S_ISREG(info.st_mode) and path.suffix.casefold() == ".md" and info.st_nlink == 1:
                        relative = path.relative_to(root).as_posix()
                        result.append({"id": self._note_id(configuration, relative), "title": re.sub(r"-[a-f0-9]{16}$", "", path.stem),
                                       "relative_path": relative, "modified_at": info.st_mtime})
        return sorted(result, key=lambda item: item["relative_path"].casefold())

    def list_notes(self, limit=30):
        limit = _bounded_limit(limit, 100)
        try:
            configuration, root, _info = self._configuration()
            result = self._notes(configuration, root)[:limit]
            return result if self._current(configuration) else []
        except (OSError, ValueError, TypeError):
            return []

    def _read_note(self, root, item):
        path = root / item["relative_path"]
        _directory(path.parent)
        if not path.is_relative_to(root) or path.resolve(strict=True) != path:
            raise ValueError("Redirected note")
        before = _identity(path.lstat())
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as source:
            if _identity(os.fstat(source.fileno())) != before:
                raise ValueError("Note changed")
            raw = source.read(PREVIEW_BYTES)
            after = _identity(os.fstat(source.fileno()))
        if before != after or after != _identity(path.lstat()):
            raise ValueError("Note changed")
        preview = codecs.getincrementaldecoder("utf-8-sig")().decode(raw, final=before[2] <= len(raw))
        if "\x00" in preview:
            raise ValueError("Binary note refused")
        return {**item, "status": "read", "preview": preview[:PREVIEW_CHARS],
                "truncated": before[2] > len(raw) or len(preview) > PREVIEW_CHARS}

    def read(self, note_id):
        if not isinstance(note_id, str) or not re.fullmatch(r"[a-f0-9]{24}", note_id):
            return _failed("missing", "Выбери заметку из списка Obsidian.")
        try:
            configuration, root, _info = self._configuration()
            item = next((item for item in self._notes(configuration, root) if item["id"] == note_id), None)
            if item is None:
                return _failed("missing", "Заметка не найдена в выбранном хранилище. Обнови список.")
            result = self._read_note(root, item)
            return result if self._current(configuration) else _failed("changed", "Выбранное хранилище изменилось. Повтори поиск.")
        except (OSError, ValueError, TypeError):
            return _failed("unavailable", "Не удалось безопасно прочитать эту текстовую заметку.")

    def search(self, query, limit=20):
        limit = _bounded_limit(limit, 100)
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or "\x00" in query:
            return []
        try:
            configuration, root, _info = self._configuration()
            matches = []
            for item in self._notes(configuration, root):
                try:
                    note = self._read_note(root, item)
                except (OSError, ValueError):
                    continue
                if query.strip().casefold() in (note["title"] + "\n" + note["preview"]).casefold():
                    matches.append(note)
                if len(matches) >= limit:
                    break
            return matches if self._current(configuration) else []
        except (OSError, ValueError, TypeError):
            return []

    def create_note(self, title, content, *, authorization_guard=None):
        temporary, temporary_identity = None, None
        try:
            title = _title(title)
            if not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT_CHARS or "\x00" in content:
                raise ValueError("Plain note must contain 1..20000 characters")
            configuration, root, _info = self._configuration()
            if not self._current(configuration, authorization_guard):
                raise ValueError("Authorization revoked")
            inbox = root / "Inbox"
            inbox.mkdir(mode=0o700, exist_ok=True)
            _directory(inbox)
            descriptor, name = tempfile.mkstemp(prefix=".jarvis-note.", suffix=".tmp", dir=inbox)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as output:
                info = os.fstat(output.fileno())
                temporary_identity = info.st_dev, info.st_ino
                _directory(inbox)
                if temporary.resolve(strict=True) != temporary:
                    raise ValueError("Temporary note redirected")
                output.write(content.encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
            with self._lock:
                if not self._current(configuration, authorization_guard):
                    raise ValueError("Authorization revoked")
                _directory(inbox)
                _identity(temporary.lstat())
                for _attempt in range(8):
                    target = inbox / f"{title}-{secrets.token_hex(8)}.md"
                    try:
                        if os.name == "nt":
                            # Windows rename fails if the target exists. POSIX
                            # rename overwrites, so use create-only hardlink
                            # publication there instead; neither uses replace.
                            os.rename(temporary, target)
                        else:
                            os.link(temporary, target, follow_symlinks=False)
                            temporary.unlink()
                        break
                    except FileExistsError:
                        continue
                else:
                    raise ValueError("Unique note name unavailable")
                relative = target.relative_to(root).as_posix()
                _identity(target.lstat())
            return {"status": "created", "id": self._note_id(configuration, relative), "title": title,
                    "relative_path": relative, "message": "Создал новую заметку в папке Inbox выбранного хранилища Obsidian."}
        except (OSError, ValueError, TypeError):
            return _failed("refused", "Не удалось создать заметку. Проверь выбранную папку, разрешение владельца, заголовок и текст до 20 000 символов. Существующие заметки не заменялись.")
        finally:
            _discard_temporary(temporary, temporary_identity)

    def _open(self, configuration, root, path, guard):
        if not self._current(configuration, guard):
            return _failed("refused", "Доступ или выбранная папка изменились. Открытие отменено.")
        _directory(path if path == root else path.parent)
        if path != root:
            _identity(path.lstat())
        uri = "obsidian://open?" + urlencode({"path": str(path)}, quote_via=quote, safe="")
        if not self._current(configuration, guard):
            return _failed("refused", "Разрешение отозвано. Открытие отменено.")
        if self.opener is not None:
            self.opener(uri)
        elif os.name == "nt":
            os.startfile(uri)
        else:
            raise OSError("No Obsidian URI handler")
        return _failed("requested", "Передал Obsidian запрос открытия. Если приложение не установлено, установи его и открой хранилище вручную один раз.")

    def open_vault(self, *, authorization_guard=None):
        try:
            configuration, root, _info = self._configuration()
            return self._open(configuration, root, root, authorization_guard)
        except (OSError, ValueError, TypeError):
            return _failed("unavailable", "Не удалось открыть Obsidian. Проверь приложение и выбранную папку хранилища.")

    def open_note(self, note_id, *, authorization_guard=None):
        try:
            configuration, root, _info = self._configuration()
            if not isinstance(note_id, str) or not re.fullmatch(r"[a-f0-9]{24}", note_id):
                return _failed("missing", "Выбери заметку из списка Obsidian.")
            item = next((item for item in self._notes(configuration, root) if item["id"] == note_id), None)
            if item is None:
                return _failed("missing", "Заметка больше не найдена в выбранном хранилище.")
            self._read_note(root, item)  # Verify text/identity before handing it to the URI handler.
            return self._open(configuration, root, root / item["relative_path"], authorization_guard)
        except (OSError, ValueError, TypeError):
            return _failed("unavailable", "Не удалось безопасно открыть эту заметку в Obsidian.")

    def close(self):
        self._closed.set()
