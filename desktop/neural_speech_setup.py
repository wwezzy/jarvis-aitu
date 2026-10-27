"""Explicit public-model installer; never called during normal speech startup."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

from desktop.neural_speech import MODEL_BASE_URL, MODEL_FILES, MODEL_REVISION, default_model_dir, verified_model


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
            progress_callback("Загружаю публичную модель голоса…")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".piper-", suffix=".partial", dir=target)
        temporary = Path(temporary_name)
        try:
            digest, size = hashlib.sha256(), 0
            request = urllib.request.Request(MODEL_BASE_URL + name, headers={"User-Agent": "JarvisDesktop/4.2 voice-setup"})
            with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=30) as response:
                while block := response.read(1024 * 512):
                    size += len(block)
                    if size > expected_size:
                        raise ValueError("Public voice download exceeds its pinned size")
                    output.write(block)
                    digest.update(block)
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise ValueError("Public voice download does not match its pinned checksum")
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    (target / "manifest.json").write_text(json.dumps({"revision": MODEL_REVISION,
        "source": MODEL_BASE_URL, "files": MODEL_FILES, "engine": "Piper 1.8.0, GPL-3.0",
        "voice_dataset": "Dmitri, CC0; see MODEL_CARD", "sample_rate": 22050}, indent=2), encoding="utf-8")
    if not verified_model(target):
        raise ValueError("Public voice verification failed")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install the checksum-pinned offline Russian Piper voice")
    parser.add_argument("--destination", type=Path)
    arguments = parser.parse_args()
    print(install_model(arguments.destination))
