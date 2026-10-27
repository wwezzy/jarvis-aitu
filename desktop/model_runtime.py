"""Best-effort startup of the setup-verified, local-only Ollama runtime.

The manifest is owned by the installer/launcher, never model or document data.
No preferences, OS settings, existing processes or network rules are changed.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess


def ensure_model_runtime(state_dir: Path, manifest: dict,
                         connector=socket.create_connection, spawn=subprocess.Popen) -> str:
    """Return disabled/already_listening/started/refused/unavailable; never raise.

    Reads only an existing preferences row. A missing/corrupt setting cannot
    enable startup. Child output is discarded: no prompts, environment values
    or unbounded background logs are written by this helper.
    """
    try:
        database = Path(state_dir).resolve() / "desktop.db"
        if not database.is_file():
            return "disabled"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            row = db.execute("SELECT value FROM preferences WHERE id=1").fetchone()
        if not row or not isinstance(row[0], str) or len(row[0]) > 65536:
            return "disabled"
        preferences = json.loads(row[0])
        if not isinstance(preferences, dict) or preferences.get("local_chat_enabled") is not True:
            return "disabled"
        try:
            connection = connector(("127.0.0.1", 11434), timeout=0.35)
            try:
                return "already_listening"
            finally:
                connection.close()
        except OSError:
            pass
        if not isinstance(manifest, dict):
            return "refused"
        raw, expected = manifest.get("ollamaExe"), manifest.get("ollamaSha256")
        if (not isinstance(raw, str) or not raw or "\x00" in raw
                or raw.startswith(("\\\\", "//")) or "://" in raw
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)):
            return "refused"
        executable = Path(raw)
        if not executable.is_absolute() or executable.name.casefold() != "ollama.exe":
            return "refused"
        executable = executable.resolve(strict=True)
        if (str(executable).startswith(("\\\\", "//")) or executable.name.casefold() != "ollama.exe"
                or not executable.is_file() or not 0 < executable.stat().st_size <= 256 * 1024 * 1024):
            return "refused"
        with executable.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != expected.lower():
            return "refused"
        environment = os.environ.copy()
        environment.update(OLLAMA_HOST="127.0.0.1:11434", OLLAMA_NO_CLOUD="1", OLLAMA_DEBUG="0",
                           OLLAMA_MAX_LOADED_MODELS="1", OLLAMA_NUM_PARALLEL="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        spawn([str(executable), "serve"], shell=False, stdin=subprocess.DEVNULL,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
              cwd=str(executable.parent), creationflags=flags)
        return "started"
    except Exception:
        # Optional startup must not prevent deterministic desktop functionality.
        return "unavailable"
