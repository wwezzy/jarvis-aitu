"""Literal private phone settings; no expansion, shell or secret logging.

Creation adds missing disabled defaults. Explicit trusted setup alone changes
the phone/control switches and private Tailscale origin. Existing tokens and
channel settings are preserved. Loading never overrides process settings.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from urllib.parse import urlsplit


PHONE_KEYS = frozenset({
    "JARVIS_PHONE_ENABLED", "JARVIS_PHONE_TOKEN", "JARVIS_PHONE_ORIGIN",
    "JARVIS_REMOTE_ENABLED", "JARVIS_REMOTE_TOKEN",
    "DESKTOP_TELEGRAM_ENABLED", "DESKTOP_TELEGRAM_BOT_TOKEN", "DESKTOP_TELEGRAM_CHAT_ID",
    "JARVIS_PHONE_CALLS_ENABLED", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM", "TWILIO_TO", "TWILIO_PERSONAL_AUDIO_URL",
})
MAX_ENV_BYTES = 16384
LOCK_TIMEOUT = 3.0
_FLAGS = {"JARVIS_PHONE_ENABLED", "JARVIS_REMOTE_ENABLED", "DESKTOP_TELEGRAM_ENABLED", "JARVIS_PHONE_CALLS_ENABLED"}
_HEADER = "# Private profile, outside Git. External channels require explicit configuration.\n"


def _strong(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value) is not None and len(set(value)) >= 8


def _origin(value, *, tailscale=False):
    if not isinstance(value, str) or not value or len(value) > 255 or not value.isascii():
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        return bool(parsed.scheme == "https" and parsed.netloc == host and not parsed.path
                    and parsed.username is None and parsed.password is None and parsed.port is None
                    and not parsed.query and not parsed.fragment and not re.search(r"[\s\\%]", value)
                    and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host)
                    and (not tailscale or host.endswith(".ts.net") and host != "ts.net"))
    except ValueError:
        return False


def _root(state_dir, *, create=False):
    root = Path(state_dir)
    raw = str(root)
    if (not root.is_absolute() or ".." in root.parts or len(raw) > 2048
            or raw.startswith(("\\\\", "//")) or "://" in raw or "\x00" in raw):
        raise ValueError("Profile path refused")
    for directory in (root, *root.parents):
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise ValueError("Linked profile refused")
        if directory.exists() and not directory.is_dir():
            raise ValueError("Profile is not a directory")
    if root.resolve() != root:
        raise ValueError("Redirected profile refused")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ValueError("Linked or nonregular private file refused")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read(path):
    try:
        before = _identity(path.lstat())
    except FileNotFoundError:
        return "", {}, None
    if before[2] > MAX_ENV_BYTES:
        raise ValueError("Private settings too large")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as source:
        if _identity(os.fstat(source.fileno())) != before:
            raise ValueError("Private settings changed")
        raw = source.read(MAX_ENV_BYTES + 1)
        after = _identity(os.fstat(source.fileno()))
    if len(raw) > MAX_ENV_BYTES or before != after or after != _identity(path.lstat()):
        raise ValueError("Private settings changed")
    text, parsed = raw.decode("utf-8-sig"), {}
    if (any(character in text for character in ("\x00", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"))
            or re.search(r"\r(?!\n)", text)):
        raise ValueError("Malformed private line endings")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in PHONE_KEYS or key in parsed:
            raise ValueError("Unknown, duplicate or malformed setting")
        if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Malformed private value")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Unclosed private value")
            value = value[1:-1]
        parsed[key] = value
    _validate(parsed)
    return text, parsed, before


def _validate(values):
    if any(values[key] not in {"0", "1"} for key in _FLAGS & values.keys()):
        raise ValueError("Invalid feature switch")
    for key in ("JARVIS_PHONE_TOKEN", "JARVIS_REMOTE_TOKEN"):
        if key in values and not _strong(values[key]):
            raise ValueError("Invalid private token")
    if values.get("JARVIS_PHONE_TOKEN") and values.get("JARVIS_REMOTE_TOKEN") == values["JARVIS_PHONE_TOKEN"]:
        raise ValueError("Separate private tokens required")
    if values.get("JARVIS_PHONE_ORIGIN") and not _origin(values["JARVIS_PHONE_ORIGIN"]):
        raise ValueError("Invalid phone origin")


def _allowed(guard):
    if guard is None:
        return True
    try:
        return callable(guard) and guard() is True
    except Exception:
        return False


@contextmanager
def _writer_lock(root):
    path = root / "phone.env.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    locked = False
    try:
        info = os.fstat(descriptor)
        _identity(info)
        if (info.st_dev, info.st_ino) != _identity(path.lstat())[:2]:
            raise ValueError("Private lock redirected")
        if info.st_size == 0:
            os.write(descriptor, b"0")
        elif info.st_size != 1:
            raise ValueError("Invalid private lock")
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Private settings busy") from None
                time.sleep(0.02)
        _root(root)
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _defaults(existing):
    values = dict(existing)
    for key in ("JARVIS_PHONE_ENABLED", "JARVIS_REMOTE_ENABLED", "DESKTOP_TELEGRAM_ENABLED", "JARVIS_PHONE_CALLS_ENABLED"):
        values.setdefault(key, "0")
    values.setdefault("JARVIS_PHONE_ORIGIN", "")
    for key, other in (("JARVIS_PHONE_TOKEN", "JARVIS_REMOTE_TOKEN"), ("JARVIS_REMOTE_TOKEN", "JARVIS_PHONE_TOKEN")):
        if key not in values:
            for _ in range(8):
                token = secrets.token_urlsafe(32)
                if _strong(token) and token != values.get(other):
                    values[key] = token
                    break
            else:
                raise ValueError("Private token generation failed")
    _validate(values)
    return values


def _updated_text(text, old, new):
    lines = (text if text else _HEADER).splitlines(keepends=True)
    for index, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if key in old and new[key] != old[key]:
            lines[index] = f"{key}={new[key]}\n"
    output = "".join(lines)
    if output and not output.endswith(("\r", "\n")):
        output += "\n"
    output += "".join(f"{key}={value}\n" for key, value in new.items() if key not in old)
    return output.encode("utf-8")


def _write(root, raw, before, guard):
    if len(raw) > MAX_ENV_BYTES:
        raise ValueError("Private settings too large")
    descriptor, temporary = tempfile.mkstemp(prefix=".phone.env.", suffix=".tmp", dir=root)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        _root(root)
        path = root / "phone.env"
        try:
            current = _identity(path.lstat())
        except FileNotFoundError:
            current = None
        if current != before or not _allowed(guard):
            raise ValueError("Private settings changed or authorization revoked")
        os.replace(temporary, path)
        # Windows has no directory-fsync os API. Other platforms persist both
        # contents above and the atomic directory rename below.
        if os.name != "nt":
            directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def create_phone_environment(state_dir):
    """Return created/migrated/preserved/refused; add missing defaults only.

    No existing token is rotated; missing channels are disabled. Malformed files
    are preserved intact for explicit local correction.
    """
    try:
        root = _root(state_dir, create=True)
        with _writer_lock(root):
            text, old, before = _read(root / "phone.env")
            new = _defaults(old)
            if new == old:
                return "preserved"
            _write(root, _updated_text(text, old, new), before, None)
            return "created" if before is None else "migrated"
    except (OSError, UnicodeError, ValueError, TypeError):
        return "refused"


def update_phone_environment(state_dir, *, origin, phone_enabled=True, remote_enabled=False, authorization_guard=None):
    """Explicit setup: persist flags and exact https://*.ts.net, file-only.

    Return updated/preserved/refused without credentials. After durable success
    the caller may refresh those three process settings; the regular loader
    adds missing values while process tokens still win. The owner guard must
    return exactly True, also immediately before replacement under the lock.
    No network or service is used.
    """
    if (type(phone_enabled) is not bool or type(remote_enabled) is not bool
            or remote_enabled and not phone_enabled or not _origin(origin, tailscale=True)
            or not _allowed(authorization_guard)):
        return "refused"
    try:
        root = _root(state_dir, create=True)
        with _writer_lock(root):
            text, old, before = _read(root / "phone.env")
            new = _defaults(old)
            new.update(JARVIS_PHONE_ENABLED=str(int(phone_enabled)), JARVIS_REMOTE_ENABLED=str(int(remote_enabled)),
                       JARVIS_PHONE_ORIGIN=origin)
            if new == old:
                return "preserved" if _allowed(authorization_guard) else "refused"
            _write(root, _updated_text(text, old, new), before, authorization_guard)
            return "updated"
    except (OSError, UnicodeError, ValueError, TypeError):
        return "refused"


def load_phone_environment(state_dir, *, environ=None):
    """Return absent/loaded/refused; validate fully before env-wins loading."""
    target = os.environ if environ is None else environ
    try:
        root = _root(state_dir)
        _text, parsed, before = _read(root / "phone.env")
        if before is None:
            return "absent"
        for key, value in parsed.items():
            target.setdefault(key, value)
        return "loaded"
    except (OSError, UnicodeError, ValueError, TypeError):
        return "refused"
