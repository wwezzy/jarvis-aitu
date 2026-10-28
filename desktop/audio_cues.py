"""Pinned personal WAV cues, read-only and independent of voice/model engines.

The user-supplied audio and manifest remain in the owner's private data folder,
never in the application bundle or Git. This module neither plays nor records
audio. A cue name is a finite identifier, never a filename or inferred command.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
from dataclasses import dataclass
import wave


MAX_MANIFEST_BYTES = 16 * 1024
MAX_CUE_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 20
ALLOWED_CUES = frozenset({"welcome_home"})


@dataclass(frozen=True)
class Cue:
    pcm: bytes
    sample_rate: int
    channels: int


def default_config_path():
    return Path.home() / "JarvisData" / "VoiceLab" / "audio-cues.json"


def _local(path):
    raw = os.fspath(path)
    if (not isinstance(raw, str) or len(raw) > 2048 or any(ord(character) < 32 for character in raw)
            or "://" in raw or raw.startswith(("\\\\", "//"))):
        raise ValueError("Invalid local cue path")
    location = Path(raw)
    if (not location.is_absolute() or ".." in location.parts
            or ":" in raw[len(location.drive):] or location.resolve(strict=True) != location
            or location.is_symlink() or getattr(location, "is_junction", lambda: False)()):
        raise ValueError("Cue links and redirects are not allowed")
    return location


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _read_bounded(path, limit):
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if not 0 < before.st_size <= limit or not path.is_file():
            raise ValueError("Cue file size is outside the supported range")
        content = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if (len(content) != before.st_size or len(content) > limit
            or _identity(before) != _identity(after) or _identity(after) != _identity(path.stat())):
        raise ValueError("Cue file changed during validation")
    _local(path)  # Recheck links/parents after the bounded read as well.
    return content


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated cue configuration key")
        result[key] = value
    return result


class CueLibrary:
    """Read exact version-one private manifests, returning None on absence/error.

    Manifest: {"version":1,"cues":{"welcome_home":{
        "path":"welcome-home.wav","sha256":"<64 hexadecimal characters>"}}}
    A relative/absolute path must stay under the canonical manifest directory.
    Available audio is PCM16 little-endian mono/stereo at 16–48 kHz, at most
    20 seconds and 8 MiB including its WAV header. No file is created or changed.
    """

    def __init__(self, config_path=None):
        self.config_path = Path(config_path) if config_path is not None else default_config_path()

    def available(self, name):
        return self.read(name) is not None

    def read(self, name):
        if not isinstance(name, str) or name not in ALLOWED_CUES:
            return None
        try:
            config = _local(self.config_path)
            parent = _local(config.parent)
            data = json.loads(_read_bounded(config, MAX_MANIFEST_BYTES).decode("utf-8"), object_pairs_hook=_unique_object)
            if (not isinstance(data, dict) or set(data) != {"version", "cues"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["cues"], dict) or not set(data["cues"]) <= ALLOWED_CUES):
                return None
            entry = data["cues"].get(name)
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                return None
            raw, digest = entry["path"], entry["sha256"]
            if (not isinstance(raw, str) or not raw or len(raw) > 2048
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
                return None
            path = Path(raw)
            if (".." in path.parts or path.drive and not path.is_absolute()
                    or ":" in raw[len(path.drive):] or raw.startswith(("\\\\", "//"))):
                return None
            cue_path = _local(path if path.is_absolute() else parent / path)
            if parent not in cue_path.parents or cue_path.suffix.casefold() != ".wav":
                return None
            content = _read_bounded(cue_path, MAX_CUE_BYTES)
            if hashlib.sha256(content).hexdigest() != digest.lower():
                return None
            # Reject bytes appended outside the RIFF container; WAV metadata
            # inside a valid pinned file remains inert and is never evaluated.
            if (len(content) < 44 or content[:4] != b"RIFF" or content[8:12] != b"WAVE"
                    or int.from_bytes(content[4:8], "little") + 8 != len(content)):
                return None
            with wave.open(io.BytesIO(content), "rb") as audio:
                rate, channels, frames = audio.getframerate(), audio.getnchannels(), audio.getnframes()
                if (audio.getcomptype() != "NONE" or audio.getsampwidth() != 2
                        or channels not in {1, 2} or not 16000 <= rate <= 48000
                        or not 1 <= frames <= rate * MAX_SECONDS):
                    return None
                expected = frames * channels * 2
                if expected > MAX_CUE_BYTES:
                    return None
                pcm = audio.readframes(frames + 1)
                if len(pcm) != expected:
                    return None
            return Cue(pcm, rate, channels)
        except (OSError, ValueError, TypeError, OverflowError, EOFError, wave.Error, RecursionError):
            return None
