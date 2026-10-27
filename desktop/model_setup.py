"""Explicit, bounded installation of the official offline Russian Vosk model.

The SHA-256 pins the archive retrieved over verified HTTPS from the model
publisher on 2026-10-27.  It is a reproducibility pin, not a publisher signature.
No audio or personal data is uploaded.  Importing this module downloads nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

MODEL_NAME = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_SHA256 = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"
MAX_DOWNLOAD = 96 * 1024 * 1024
MAX_UNPACKED = 256 * 1024 * 1024
REQUIRED = ("am/final.mdl", "conf/model.conf", "conf/mfcc.conf", "graph/Gr.fst", "graph/HCLr.fst")


def model_ready(path: Path) -> bool:
    path = Path(path)
    try:
        return path.is_dir() and all((path / name).is_file() and (path / name).stat().st_size > 0
                                     for name in REQUIRED)
    except OSError:
        return False


def default_model_dir() -> Path:
    user_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share")))
    user_path = user_path / "JarvisDesktop" / "models" / MODEL_NAME
    if model_ready(user_path):
        return user_path.resolve()
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    bundled = bundle_root / "models" / MODEL_NAME
    return bundled if model_ready(bundled) else user_path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _progress(callback, message):
    if callback:
        callback(message)


def _download(destination: Path, callback):
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "JarvisDesktop/4.2 model-installer"})
    with opener.open(request, timeout=30) as response, destination.open("xb") as output:
        if response.geturl() != MODEL_URL:
            raise ValueError("Unexpected model download origin")
        header = response.headers.get("Content-Length")
        expected = int(header) if header else None
        if expected is not None and not 0 < expected <= MAX_DOWNLOAD:
            raise ValueError("Model archive is too large")
        total, last_percent = 0, -1
        while True:
            block = response.read(1024 * 512)
            if not block:
                break
            total += len(block)
            if total > MAX_DOWNLOAD:
                raise ValueError("Model archive is too large")
            output.write(block)
            percent = min(99, int(total * 100 / expected)) if expected else None
            if percent != last_percent:
                _progress(callback, f"Загрузка модели: {percent}%" if percent is not None else "Загружаю модель…")
                last_percent = percent
        if total == 0 or (expected is not None and total != expected):
            raise ValueError("Incomplete model archive")


def _extract(archive: Path, destination: Path):
    root = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > 1000 or sum(x.file_size for x in members) > MAX_UNPACKED:
            raise ValueError("Model archive exceeds extraction limits")
        seen = set()
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            if (not name or "\\" in name or ":" in name or path.is_absolute()
                    or ".." in path.parts or path.parts[0] != MODEL_NAME
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError("Unsafe model archive member")
            if any(part.endswith((".", " ")) or part.upper().split(".")[0]
                   in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))} for part in path.parts):
                raise ValueError("Unsafe model archive filename")
            candidate = root.joinpath(*path.parts).resolve()
            if root not in candidate.parents:
                raise ValueError("Model archive escapes destination")
            normalized = str(candidate).casefold()
            if normalized in seen:
                raise ValueError("Duplicate model archive member")
            seen.add(normalized)
            if member.is_dir():
                candidate.mkdir(parents=True, exist_ok=True)
                continue
            if member.file_size < 0 or member.file_size > MAX_UNPACKED:
                raise ValueError("Invalid model archive member size")
            candidate.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with source.open(member) as reader, candidate.open("xb") as writer:
                while block := reader.read(1024 * 512):
                    written += len(block)
                    if written > member.file_size:
                        raise ValueError("Invalid expanded model size")
                    writer.write(block)
            if written != member.file_size:
                raise ValueError("Incomplete model file")


def install_model(target_dir: Path, progress_callback=None, *, archive_path: Path | None = None) -> Path:
    """Download (or verify an explicit cache archive) and atomically install.

    The caller must invoke this after an explicit download action.  Existing
    valid models are reused.  Existing invalid folders are never deleted.
    ``progress_callback`` receives a localized string; GUI clients run this
    function in a worker and marshal progress back to the UI thread.
    """
    target = Path(target_dir).expanduser().resolve()
    if model_ready(target):
        _progress(progress_callback, "Локальная модель готова")
        return target
    if target.exists():
        raise FileExistsError("Model destination already exists but is incomplete")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Packaged Windows apps can redirect LOCALAPPDATA only when the folder is
    # created. Resolve again after creation instead of comparing a stale alias.
    target = target.parent.resolve() / target.name
    with tempfile.TemporaryDirectory(prefix=".jarvis-model-", dir=target.parent) as staging:
        stage = Path(staging).resolve()
        if not stage.parent.samefile(target.parent):
            raise ValueError("Model staging directory escapes the requested parent")
        archive = stage / "model.zip"
        if archive_path is None:
            _progress(progress_callback, "Загружаю модель речи, около 45 МБ…")
            _download(archive, progress_callback)
        else:
            source = Path(archive_path).resolve()
            if not source.is_file() or not 0 < source.stat().st_size <= MAX_DOWNLOAD:
                raise ValueError("Invalid cached model archive")
            shutil.copyfile(source, archive)
        _progress(progress_callback, "Проверяю контрольную сумму…")
        digest = hashlib.sha256()
        with archive.open("rb") as reader:
            while block := reader.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != MODEL_SHA256:
            raise ValueError("Model archive checksum mismatch")
        _progress(progress_callback, "Распаковываю локальную модель…")
        _extract(archive, stage)
        extracted = (stage / MODEL_NAME).resolve()
        if extracted.parent != stage or not model_ready(extracted):
            raise ValueError("Model archive is incomplete")
        # Move only this verified child into the explicitly requested folder.
        # No existing user folder or model is overwritten/removed.
        extracted.rename(target)
    _progress(progress_callback, "Локальная модель готова")
    return target


def main():
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Install the official offline Vosk Russian model")
    parser.add_argument("--destination", type=Path, default=default_model_dir())
    parser.add_argument("--archive", type=Path, help="Use an explicitly provided, checksum-verified cache ZIP")
    args = parser.parse_args()
    install_model(args.destination, print, archive_path=args.archive)


if __name__ == "__main__":
    main()
