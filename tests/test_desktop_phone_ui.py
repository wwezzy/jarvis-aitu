"""Optional integration lifecycle with fakes; no sockets, providers or mic."""
import os
from pathlib import Path
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop GUI tests run in the desktop job")
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from desktop.core import DesktopCore, Reply  # noqa: E402
from desktop.ui import MainWindow  # noqa: E402


class Voice:
    def __init__(self, on_text, on_state, model_dir=None):
        self.on_text, self.on_state = on_text, on_state
        self.starts = self.stops = 0
        self.applied = []
        self.spoken = []

    def available(self):
        return {"ready": True, "model": True}

    def apply_preferences(self, preferences):
        self.applied.append(dict(preferences))

    def start(self):
        self.starts += 1
        return True

    def stop(self):
        self.stops += 1

    def say(self, text):
        self.spoken.append(text)


class Server:
    instances = []
    configured = True
    transport = "loopback_only"

    def __init__(self, api, state_dir, *, remote=None):
        self.api, self.state_dir = api, Path(state_dir)
        self.remote = remote
        self.started = self.stopped = False
        self.threads = [threading.get_ident()]
        type(self).instances.append(self)

    def status(self):
        return {"enabled": self.configured, "running": self.started,
                "transport": self.transport, "reason": "ready" if self.started else "configuration_required",
                "url": "https://never-display-secret.example", "phone_origin": "https://also-private.example"}

    def start(self):
        self.threads.append(threading.get_ident())
        self.started = self.configured
        return self.status()

    def stop(self):
        self.threads.append(threading.get_ident())
        self.stopped, self.started = True, False


class Delivery:
    instances = []
    telegram = calls = "not_configured"

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.enqueued = []
        self.pending = self.processed = 0
        self.threads = []
        self.release = None
        type(self).instances.append(self)

    def status(self):
        return {"telegram": self.telegram, "calls": self.calls, "voice": "provider_fallback",
                "counts": {"pending": self.pending} if self.pending else {}}

    def enqueue(self, event_id, text, *, is_current, call=False):
        self.threads.append(threading.get_ident())
        self.enqueued.append((event_id, text, is_current, call))
        self.pending += 1
        return True

    def process_pending(self):
        self.threads.append(threading.get_ident())
        self.processed += 1
        if self.release is not None:
            assert self.release.wait(3), "Test did not release fake delivery"
        self.pending = 0
        return self.status()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, condition):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            app.processEvents()
            return
        time.sleep(0.005)
    raise AssertionError("Optional integration worker did not settle")


@pytest.fixture
def make_window(app, monkeypatch, tmp_path):
    import desktop.phone
    import desktop.phone_delivery
    Server.instances, Delivery.instances = [], []
    monkeypatch.setattr(Server, "configured", True)
    monkeypatch.setattr(Server, "transport", "loopback_only")
    monkeypatch.setattr(Delivery, "telegram", "not_configured")
    monkeypatch.setattr(Delivery, "calls", "not_configured")
    monkeypatch.setattr(desktop.phone, "PhoneServer", Server)
    monkeypatch.setattr(desktop.phone_delivery, "PhoneDelivery", Delivery)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "isolated-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "isolated-home"))
    monkeypatch.setattr(DesktopCore, "due_events", lambda *_args, **_kwargs: [])
    windows = []

    def create(*, enabled=False):
        core = DesktopCore(tmp_path / str(len(windows)), executor=lambda _action: "fake action")
        widget = MainWindow(core, voice_factory=Voice, enable_tray=False,
                            personal_path=tmp_path / "absent.db", enable_integrations=enabled)
        windows.append(widget)
        widget.show()
        wait_for(app, lambda: not widget._integration_workers)
        return widget

    yield create
    for widget in windows:
        for delivery in Delivery.instances:
            if delivery.release is not None:
                delivery.release.set()
        widget._quitting = True
        widget.close()
        wait_for(app, lambda: widget._resources_closed)


def test_defaults_never_construct_integrations_even_if_environment_is_present(make_window, monkeypatch):
    monkeypatch.setenv("JARVIS_PHONE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_PHONE_TOKEN", "synthetic-token-that-must-never-be-consumed")
    window = make_window()
    assert Server.instances == Delivery.instances == []
    assert not window._integration_timer.isActive()
    assert "не настроен" in window.phone_status.text()
    assert "не подключён" in window.phone_delivery_status.text()


def test_explicit_flag_configures_lifecycle_off_gui_thread_without_exposing_urls(app, make_window):
    window = make_window(enabled=True)
    server, delivery = Server.instances[0], Delivery.instances[0]
    assert server.api.organizer is window.core.organizer
    assert server.remote is window._remote_controller
    assert server.state_dir == window.core.store.state_dir
    assert server.started and not server.stopped
    assert all(thread_id != threading.get_ident() for thread_id in server.threads)
    assert window._integration_pool.maxThreadCount() == 1
    assert window._integration_pool is not window.pool
    assert "Локальный мост готов" in window.phone_status.text()
    assert "зашифрованное подключение" in window.phone_status.text()
    assert "телефон подключен" not in window.phone_status.text().lower()
    assert not any("secret.example" in value.text() or "private.example" in value.text()
                   for value in window.findChildren(QLabel))
    window._poll_phone_integrations()
    assert delivery.processed == 0
    window.quit_application()
    wait_for(app, lambda: window._resources_closed)
    assert server.stopped and all(thread_id != threading.get_ident() for thread_id in server.threads)


def test_disabled_adapter_is_honest_and_does_not_process_delivery(make_window, monkeypatch):
    monkeypatch.setattr(Server, "configured", False)
    window = make_window(enabled=True)
    assert not Server.instances[0].started
    assert "не настроен" in window.phone_status.text()
    assert "не подключены" in window.phone_delivery_status.text()
    window._poll_phone_integrations()
    assert not Delivery.instances[0].processed


def test_https_origin_does_not_claim_verified_transport_or_device(make_window, monkeypatch):
    monkeypatch.setattr(Server, "transport", "https_proxy_not_verified")
    window = make_window(enabled=True)
    assert "ещё не проверено" in window.phone_status.text()
    assert "телефон подключен" not in window.phone_status.text().lower()


def test_only_current_organizer_events_forward_and_calls_require_evening_optin(app, make_window, monkeypatch):
    monkeypatch.setattr(Delivery, "telegram", "configured")
    monkeypatch.setattr(Delivery, "calls", "configured")
    window = make_window(enabled=True)
    current = {"value": True}
    monkeypatch.setattr(window.core, "event_is_current", lambda _reply: current["value"])
    morning = Reply("Доброе утро", organizer_event={"id": "morning:2026-10-29", "kind": "morning"})
    evening = Reply("Планируем завтра", organizer_event={"id": "evening:2026-10-29", "kind": "evening"})
    window._deliver_notifications([Reply("Обычный таймер"), morning, evening])
    wait_for(app, lambda: not window._integration_workers)
    delivery = Delivery.instances[0]
    assert [(row[0], row[1], row[3]) for row in delivery.enqueued] == [
        ("morning:2026-10-29", "Доброе утро", False), ("evening:2026-10-29", "Планируем завтра", True)]
    assert delivery.processed == 1
    assert all(thread_id != threading.get_ident() for thread_id in delivery.threads)
    assert "получение не подтверждено" in window.phone_delivery_status.text()
    current["value"] = False
    assert not any(row[2]() for row in delivery.enqueued)
    window._deliver_notifications([evening])
    assert len(delivery.enqueued) == 2


def test_phone_work_rechecks_after_queue_and_never_routes_event_text_to_core(app, make_window, monkeypatch):
    monkeypatch.setattr(Delivery, "telegram", "configured")
    window = make_window(enabled=True)
    current = {"value": True}
    monkeypatch.setattr(window.core, "event_is_current", lambda _reply: current["value"])
    monkeypatch.setattr(window.core, "handle", lambda *_args, **_kwargs: pytest.fail("Phone event became a command"))
    # Hold the optional worker, enqueue an event, then invalidate it before it runs.
    release = threading.Event()
    window._run_integration(lambda: release.wait(3), lambda _value: window._schedule_phone_work())
    reply = Reply("открой калькулятор", organizer_event={"id": "evening:synthetic", "kind": "evening"})
    window._deliver_notifications([reply])
    current["value"] = False
    release.set()
    wait_for(app, lambda: not window._integration_workers)
    assert Delivery.instances[0].enqueued == []


def test_provider_does_not_block_commands_or_close_gui_and_guard_expires(app, make_window, monkeypatch):
    monkeypatch.setattr(Delivery, "telegram", "configured")
    window = make_window(enabled=True)
    delivery = Delivery.instances[0]
    delivery.release = threading.Event()
    monkeypatch.setattr(window.core, "event_is_current", lambda _reply: True)
    window._deliver_notifications([Reply("План", organizer_event={"id": "evening:wait", "kind": "evening"})])
    wait_for(app, lambda: delivery.processed == 1)
    window.submit("как дела")
    wait_for(app, lambda: not window.busy)
    assert window._integration_workers  # Provider still waits independently.
    window.quit_application()
    app.processEvents()
    assert not window._resources_closed and not window.listening
    assert not delivery.enqueued[0][2]()
    delivery.release.set()
    wait_for(app, lambda: window._resources_closed)
    assert Server.instances[0].stopped


def test_phone_plan_poll_only_when_visible_preserves_unsaved_form(make_window, monkeypatch):
    window = make_window(enabled=True)
    calls = []
    refresh = window.plan.refresh
    monkeypatch.setattr(window.plan, "refresh", lambda: (calls.append(True), refresh()))
    window._poll_phone_integrations()
    assert not calls
    window.navigate(window.PLAN_PAGE)
    window.plan.item_title.setText("Несохранённый черновик")
    calls.clear()
    today = window.core.organizer.snapshot()["today"]["date"]
    window.core.organizer.add_item("Добавлено с телефона", today)
    window._poll_phone_integrations()
    assert calls == [True]
    assert window.plan.item_title.text() == "Несохранённый черновик"
    assert any(item["title"] == "Добавлено с телефона" for item in window.plan.items)
    assert window._integration_timer.interval() == 5000


def test_multilingual_change_restarts_active_capture_and_drops_old_session(app, make_window, monkeypatch):
    window = make_window()
    window.start_listening()
    wait_for(app, lambda: window.listening)
    old_callback, old_session = window.voice.on_text, window._capture_session
    window.multilingual_toggle.setChecked(True)
    wait_for(app, lambda: window.listening and not window._workers)
    assert window.voice.starts == 2 and window.voice.stops >= 1
    assert window._capture_session > old_session
    assert window.voice.applied[-1]["multilingual_stt_enabled"] is True
    monkeypatch.setattr(window.core, "handle", lambda *_args, **_kwargs: pytest.fail("Stale speech dispatched"))
    old_callback("открой калькулятор")
    app.processEvents()
    assert not window.busy


def test_multilingual_change_while_off_keeps_microphone_off(make_window):
    window = make_window()
    window.multilingual_toggle.setChecked(True)
    assert window.voice.starts == window.voice.stops == 0
    assert not window.listening
    assert window.voice.applied[-1]["multilingual_stt_enabled"] is True


def test_capture_restart_refusal_does_not_leave_starting_status(app, make_window, monkeypatch):
    window = make_window()
    window.start_listening()
    wait_for(app, lambda: window.listening)
    monkeypatch.setattr(window.voice, "start", lambda: False)
    window.multilingual_toggle.setChecked(True)
    wait_for(app, lambda: not window._workers)
    assert not window.listening and not window._capture_requested
    assert "Микрофон не запущен" in window.status.text()


def remote_environment(monkeypatch):
    monkeypatch.setenv("JARVIS_PHONE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_PHONE_TOKEN", "Synthetic_Phone_Secret_0123456789_AbCdEfGhIjKl")
    monkeypatch.setenv("JARVIS_REMOTE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_REMOTE_TOKEN", "Different_Control_Secret_9876543210_ZyXwVuTsRq")
    monkeypatch.setenv("JARVIS_PHONE_ORIGIN", "https://laptop.example-network.ts.net")


def test_paired_remote_access_survives_local_hello_expiry_but_not_revocation_or_exit(app, make_window, monkeypatch):
    remote_environment(monkeypatch)
    window = make_window(enabled=True)
    owner = window.core.owner
    owner.set_enabled(True)
    monkeypatch.setattr(owner, "allowed", lambda: True)
    assert window.core.approve_remote(True)
    controller = window._remote_controller
    effects = []
    monkeypatch.setattr(window.core, "executor", lambda action: effects.append(action) or "Тестовый обработчик завершён.")
    monkeypatch.setattr(owner, "allowed", lambda: False)
    assert controller.status()["available"]
    assert controller.request({"kind": "action", "name": "calculator"})["ok"]
    assert effects == ["calculator"]
    assert window.voice.spoken == []
    assert window.core.approve_remote(False)
    assert not controller.status()["available"]
    controller.request({"kind": "action", "name": "calculator"})
    assert effects == ["calculator"]
    # Durable pairing remains after a normal exit; its live adapter gate does
    # not accept any command during shutdown, even with the old key in RAM.
    monkeypatch.setattr(owner, "allowed", lambda: True)
    assert window.core.approve_remote(True)
    path = window.core.store.state_dir
    window.quit_application()
    assert not controller.status()["available"]
    wait_for(app, lambda: window._resources_closed)
    from desktop.store import LocalStore
    reopened = LocalStore(path)
    try:
        assert reopened.preferences()["remote_control_approved"] is True
    finally:
        reopened.close()
    assert effects == ["calculator"]


def test_phone_setup_navigation_uses_explicit_panel_without_opening_document(make_window, monkeypatch):
    window = make_window()
    calls = []
    monkeypatch.setattr(window.remote_setup, "refresh", lambda: calls.append("status"))
    window._open_phone_setup()
    assert window._view_index == window.CONTROLS_PAGE and calls == ["status"]


def test_private_route_configuration_requires_explicit_owner_and_private_origin(make_window, monkeypatch):
    window = make_window(enabled=True)
    monkeypatch.setattr(window.core.owner, "allowed", lambda: True)
    assert not window._apply_remote_setup("https://laptop.example-network.ts.net")
    window.core.owner.set_enabled(True)
    for invalid in ("https://public.example", "https://laptop.example-network.ts.net/path", "http://laptop.example-network.ts.net", None):
        assert not window._apply_remote_setup(invalid)
    window._integration_busy = True
    assert not window._apply_remote_setup("https://laptop.example-network.ts.net")
    window._integration_busy = False
    window._integrations_enabled = False
    assert not window._apply_remote_setup("https://laptop.example-network.ts.net")
    assert len(Server.instances) == 1


def test_private_route_apply_writes_off_qt_and_restarts_only_owned_loopback(app, make_window, monkeypatch):
    import desktop.runtime_env
    remote_environment(monkeypatch)
    window = make_window(enabled=True)
    window.core.owner.set_enabled(True)
    monkeypatch.setattr(window.core.owner, "allowed", lambda: True)
    calls = []
    def update(state_dir, **kwargs):
        calls.append((state_dir, kwargs, threading.get_ident()))
        assert kwargs["authorization_guard"]() is True
        return "updated"
    monkeypatch.setattr(desktop.runtime_env, "update_phone_environment", update)
    previous = Server.instances[0]
    origin = "https://laptop.another-network.ts.net"
    assert window._apply_remote_setup(origin)
    wait_for(app, lambda: len(Server.instances) == 2 and not window._integration_workers)
    assert calls[0][0] == window.core.store.state_dir
    assert calls[0][1]["origin"] == origin and calls[0][1]["remote_enabled"] is True
    assert calls[0][2] != threading.get_ident()
    assert previous.stopped and window._phone_server is Server.instances[1]
    assert window._phone_server.started and window._phone_server.remote is window._remote_controller
    assert os.environ["JARVIS_PHONE_ORIGIN"] == origin
    assert not window._remote_configuration_pending
    assert "ещё не проверено" in window.remote_setup.status_label.text()


def test_failed_route_write_preserves_running_server_and_releases_retry(app, make_window, monkeypatch):
    import desktop.runtime_env
    window = make_window(enabled=True)
    window.core.owner.set_enabled(True)
    monkeypatch.setattr(window.core.owner, "allowed", lambda: True)
    monkeypatch.setattr(desktop.runtime_env, "update_phone_environment", lambda *_args, **_kwargs: "refused")
    assert window._apply_remote_setup("https://laptop.example-network.ts.net")
    wait_for(app, lambda: not window._integration_workers)
    assert len(Server.instances) == 1 and Server.instances[0].started and not Server.instances[0].stopped
    assert not window._remote_configuration_pending
    assert "Не удалось" in window.remote_setup.status_label.text()
