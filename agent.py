"""Optional Windows agent: no model, shell, or remote arbitrary execution."""
import ctypes
import json
import logging
import logging.handlers
import os
import subprocess
import time
import threading
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
    elif cmd in {"shutdown", "restart", "cancel_shutdown"}:
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "shutdown.exe"
        arguments = ["/a"] if cmd == "cancel_shutdown" else ["/s" if cmd == "shutdown" else "/r", "/t", "30"]
        subprocess.run([str(executable), *arguments],
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
        self.monitor = None
        if self.result_path.exists():
            try:
                self.last = json.loads(self.result_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass

    def publish(self):
        data = signed_status({
            "user_id": self.user_id, "seen_at": int(time.time()), "state": "running",
            "version": "4.1", "last_result": self.last,
            "monitoring": self.monitor is not None,
            "activity_category": self.monitor.current if self.monitor else None,
        }, self.secret)
        if self.last:
            self.redis.set(f"jarvis:pc_result:{self.user_id}:{self.last['nonce']}", data, ex=86400)
        self.redis.set(f"jarvis:pc_status:{self.user_id}", data, ex=86400)

    def record(self, command, nonce, state, error=None):
        self.last = {"command": command, "nonce": nonce, "state": state,
                     "at": int(time.time()), "error": error}
        temporary = self.result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.last), encoding="utf-8")
        temporary.replace(self.result_path)

    def tick(self):
        self.publish()
        if self.monitor:
            try:
                self.monitor.sync(self.redis, self.user_id, self.secret)
            except Exception as error:
                # Optional observation must not prevent authenticated commands.
                logger.warning("Activity sync unavailable: %s", type(error).__name__)
        payload = self.redis.getdel(f"jarvis:pc_command:{self.user_id}")
        if not payload:
            return
        command = verify_signed_command(payload, self.user_id, self.secret)
        if command is None or not self.guard.claim(payload):
            logger.warning("Rejected invalid or replayed command")
            return
        nonce = json.loads(payload)["nonce"]
        self.record(command, nonce, "accepted")
        # Persist replay claim and publish ACK before any OS effect.
        self.publish()
        try:
            self.execute(command)
            self.record(command, nonce, "scheduled" if command in {"hibernate", "shutdown", "restart"} else "completed")
        except Exception as exc:
            self.record(command, nonce, "failed", type(exc).__name__)
        self.publish()


def local_config(state_dir):
    load_dotenv(state_dir / "agent.env", override=True)
    load_dotenv(override=False)


def doctor():
    """Read-only diagnostics. Never consumes commands or dumps configuration."""
    from services.redis_backend import backend_name, safe_transport_reason
    state_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Jarvis"
    local_config(state_dir)
    report = {"version": "4.1", "windows": os.name == "nt", "backend": backend_name(),
        "admin_configured": bool(os.getenv("ADMIN_ID", "").isdigit() and int(os.getenv("ADMIN_ID", "0")) > 0),
        "secret_configured": bool(os.getenv("PC_AGENT_SECRET")),
        "monitoring_enabled": os.getenv("PC_MONITOR_ENABLED", "0").lower() in {"1", "true", "yes"}}
    try:
        if os.getenv('PC_AGENT_SERVER_URL'):
            from services.agent_http import AgentHttps
            redis = AgentHttps(os.getenv('PC_AGENT_SERVER_URL'), int(os.getenv('ADMIN_ID', '0')), os.getenv('PC_AGENT_SECRET', ''))
            report['backend'] = 'agent_https'
        else:
            redis = create_redis(sync=True)
        report["redis_healthy"] = bool(redis and redis.ping())
        if redis:
            from services.pc_agent import read_status
            user_id, secret = int(os.getenv("ADMIN_ID", "0")), os.getenv("PC_AGENT_SECRET", "")
            data = read_status(redis.get(f"jarvis:pc_status:{user_id}"), user_id, secret)
            report["heartbeat_verified"] = bool(data and time.time() - data["seen_at"] <= 45)
            report["agent_version"] = data.get("version") if data else None
    except Exception as error:
        report.update(redis_healthy=False, reason=safe_transport_reason(error), error_class=type(error).__name__)
    return report


def start_agent():
    # Prefer a per-user agent env outside the repository. This keeps the
    # external Redis credential and HMAC secret out of git and makes Task
    # Scheduler startup independent from the interactive shell environment.
    state_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Jarvis"
    state_dir.mkdir(parents=True, exist_ok=True)
    local_config(state_dir)
    user_id, secret = int(os.getenv("ADMIN_ID", "0")), os.getenv("PC_AGENT_SECRET", "")
    if os.getenv('PC_AGENT_SERVER_URL'):
        from services.agent_http import AgentHttps
        redis = AgentHttps(os.getenv('PC_AGENT_SERVER_URL'), user_id, secret)
    else:
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
    stop_monitor, monitor_thread = threading.Event(), None
    if os.getenv("PC_MONITOR_ENABLED", "0").lower() in {"1", "true", "yes"}:
        from services.activity import ActivityJournal
        agent.monitor = ActivityJournal(state_dir / "activity.db")
        def record_activity():
            while not stop_monitor.is_set():
                try:
                    agent.monitor.tick()
                except Exception as error:
                    logger.warning("Activity sample unavailable error=%s", type(error).__name__)
                stop_monitor.wait(2)
        monitor_thread = threading.Thread(target=record_activity, name="JarvisActivity", daemon=True)
        monitor_thread.start()
    backoff = 2
    try:
        while True:
            try:
                agent.tick()
                backoff = 2
            except Exception as exc:
                from services.redis_backend import safe_transport_reason
                logger.warning("Agent transport failure error=%s reason=%s", type(exc).__name__, safe_transport_reason(exc))
                backoff = min(backoff * 2, 60)
            time.sleep(max(backoff, 5) if os.getenv('PC_AGENT_SERVER_URL') else backoff)
    finally:
        stop_monitor.set()
        if monitor_thread:
            monitor_thread.join(timeout=3)
        instance.close()
        handler.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Jarvis Windows agent")
    parser.add_argument("--doctor", action="store_true", help="Read-only safe diagnostics")
    parser.add_argument("--clear-activity", action="store_true", help="Delete only local activity history while the agent is stopped")
    options = parser.parse_args()
    if options.doctor:
        print(json.dumps(doctor(), ensure_ascii=False, indent=2))
    elif options.clear_activity:
        state_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Jarvis"
        state_dir.mkdir(parents=True, exist_ok=True)
        instance = SingleInstance(state_dir / "agent.lock")
        try:
            from services.activity import ActivityJournal
            ActivityJournal(state_dir / "activity.db").clear()
            print("Local activity history cleared.")
        finally:
            instance.close()
    else:
        start_agent()
