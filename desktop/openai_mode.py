"""Explicit desktop OpenAI mode; no credential discovery or secret persistence.

The optional private credential-source manifest is written only after the
owner confirms the exact env-file destination in the secure key setup flow.
No repository, home-directory or Codex credential search is performed.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import stat


def _regular(path, limit):
    raw = str(path)
    if (not path.is_absolute() or len(raw) > 2048 or "\x00" in raw or ".." in path.parts
            or raw.startswith(("\\\\", "//")) or "://" in raw):
        raise ValueError("Absolute private configuration required")
    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked configuration refused")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
        raise ValueError("Invalid private configuration")
    return info


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate credential-source field")
        result[key] = value
    return result


class OpenAIEnvironment(Mapping):
    """A live, revocable view of process env plus an explicitly chosen key file.

    Selecting the OpenAI voice mode enables chat, post-wake transcription and
    speech together. Local wake-word audio, notes and screen contents remain
    outside this view; cloud context still needs its own explicit switches.
    """
    def __init__(self, state_dir, preferences, *, environ=None):
        self.state_dir = Path(state_dir)
        self.preferences = preferences
        self.environ = os.environ if environ is None else environ
        self._signature, self._key = None, None

    def _file_key(self):
        try:
            manifest = self.state_dir / "openai-key-source.json"
            _regular(manifest, 4096)
            config = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            if (not isinstance(config, dict) or set(config) != {"version", "env_file", "env_name"}
                    or type(config["version"]) is not int or config["version"] != 1 or config["env_name"] != "OPENAI_API_KEY"
                    or not isinstance(config["env_file"], str)):
                raise ValueError("Invalid credential source")
            path = Path(config["env_file"])
            info = _regular(path, 8192)
            signature = (str(path), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
            if signature != self._signature:
                # Deliberately no interpolation, shell evaluation or dotenv
                # expansion. Only one exact, bounded env assignment is read.
                lines = path.read_text(encoding="utf-8-sig").splitlines()
                values = []
                for line in lines:
                    if not re.match(r"\s*(?:export\s+)?OPENAI_API_KEY\s*=", line):
                        continue
                    match = re.fullmatch(r"\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*(['\"]?)(sk-[A-Za-z0-9_-]{20,500})\1\s*", line)
                    if match is None:
                        values = []
                        break
                    values.append(match[2])
                self._key = values[0] if len(values) == 1 else None
                self._signature = signature
            return self._key
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            self._signature = self._key = None
            return None

    def __getitem__(self, key):
        cloud_flags = {"JARVIS_CLOUD_STT_ENABLED", "JARVIS_CLOUD_CHAT_ENABLED", "JARVIS_CLOUD_SPEECH_ENABLED"}
        preferences = self.preferences() if key == "OPENAI_API_KEY" or key in cloud_flags else {}
        if preferences.get("free_mode") is True:
            # Explicit local operation overrides inherited process flags and
            # never opens the owner's credential source, even during probing.
            self._signature = self._key = None
            if key == "OPENAI_API_KEY":
                raise KeyError(key)
            return "0"
        if key == "OPENAI_API_KEY":
            value = self.environ.get(key) or self._file_key()
        elif key in cloud_flags:
            value = "1" if preferences.get("voice_engine") == "openai" else self.environ.get(key)
        else:
            value = self.environ.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self):
        keys = set(self.environ) | {"OPENAI_API_KEY", "JARVIS_CLOUD_STT_ENABLED",
                                   "JARVIS_CLOUD_CHAT_ENABLED", "JARVIS_CLOUD_SPEECH_ENABLED"}
        present = []
        for key in sorted(keys):
            try:
                self[key]
            except KeyError:
                continue
            present.append(key)
        return iter(present)

    def __len__(self):
        return len(set(iter(self)))
