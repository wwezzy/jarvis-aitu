"""Explicit, checksum-pinned public RU/EN speech model installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

from desktop.multilingual_stt import MODEL_FILES, MODEL_REVISION, MODEL_URLS, default_model_dir, verified_model


def install_model(target_dir=None, progress_callback=None) -> Path:
    target = Path(target_dir) if target_dir is not None else default_model_dir()
    target.mkdir(parents=True, exist_ok=True)
    target = target.resolve()
    for name, (expected_size, expected_hash) in MODEL_FILES.items():
        destination = target / name
        if destination.is_file() and destination.stat().st_size == expected_size:
            with destination.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() == expected_hash:
                    continue
        if progress_callback:
            progress_callback("Загружаю локальное распознавание русского и английского…")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".stt-", suffix=".partial", dir=target)
        temporary = Path(temporary_name)
        try:
            digest, size = hashlib.sha256(), 0
            request = urllib.request.Request(MODEL_URLS[name], headers={"User-Agent": "JarvisDesktop/4.2 stt-setup"})
            with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=30) as response:
                while block := response.read(512 * 1024):
                    size += len(block)
                    if size > expected_size:
                        raise ValueError("Speech model exceeds its pinned size")
                    output.write(block)
                    digest.update(block)
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise ValueError("Speech model does not match its pinned checksum")
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    if not verified_model(target):
        raise ValueError("Speech model verification failed")
    (target / "manifest.json").write_text(json.dumps({
        "engine": "CTranslate2 CPU int8, NumPy frontend, Silero VAD",
        "model": "Systran/faster-whisper-small", "model_revision": MODEL_REVISION,
        "files": MODEL_FILES, "urls": MODEL_URLS, "license": "MIT; see included licenses",
        "languages": ["ru", "en"], "maximum_audio_seconds": 12,
        "network_during_transcription": False, "pyav_required": False,
    }, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install the checksum-pinned local RU/EN speech model")
    parser.add_argument("--destination", type=Path)
    arguments = parser.parse_args()
    print(install_model(arguments.destination))
