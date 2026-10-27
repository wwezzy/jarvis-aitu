"""Load only explicit phone settings from the installed private profile.

This is a literal key/value reader, never a shell or a general dotenv loader.
External process settings win. No value, credential or file content is logged.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets


PHONE_KEYS = frozenset({
    "JARVIS_PHONE_ENABLED", "JARVIS_PHONE_TOKEN", "JARVIS_PHONE_ORIGIN",
    "DESKTOP_TELEGRAM_ENABLED", "DESKTOP_TELEGRAM_BOT_TOKEN", "DESKTOP_TELEGRAM_CHAT_ID",
    "JARVIS_PHONE_CALLS_ENABLED", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM", "TWILIO_TO", "TWILIO_PERSONAL_AUDIO_URL",
})


def create_phone_environment(state_dir):
    """Create an off-by-default private template once; never replace a token."""
    try:
        root = Path(state_dir)
        if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
            return "refused"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "phone.env"
        with path.open("x", encoding="utf-8") as output:
            output.write("# Private profile, outside Git. All external channels start disabled.\n"
                         "JARVIS_PHONE_ENABLED=0\nJARVIS_PHONE_TOKEN=" + secrets.token_urlsafe(32)
                         + "\nJARVIS_PHONE_ORIGIN=\nDESKTOP_TELEGRAM_ENABLED=0\n"
                         "JARVIS_PHONE_CALLS_ENABLED=0\n")
        return "created"
    except FileExistsError:
        return "preserved"
    except (OSError, ValueError):
        return "refused"


def load_phone_environment(state_dir, *, environ=None):
    """Return absent/loaded/refused without exposing secrets or other env files."""
    target = os.environ if environ is None else environ
    try:
        root = Path(state_dir)
        if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
            return "refused"
        root = root.resolve()
        path = root / "phone.env"
        if not path.exists():
            return "absent"
        if (not path.is_file() or not path.resolve().is_relative_to(root)
                or path.stat().st_size > 16384):
            return "refused"
        parsed = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or key not in PHONE_KEYS or key in parsed or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                return "refused"
            if "\x00" in value or len(value) > 4096:
                return "refused"
            if value[:1] in {"'", '"'}:
                if len(value) < 2 or value[-1] != value[0]:
                    return "refused"
                value = value[1:-1]
            parsed[key] = value
        for key, value in parsed.items():
            target.setdefault(key, value)
        return "loaded"
    except (OSError, UnicodeError, ValueError):
        return "refused"
