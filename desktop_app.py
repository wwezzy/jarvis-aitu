"""Windows desktop entrypoint; optional dependencies stay outside bot startup."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

from desktop.model_runtime import should_start_local_model


def write_report(path, report):
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _warm_local_chat(model):
    """Optional model load with no question/history and no UI/database access."""
    try:
        import socket
        import time
        from desktop.chat import LocalChat
        # The verified launcher may have just started Ollama. Wait only on
        # this loopback port, on the daemon worker, never on the GUI thread.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", 11434), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            return
        LocalChat(model, timeout=20).prewarm()
    except Exception:
        pass  # Warm-up failure never prevents commands or application startup.


def voice_diagnostics(*, extended=False):
    """Probe bundled native runtimes/model without recording or speaking."""
    report = {"kind": "voice-runtime", "microphone_started": False, "imports": {}}
    for name in ("vosk", "sounddevice", "pythoncom", "win32com.client"):
        try:
            importlib.import_module(name)
            report["imports"][name] = {"ok": True}
        except Exception as error:
            report["imports"][name] = {"ok": False, "error_class": type(error).__name__, "detail": str(error)[:700]}
    try:
        from desktop.model_setup import default_model_dir, model_ready
        model_path = default_model_dir()
        report["model_present"] = model_ready(model_path)
        if report["model_present"] and report["imports"]["vosk"]["ok"]:
            vosk = importlib.import_module("vosk")
            vosk.SetLogLevel(-1)
            model = vosk.Model(str(model_path))
            report["model_loaded"] = model is not None
    except Exception as error:
        report["model_loaded"] = False
        report["model_error_class"] = type(error).__name__
        report["model_error_detail"] = str(error)[:700]
    try:
        from desktop.neural_speech import NeuralSpeech
        speech = NeuralSpeech()
        neural_ready = speech.available() and speech.prepare(rate=185)
        neural = {"ready": bool(neural_ready), "reason": speech.reason,
                  "microphone_started": False, "audio_played": False}
        if neural_ready:
            pcm, sample_rate = speech.synthesize("Да, сэр", rate=185)
            neural.update(sample_rate=sample_rate, samples=len(pcm) // 2)
        report["neural_speech"] = neural
    except Exception as error:
        report["neural_speech"] = {"ready": False, "error_class": type(error).__name__}
    if extended:
        # Explicit operator probe only: no capture, speaker output, user texts
        # or model download. Exercise the actual installed native libraries.
        for key, module_name, class_name, options in (
            ("multilingual_stt", "desktop.multilingual_stt", "MultilingualSTT", {}),
            ("personal_speech", "desktop.expressive_speech", "ExpressiveSpeech", {"voice": "eugene", "personal_use": True}),
            ("clone_speech", "desktop.clone_speech", "CloneSpeech", {"personal_use": True}),
        ):
            backend = None
            try:
                backend = getattr(importlib.import_module(module_name), class_name)(**options)
                ready = bool(backend.prepare())
                report[key] = {"ready": ready, "reason": backend.reason,
                               "microphone_started": False, "audio_played": False}
            except Exception as error:
                report[key] = {"ready": False, "error_class": type(error).__name__}
            finally:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="JARVIS local desktop assistant")
    parser.add_argument("--state-dir", type=Path, help="Override local personal-data directory")
    parser.add_argument("--smoke-test", action="store_true", help="Open an isolated offscreen UI, with microphone and OS actions disabled")
    parser.add_argument("--quiet-start", action="store_true", help="Skip this launch's organizer greeting; retain the selected microphone mode")
    parser.add_argument("--screenshot", type=Path, help="Save a UI screenshot in smoke-test mode")
    parser.add_argument("--diagnose-voice", action="store_true", help="Check voice imports/model without recording or speaking")
    parser.add_argument("--extended-models", action="store_true", help="Include optional installed RU/EN, personal and clone native probes")
    parser.add_argument("--report", type=Path, help="Write private diagnostic JSON for smoke-test/voice diagnosis")
    args = parser.parse_args(argv)
    if args.screenshot and not args.smoke_test:
        parser.error("--screenshot requires --smoke-test")
    if args.report and not (args.smoke_test or args.diagnose_voice):
        parser.error("--report requires --smoke-test or --diagnose-voice")
    if args.extended_models and not args.diagnose_voice:
        parser.error("--extended-models requires --diagnose-voice")
    if args.diagnose_voice:
        report = voice_diagnostics(extended=True) if args.extended_models else voice_diagnostics()
        write_report(args.report, report)
        if sys.stdout is not None:
            print(json.dumps(report, ensure_ascii=False))
        if args.extended_models and not all(report.get(name, {}).get("ready") is True
                                            for name in ("multilingual_stt", "personal_speech", "clone_speech")):
            return 7
        return 0 if (all(item["ok"] for item in report["imports"].values())
                     and report.get("model_loaded") is True) else 5
    diagnostic = {"kind": "ui-smoke", "stage": "qt-import", "microphone_started": False, "os_actions": False, "personal_data_loaded": False}
    if args.smoke_test:
        write_report(args.report, diagnostic)
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        from PySide6.QtWidgets import QApplication, QMessageBox
    except (ImportError, OSError) as error:
        diagnostic.update(ok=False, error_class=type(error).__name__, detail=str(error)[:1000])
        write_report(args.report, diagnostic)
        if sys.stderr is not None:
            print("Desktop dependencies are missing. Install requirements-desktop.txt first.", file=sys.stderr)
        return 2
    from desktop.core import DesktopCore
    from desktop.ui import MainWindow
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("JARVIS Desktop")
    app.setOrganizationName("JarvisPersonal")
    temporary = tempfile.TemporaryDirectory(prefix="jarvis-ui-smoke-") if args.smoke_test else None
    state_dir = Path(temporary.name) if temporary else args.state_dir or Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "JarvisDesktop"
    socket_name = "JarvisDesktop_" + hashlib.sha256(str(state_dir.resolve()).casefold().encode()).hexdigest()[:20]
    server = None
    if not args.smoke_test:
        connection = QLocalSocket()
        connection.connectToServer(socket_name)
        if connection.waitForConnected(300):
            connection.write(b"activate")
            connection.waitForBytesWritten(300)
            connection.disconnectFromServer()
            return 0
        QLocalServer.removeServer(socket_name)
        server = QLocalServer(app)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(socket_name):
            print("Cannot start the local application instance.", file=sys.stderr)
            return 3
    class SmokeVoice:
        def __init__(self, **_kwargs):
            pass
        def available(self):
            return {"ready": False, "model": False, "reason": "Проверка интерфейса: микрофон выключен"}
        def start(self):
            return False
        def stop(self):
            pass
        def say(self, _text):
            pass
    core = None
    try:
        if not args.smoke_test:
            from desktop.runtime_env import create_phone_environment, load_phone_environment
            create_phone_environment(state_dir)
            load_phone_environment(state_dir)
        from desktop.personal import default_snapshot_path
        personal_path = state_dir / "no-personal-data.sqlite3" if args.smoke_test else default_snapshot_path()
        core = DesktopCore(state_dir, executor=(lambda _action: "Smoke-test executor") if args.smoke_test else None,
                           personal_path=personal_path)
        window = MainWindow(core, voice_factory=SmokeVoice if args.smoke_test else None,
                            enable_tray=not args.smoke_test,
                            personal_path=personal_path, enable_integrations=not args.smoke_test,
                            quiet_start=args.quiet_start)
        if not args.smoke_test:
            preferences = core.store.preferences()
            if should_start_local_model(preferences):
                from threading import Thread
                Thread(target=_warm_local_chat, args=(preferences["local_chat_model"],),
                       name="JarvisModelWarmup", daemon=True).start()
    except Exception as error:
        # Never reset a newer/corrupt database or silently disappear under
        # pythonw. Exception messages can contain personal content or paths.
        diagnostic.update(ok=False, stage="local-data", error_class=type(error).__name__)
        write_report(args.report, diagnostic)
        if not args.smoke_test:
            QMessageBox.critical(None, "Jarvis не запущен",
                "Не удалось открыть локальные данные или настройки. Данные не сброшены. "
                "Сохраните папку JarvisDesktop вместе с backups и восстановите проверенную копию. "
                "Тип ошибки: " + type(error).__name__)
        if core:
            try:
                core.close()
            except Exception:
                pass
        if server:
            server.close()
        if temporary:
            temporary.cleanup()
        return 6
    if server:
        def activate():
            client = server.nextPendingConnection()
            if client:
                client.disconnectFromServer()
                client.deleteLater()
            window.restore_window()
        server.newConnection.connect(activate)
    window.show()
    if args.smoke_test:
        diagnostic["stage"] = "window-created"
        write_report(args.report, diagnostic)
        def finish():
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(args.screenshot)):
                    diagnostic.update(ok=False, stage="screenshot-failed")
                    write_report(args.report, diagnostic)
                    app.exit(4)
                    return
            diagnostic.update(ok=True, stage="rendered")
            write_report(args.report, diagnostic)
            window.quit_application()
        QTimer.singleShot(350, finish)
    result = app.exec()
    if server:
        server.close()
    if temporary:
        temporary.cleanup()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
