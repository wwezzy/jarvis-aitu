"""Explicit Gemini credential source and revocable owner preferences.

No directory search, automatic dotenv loading or billing enrollment occurs.
The free-project attestation records an operator's verified existing project;
it cannot prevent a later billing change on Google's side.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from desktop.openai_mode import _regular, _unique_object

_KEY = r"(?:[A-Za-z0-9_-]{20,200}|AQ\.[A-Za-z0-9_-]{20,197})"


def _atomic(path, content):
    if path.exists():
        _regular(path, 16384)
    temporary = path.with_name(path.name + ".setup.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if path.exists() or path.is_symlink():
            _regular(path, 16384)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_existing_key(state_dir, key, project_id, *, free_project_verified=False, now=None):
    """Provision only an existing key after verifying its project's Free tier.

    Caller must neither create a new credential nor turn billing on. Secret
    material is written only to the explicit private profile, never returned.
    """
    if free_project_verified is not True:
        raise ValueError("Verify the existing project's Free tier first")
    if not isinstance(key, str) or not re.fullmatch(_KEY, key):
        raise ValueError("Invalid Gemini credential")
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project_id):
        raise ValueError("Invalid Google project")
    directory = Path(state_dir)
    raw = str(directory)
    if (not directory.is_absolute() or len(raw) > 2048 or "\x00" in raw or ".." in directory.parts
            or raw.startswith(("\\\\", "//")) or "://" in raw):
        raise ValueError("Existing private profile required")
    for parent in (directory, *directory.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked private profile refused")
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.utcoffset() != timedelta(0):
        raise ValueError("UTC verification time required")
    env_file = directory / "gemini.env"
    # Reject every known bad target before replacing even the first secret.
    # The marker is installed last; interrupted setup fails its key binding.
    for name in ("gemini.env", "gemini-key-source.json", "gemini-free-project.json"):
        target = directory / name
        if target.exists() or target.is_symlink():
            _regular(target, 16384)
        temporary = target.with_name(target.name + ".setup.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise ValueError("Unfinished Gemini setup requires inspection")
    _atomic(env_file, "GEMINI_API_KEY=" + key + "\n")
    _atomic(directory / "gemini-key-source.json", json.dumps(
        {"version": 1, "env_name": "GEMINI_API_KEY", "env_file": str(env_file)}, indent=2))
    _atomic(directory / "gemini-free-project.json", json.dumps({
        "version": 1, "project_id": project_id, "key_sha256": hashlib.sha256(key.encode("ascii")).hexdigest(),
        "billing_enabled": False, "verified_at": moment.isoformat(),
        "expires_at": (moment + timedelta(days=30)).isoformat()}, indent=2))
    return {"configured": True, "project_id": project_id, "billing_enabled": False}


class GeminiEnvironment(Mapping):
    def __init__(self, state_dir, preferences, *, environ=None):
        self.state_dir = Path(state_dir)
        self.preferences = preferences
        self.environ = os.environ if environ is None else environ

    def _key(self):
        raw = self.environ.get("GEMINI_API_KEY")
        if raw:
            return raw
        try:
            manifest = self.state_dir / "gemini-key-source.json"
            _regular(manifest, 4096)
            value = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            if (not isinstance(value, dict) or set(value) != {"version", "env_name", "env_file"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or value["env_name"] != "GEMINI_API_KEY" or not isinstance(value["env_file"], str)):
                return None
            path = Path(value["env_file"])
            _regular(path, 8192)
            values = []
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if not re.match(r"\s*GEMINI_API_KEY\s*=", line):
                    continue
                match = re.fullmatch(r"\s*GEMINI_API_KEY\s*=\s*(" + _KEY + r")\s*", line)
                if match is None:
                    return None
                values.append(match[1])
            return values[0] if len(values) == 1 else None
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
            return None

    def __getitem__(self, key):
        preferences = self.preferences()
        enabled = preferences.get("gemini_enabled") is True
        if key == "GEMINI_API_KEY":
            value = self._key() if enabled else None
        elif key == "JARVIS_GEMINI_ENABLED":
            value = "1" if enabled else "0"
        elif key == "JARVIS_GEMINI_MODEL":
            value = preferences.get("gemini_model", "gemini-3.5-flash-lite")
        elif key == "JARVIS_GEMINI_HISTORY_ENABLED":
            value = "1" if enabled and preferences.get("local_chat_history") is True else "0"
        elif key == "JARVIS_GEMINI_PLAN_CONTEXT_ENABLED":
            value = "1" if enabled and preferences.get("local_chat_plan_context") is True else "0"
        elif key == "JARVIS_GEMINI_PERSONALITY_CONTEXT_ENABLED":
            value = "1" if enabled and preferences.get("personal_context_enabled") is True else "0"
        elif key == "JARVIS_GEMINI_DOCUMENT_CONTEXT_ENABLED":
            value = "1" if enabled else "0"  # Only explicitly supplied attachments exist at the caller.
        elif key == "JARVIS_GEMINI_SPEECH_ENABLED":
            value = "1" if enabled and preferences.get("voice_engine") == "gemini" else "0"
        elif key == "OPENAI_API_KEY":
            value = None
        elif key in {"JARVIS_CLOUD_CHAT_ENABLED", "JARVIS_CLOUD_STT_ENABLED", "JARVIS_CLOUD_SPEECH_ENABLED"}:
            value = "0"
        else:
            value = self.environ.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self):
        keys = set(self.environ) | {"GEMINI_API_KEY", "JARVIS_GEMINI_ENABLED", "JARVIS_GEMINI_MODEL",
            "JARVIS_GEMINI_HISTORY_ENABLED", "JARVIS_GEMINI_PLAN_CONTEXT_ENABLED",
            "JARVIS_GEMINI_DOCUMENT_CONTEXT_ENABLED", "JARVIS_GEMINI_PERSONALITY_CONTEXT_ENABLED",
            "JARVIS_GEMINI_SPEECH_ENABLED", "JARVIS_CLOUD_CHAT_ENABLED",
            "JARVIS_CLOUD_STT_ENABLED", "JARVIS_CLOUD_SPEECH_ENABLED"}
        return iter(sorted(key for key in keys if self.get(key) is not None))

    def __len__(self):
        return sum(1 for _ in self)
