"""Optional Windows agent: no model, shell, or remote arbitrary execution."""
import ctypes
import json
import logging
import logging.handlers
import os
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv
from services.pc_agent import ReplayGuard, signed_status, verify_signed_command
from services.redis_backend import create_redis

logger = logging.getLogger("jarvis.agent")


class SingleInstance:
    def __init__(self, path):
        self.file = open(path, "a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another Jarvis agent is already running") from None

    def close(self):
        self.file.close()


def execute_command(cmd):
    if os.name != "nt":
        raise RuntimeError("PC actions require Windows")
    if cmd == "lock":
        if not ctypes.windll.user32.LockWorkStation():
            raise OSError("LockWorkStation failed")
    elif cmd == "hibernate":
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "shutdown.exe"
        subprocess.run([str(executable), "/h"],
                       shell=False, check=True, timeout=15, capture_output=True)
    elif cmd in {"shutdown", "restart"}:
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "shutdown.exe"
        subprocess.run([str(executable), "/s" if cmd == "shutdown" else "/r", "/t", "10"],
                       shell=False, check=True, timeout=15, capture_output=True)
    elif cmd != "status":
        raise ValueError("Unsupported PC command")


class Agent:
    def __init__(self, redis, user_id, secret, state_dir, execute=execute_command):
        self.redis, self.user_id, self.secret = redis, user_id, secret
        self.execute = execute
        self.guard = ReplayGuard(state_dir / "agent-replay.db")
        self.result_path = state_dir / "last-result.json"
        self.last = None
        if self.result_path.exists():
            try:
                self.last = json.loads(self.result_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass

    def publish(self):
        self.redis.set(f"jarvis:pc_status:{self.user_id}", signed_status({
            "user_id": self.user_id, "seen_at": int(time.time()), "state": "running",
            "version": "4", "last_result": self.last,
        }, self.secret), ex=86400)

    def record(self, command, nonce, state, error=None):
        self.last = {"command": command, "nonce": nonce, "state": state,
                     "at": int(time.time()), "error": error}
        temporary = self.result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.last), encoding="utf-8")
        temporary.replace(self.result_path)

    def tick(self):
        self.publish()
        payload = self.redis.getdel(f"jarvis:pc_command:{self.user_id}")
        if not payload:
            return
        command = verify_signed_command(payload, self.user_id, self.secret)
        if command is None or not self.guard.claim(payload):
            logger.warning("Rejected invalid or replayed command")
            return
        nonce = json.loads(payload)["nonce"]
        self.record(command, nonce, "accepted")
        # ACK before suspend/shutdown; never confuse accepted with completed.
        # If ACK cannot be published, do not execute or replay the command.
        self.publish()
        try:
            self.execute(command)
            self.record(command, nonce, "scheduled" if command in {"hibernate", "shutdown", "restart"} else "completed")
        except Exception as exc:
            self.record(command, nonce, "failed", type(exc).__name__)
        self.publish()


def start_agent():
    # Prefer a per-user agent env outside the repository. This keeps the
    # external Redis credential and HMAC secret out of git and makes Task
    # Scheduler startup independent from the interactive shell environment.
    state_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Jarvis"
    state_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv(state_dir / "agent.env", override=True)
    load_dotenv(override=False)
    user_id, secret = int(os.getenv("ADMIN_ID", "0")), os.getenv("PC_AGENT_SECRET", "")
    redis = create_redis(sync=True)
    if user_id <= 0 or not secret or redis is None:
        raise RuntimeError("Redis, ADMIN_ID and PC_AGENT_SECRET are required")
    if os.name != "nt":
        raise RuntimeError("The installed agent requires Windows")
    instance = SingleInstance(state_dir / "agent.lock")
    handler = logging.handlers.RotatingFileHandler(state_dir / "agent.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    agent = Agent(redis, user_id, secret, state_dir)
    backoff = 2
    try:
        while True:
            try:
                agent.tick()
                backoff = 2
            except Exception as exc:
                logger.warning("Agent transport failure error=%s", type(exc).__name__)
                backoff = min(backoff * 2, 60)
            time.sleep(backoff)
    finally:
        instance.close()
        handler.close()


if __name__ == "__main__":
    start_agent()
