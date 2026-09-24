import ctypes
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from upstash_redis import Redis

from services.pc_agent import ReplayGuard, consume_command

load_dotenv()

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
PC_AGENT_SECRET = os.getenv("PC_AGENT_SECRET")
COMMAND_KEY = f"jarvis:pc_command:{ADMIN_ID}"


def execute_command(cmd: str) -> None:
    print(f"Executing system directive: {cmd}")
    if cmd == "sleep":
        ctypes.windll.PowrProf.SetSuspendState(0, 1, 0)
    elif cmd == "lock":
        ctypes.windll.user32.LockWorkStation()
    elif cmd == "shutdown":
        os.system("shutdown /s /t 0")
    elif cmd == "restart":
        os.system("shutdown /r /t 0")


def start_agent() -> None:
    if not UPSTASH_URL or not UPSTASH_TOKEN or not ADMIN_ID or not PC_AGENT_SECRET:
        raise RuntimeError("Redis credentials, ADMIN_ID and PC_AGENT_SECRET are required")

    print("Initializing Jarvis Windows agent...")
    redis = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
    state_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Jarvis"
    state_dir.mkdir(parents=True, exist_ok=True)
    guard = ReplayGuard(state_dir / "agent-replay.db")
    backoff = 2

    while True:
        try:
            consume_command(redis, COMMAND_KEY, int(ADMIN_ID), PC_AGENT_SECRET, guard, execute_command)
            backoff = 2
        except Exception as exc:
            print(f"Agent connection error: {type(exc).__name__}")
            backoff = min(backoff * 2, 60)
        time.sleep(backoff)


if __name__ == "__main__":
    start_agent()
