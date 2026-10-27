"""Explicit installation of a pinned, personal-use-only Silero voice model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

from desktop.expressive_speech import (
    MODEL_FILES, MODEL_REVISION, MODEL_URLS, default_model_dir, verified_model,
)


def install_model(target_dir=None, progress_callback=None, *, personal_use=False) -> Path:
    if personal_use is not True:
        raise ValueError("Silero v5_6_ru requires explicit personal noncommercial use")
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
            progress_callback("Загружаю голос для личного некоммерческого использования…")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".silero-", suffix=".partial", dir=target)
        temporary = Path(temporary_name)
        try:
            digest, size = hashlib.sha256(), 0
            request = urllib.request.Request(MODEL_URLS[name], headers={"User-Agent": "JarvisDesktop/4.2 personal-voice-setup"})
            with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=30) as response:
                while block := response.read(512 * 1024):
                    size += len(block)
                    if size > expected_size:
                        raise ValueError("Personal voice download exceeds its pinned size")
                    output.write(block)
                    digest.update(block)
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise ValueError("Personal voice download does not match its pinned checksum")
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    if not verified_model(target):
        raise ValueError("Personal voice verification failed")
    (target / "manifest.json").write_text(json.dumps({
        "engine": "Silero v5_6_ru", "source_revision": MODEL_REVISION,
        "files": MODEL_FILES, "urls": MODEL_URLS, "license": "CC-BY-NC-SA-4.0",
        "personal_noncommercial_only": True, "commercial_distribution": False,
        "checksum_provenance": "Official TLS asset downloaded and pinned; no upstream checksum published",
        "sample_rate": 24000, "voices": ["aidar", "eugene"],
    }, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install the local, personal noncommercial Silero voice")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--personal-use", action="store_true", required=True,
                        help="Acknowledge the model's CC-BY-NC-SA-4.0 noncommercial restriction")
    arguments = parser.parse_args()
    print(install_model(arguments.destination, personal_use=arguments.personal_use))
