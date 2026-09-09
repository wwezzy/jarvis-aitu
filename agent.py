import ctypes
import os
import time

from dotenv import load_dotenv
from upstash_redis import Redis

from services.pc_agent import verify_signed_command

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

    while True:
        try:
            payload = redis.get(COMMAND_KEY)
            if payload:
                redis.delete(COMMAND_KEY)
                cmd = verify_signed_command(payload, int(ADMIN_ID), PC_AGENT_SECRET)
                if cmd:
                    execute_command(cmd)
                else:
                    print("Rejected invalid or expired PC command")
        except Exception as exc:
            print(f"Agent connection error: {type(exc).__name__}")
        time.sleep(2)


if __name__ == "__main__":
    start_agent()
