"""Offscreen private setup: fake CLI/download/installer/auth/clipboard only."""
import hashlib
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import weakref

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop UI dependency")
from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from desktop import remote_setup_ui as module  # noqa: E402


ORIGIN = "https://laptop.synthetic-tail.ts.net"
PLAN_KEY = "SyntheticPlannerKey_0123456789abcdef"
CONTROL_KEY = "SyntheticControlKey_9876543210fedcba"
CONSENT = "https://login.tailscale.com/admin/https?next=synthetic-consent"


def ready():
    return {"reason": "ready", "installed": True, "logged_in": True, "serve_configured": True,
            "origin": ORIGIN, "phone_connection_verified": False}


class FakeOwner:
    def __init__(self):
        self.enabled, self.verified = True, False
        self.calls = 0
        self.closed = False

    def allowed(self):
        assert not self.closed, "Owner accessed after Core closed"
        self.calls += 1
        return self.verified if self.enabled else True


class FakeSetup:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.calls = []
        self.status_result = {"reason": "not_installed"}
        self.configure_result = ready()
        self.download_result = {"ok": True, "reason": "installer_ready", "launched": False}
        self.installer_result = None
        self.guard = None

    def mark(self, name):
        self.calls.append((name, threading.get_ident()))

    def status(self):
        self.mark("status")
        return self.status_result

    def installer_info(self):
        self.mark("info")
        return {"url": module._OFFICIAL_INSTALLER, "max_bytes": 100 * 1024 * 1024,
                "runs_automatically": False, "requires_user_installation": True,
                "requires_windows_and_iphone_login": True}

    def download_installer(self, *, cancel_event=None):
        self.mark("download")
        assert isinstance(cancel_event, threading.Event) and not cancel_event.is_set()
        return self.download_result

    def verified_installer(self):
        self.mark("verify_installer")
        return self.installer_result

    def configure_serve(self, approved=False, *, approval_guard=None):
        self.mark("configure")
        assert approved is True and callable(approval_guard)
        self.guard = approval_guard
        assert approval_guard() is True
        return self.configure_result


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ("JARVIS_PHONE_ENABLED", "JARVIS_PHONE_ORIGIN", "JARVIS_PHONE_TOKEN", "JARVIS_REMOTE_ENABLED", "JARVIS_REMOTE_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def panel(app, tmp_path, monkeypatch):
    core = SimpleNamespace(owner=FakeOwner(), store=SimpleNamespace(state_dir=tmp_path / "state"))
    setup = FakeSetup(core.store.state_dir)
    configured, opened, copied = [], [], []
    widget = module.RemoteSetupPanel(core, setup=setup, on_configured=lambda value: configured.append((value, threading.get_ident())))
    widget.configured, widget.opened, widget.copied = configured, opened, copied
    monkeypatch.setattr(module.QDesktopServices, "openUrl", lambda url: opened.append((url.toString(), threading.get_ident())) or True)
    monkeypatch.setattr(module, "_write_clipboard", copied.append)
    widget.show()
    yield widget
    widget.shutdown()
    settled(app, widget, condition=lambda: widget.shutdown_complete)
    widget.core.owner.closed = True
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def settled(app, widget, *, condition=None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if not widget.busy and (condition is None or condition()):
            app.processEvents()
            return
        time.sleep(0.005)
    raise AssertionError("Remote setup worker did not settle")


def owner_verified(widget):
    widget.core.owner.verified = True
    widget._sync()


def paired(widget, monkeypatch):
    owner_verified(widget)
    widget._apply_status("status", ready())
    widget._sync()
    for key, value in {"JARVIS_PHONE_ENABLED": "1", "JARVIS_PHONE_ORIGIN": ORIGIN, "JARVIS_PHONE_TOKEN": PLAN_KEY,
                       "JARVIS_REMOTE_ENABLED": "1", "JARVIS_REMOTE_TOKEN": CONTROL_KEY}.items():
        monkeypatch.setenv(key, value)


def installer(widget):
    path = widget.core.store.state_dir / "installers" / "tailscale-setup-latest.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"MZ" + b"synthetic-vendor-signed-fixture-never-executed" * 10)
    widget.setup.installer_result = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return path


def test_constructor_never_probes_downloads_authorizes_or_opens(panel, app):
    app.processEvents()
    assert panel.setup.calls == panel.opened == panel.copied == panel.configured == []
    assert not panel.core.store.state_dir.exists()
    assert panel._signals.parent() is panel
    assert panel.shutdown_complete is False
    assert not panel.configure_button.isEnabled()
    assert not panel.copy_link_button.isEnabled() and not panel.copy_control_button.isEnabled()
    assert all(label.textFormat() == Qt.TextFormat.PlainText for label in panel.findChildren(QLabel))


def test_default_setup_constructor_is_lazy_and_keeps_same_api(app, tmp_path, monkeypatch):
    from desktop import remote_setup
    constructed = []
    def make_setup(state_dir):
        constructed.append(state_dir)
        return FakeSetup(state_dir)
    monkeypatch.setattr(remote_setup, "RemoteSetup", make_setup)
    core = SimpleNamespace(owner=FakeOwner(), store=SimpleNamespace(state_dir=tmp_path / "state"))
    widget = module.RemoteSetupPanel(core)
    assert constructed == [core.store.state_dir] and widget.setup.calls == []
    widget.shutdown()
    assert widget.shutdown_complete
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_refresh_is_explicit_read_only_worker_and_has_three_manual_login_steps(panel, app):
    panel.setup.status_result = {"reason": "login_required", "message": "private-error-token"}
    panel.refresh_button.click()
    settled(app, panel)
    assert [name for name, _ in panel.setup.calls] == ["status"]
    assert all(thread != threading.get_ident() for _, thread in panel.setup.calls)
    assert "Войди в Tailscale" in panel.status_label.text()
    assert all(value in panel.steps_label.text() for value in ("1.", "2.", "3.", "Windows", "iPhone"))
    assert "private-error-token" not in panel.status_label.text()
    assert panel.configured == panel.opened == panel.copied == []


def test_explicit_download_never_opens_installer_and_all_backend_work_is_off_gui(panel, app):
    panel.download_button.click()
    settled(app, panel)
    assert [name for name, _ in panel.setup.calls] == ["info", "download"]
    assert all(thread != threading.get_ident() for _, thread in panel.setup.calls)
    assert panel.opened == [] and "ещё не запущен" in panel.status_label.text()


@pytest.mark.parametrize("field,value", [("url", "https://example.test/installer.exe"), ("runs_automatically", True),
    ("requires_user_installation", False), ("requires_windows_and_iphone_login", False), ("max_bytes", True), ("max_bytes", 200_000_000)])
def test_download_requires_official_nonautomatic_bounded_metadata(panel, app, monkeypatch, field, value):
    info = panel.setup.installer_info()
    panel.setup.calls.clear()
    info[field] = value
    monkeypatch.setattr(panel.setup, "installer_info", lambda: info)
    panel.download_button.click()
    settled(app, panel)
    assert panel.setup.calls == panel.opened == []
    assert "Не удалось" in panel.status_label.text()


def test_installer_is_reverified_by_explicit_click_and_opened_only_on_gui(panel, app):
    path = installer(panel)
    panel.installer_button.click()
    settled(app, panel)
    assert [name for name, _ in panel.setup.calls] == ["verify_installer"]
    assert panel.setup.calls[0][1] != threading.get_ident()
    assert panel.opened == [(module.QUrl.fromLocalFile(str(path)).toString(), threading.get_ident())]
    assert "передан Windows" in panel.status_label.text()


@pytest.mark.parametrize("case", ["wrong_path", "relative", "bad_hash", "changed_hash", "missing", "small"])
def test_installer_path_signature_and_bytes_are_required_before_open(panel, app, tmp_path, case):
    path = installer(panel)
    if case == "wrong_path":
        other = tmp_path / "other.exe"
        other.write_bytes(path.read_bytes())
        panel.setup.installer_result["path"] = str(other)
    elif case == "relative":
        panel.setup.installer_result["path"] = "installers/tailscale-setup-latest.exe"
    elif case == "bad_hash":
        panel.setup.installer_result["sha256"] = "invalid-private-token"
    elif case == "changed_hash":
        path.write_bytes(path.read_bytes() + b"updated")
    elif case == "missing":
        path.unlink()
    else:
        path.write_bytes(b"MZ")
        panel.setup.installer_result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    panel.installer_button.click()
    settled(app, panel)
    assert panel.opened == [] and "не готов" in panel.status_label.text()


def test_replaced_installer_between_worker_and_queued_gui_result_is_not_opened(panel, app):
    path = installer(panel)
    panel.installer_button.click()
    deadline = time.monotonic() + 2
    while any(thread.is_alive() for thread, _ in panel._worker_threads.values()) and time.monotonic() < deadline:
        time.sleep(0.005)  # Deliberately do not drain the queued GUI completion.
    path.write_bytes(path.read_bytes() + b"swapped-before-gui-dispatch")
    settled(app, panel)
    assert panel.opened == []


def test_configuration_requires_enabled_verified_owner_before_worker(panel, app):
    panel._configure()
    panel.core.owner.enabled = False  # allowed() intentionally returns True.
    panel._configure()
    assert panel.setup.calls == panel.configured == []
    assert "Windows Hello" in panel.status_label.text()
    panel.core.owner.enabled = True
    owner_verified(panel)
    panel.configure_button.click()
    settled(app, panel)
    assert [name for name, _ in panel.setup.calls] == ["configure"]
    assert panel.configured == [(ORIGIN, threading.get_ident())]
    assert callable(panel.setup.guard)
    assert "iPhone ещё нужно проверить" in panel.status_label.text()


def test_configuration_rechecks_after_backend_before_any_gui_callback(panel, app, monkeypatch):
    owner_verified(panel)
    def expired(approved=False, *, approval_guard):
        assert approved and approval_guard()
        panel.core.owner.verified = False
        assert not approval_guard()
        return ready()
    monkeypatch.setattr(panel.setup, "configure_serve", expired)
    panel.configure_button.click()
    settled(app, panel)
    assert panel.configured == [] and panel._origin is None
    assert "Windows Hello" in panel.status_label.text()


def test_queued_configuration_result_rechecks_owner_before_callback(panel, app):
    owner_verified(panel)
    panel.configure_button.click()
    deadline = time.monotonic() + 2
    while any(thread.is_alive() for thread, _ in panel._worker_threads.values()) and time.monotonic() < deadline:
        time.sleep(0.005)
    panel.core.owner.verified = False
    settled(app, panel)
    assert panel.configured == [] and panel._origin is None


@pytest.mark.parametrize("outcome", [False, "exception", "revoked"])
def test_callback_failure_never_enables_pairing_or_leaks_errors(panel, app, outcome):
    owner_verified(panel)
    def callback(origin):
        assert origin == ORIGIN and threading.get_ident() == threading.main_thread().ident
        if outcome == "exception":
            raise RuntimeError("private-url-secret")
        if outcome == "revoked":
            panel.core.owner.verified = False
            return True
        return False
    panel.on_configured = callback
    panel.configure_button.click()
    settled(app, panel)
    assert panel._origin is None and not panel.copy_link_button.isEnabled()
    assert "private-url-secret" not in panel.status_label.text()


@pytest.mark.parametrize("origin", ["http://laptop.synthetic-tail.ts.net", "https://evil.test", "https://laptop.synthetic-tail.ts.net/", "https://laptop.synthetic-tail.ts.net:443", "https://user@laptop.synthetic-tail.ts.net", "https://laptop.synthetic-tail.ts.net?token=secret", "https://laptop.synthetic-tail.ts.net/#secret", "https://laptop.synthetic-tail.ts.net\\@evil.test"])
def test_untrusted_origins_never_reach_callback_or_pairing(panel, app, origin):
    owner_verified(panel)
    panel.setup.configure_result = {**ready(), "origin": origin, "message": "private-token"}
    panel.configure_button.click()
    settled(app, panel)
    assert panel._origin is None and panel.configured == []
    assert "не подтверждён" in panel.status_label.text() and "private-token" not in panel.status_label.text()


def test_ready_status_is_not_device_connected_and_does_not_apply_environment(panel, app):
    owner_verified(panel)
    panel.setup.status_result = {**ready(), "phone_connection_verified": True, "message": "private-token"}
    panel.refresh_button.click()
    settled(app, panel)
    assert panel._origin == ORIGIN and panel.configured == []
    assert "iPhone ещё нужно проверить" in panel.status_label.text()
    assert "private-token" not in panel.status_label.text()
    panel._copy_link()
    assert panel.copied == []


@pytest.mark.parametrize("field", ["installed", "logged_in", "serve_configured"])
def test_ready_requires_complete_boolean_service_evidence(panel, app, field):
    owner_verified(panel)
    panel.setup.configure_result = {**ready(), field: False}
    panel.configure_button.click()
    settled(app, panel)
    assert panel._origin is None and panel.configured == []


def test_consent_link_stays_in_ram_and_opens_only_by_verified_owner_click(panel, app):
    owner_verified(panel)
    panel.setup.configure_result = {"reason": "needs_user_consent", "consent_url": CONSENT}
    panel.configure_button.click()
    settled(app, panel)
    assert panel.opened == [] and panel.consent_button.isVisible()
    assert all(CONSENT not in label.text() and "synthetic-consent" not in label.text() for label in panel.findChildren(QLabel))
    panel.consent_button.click()
    assert panel.opened == [(CONSENT, threading.get_ident())]
    panel.core.owner.verified = False
    panel._open_consent()
    assert len(panel.opened) == 1


@pytest.mark.parametrize("url", ["https://evil.test/", "https://login.tailscale.com.evil.test/", "https://evil@login.tailscale.com/", "https://login.tailscale.com:443/", "http://login.tailscale.com/", "https://login.tailscale.com/a#secret", "https://login.tailscale.com\\@evil.test/", "https://login.tailscale.com/a\nprivate"])
def test_invalid_consent_urls_are_not_available_or_opened(panel, app, url):
    owner_verified(panel)
    panel.setup.configure_result = {"reason": "needs_user_consent", "consent_url": url}
    panel.configure_button.click()
    settled(app, panel)
    assert not panel.consent_button.isVisible() and panel._consent_url is None
    panel._open_consent()
    assert panel.opened == []


def test_pairing_copies_fragment_planner_and_separate_control_only_explicitly(panel, monkeypatch):
    paired(panel, monkeypatch)
    assert panel.copied == []
    panel.copy_link_button.click()
    panel.copy_control_button.click()
    assert panel.copied == [ORIGIN + "/#token=" + PLAN_KEY, CONTROL_KEY]
    assert CONTROL_KEY not in panel.copied[0] and "?" not in panel.copied[0]
    assert all(key not in label.text() for label in panel.findChildren(QLabel) for key in (PLAN_KEY, CONTROL_KEY))
    assert panel.setup.calls == panel.opened == []


@pytest.mark.parametrize("name,value", [("JARVIS_PHONE_ENABLED", "0"), ("JARVIS_PHONE_ORIGIN", "https://other.synthetic-tail.ts.net"), ("JARVIS_PHONE_TOKEN", "weak")])
def test_planner_copy_refuses_unconfigured_or_mismatched_private_environment(panel, monkeypatch, name, value):
    paired(panel, monkeypatch)
    monkeypatch.setenv(name, value)
    panel._copy_link()
    assert panel.copied == []


@pytest.mark.parametrize("name,value", [("JARVIS_REMOTE_ENABLED", "0"), ("JARVIS_REMOTE_TOKEN", "weak"), ("JARVIS_REMOTE_TOKEN", PLAN_KEY)])
def test_control_copy_requires_distinct_strong_explicitly_enabled_key(panel, monkeypatch, name, value):
    paired(panel, monkeypatch)
    monkeypatch.setenv(name, value)
    panel._copy_control()
    assert panel.copied == []


@pytest.mark.parametrize("kind", ["planner", "control"])
def test_pairing_rechecks_owner_immediately_before_clipboard_and_updates_status(panel, monkeypatch, kind):
    paired(panel, monkeypatch)
    calls = []
    def allowed():
        calls.append(True)
        return len(calls) == 1
    panel.core.owner.allowed = allowed
    (panel._copy_link if kind == "planner" else panel._copy_control)()
    assert len(calls) == 2 and panel.copied == []
    assert "Windows Hello" in panel.status_label.text()


def test_owner_expiry_disables_sensitive_buttons_without_cli_probes(panel, monkeypatch):
    paired(panel, monkeypatch)
    assert panel.copy_link_button.isEnabled() and panel.copy_control_button.isEnabled()
    panel.core.owner.verified = False
    panel._sync()
    assert not panel.configure_button.isEnabled() and not panel.copy_link_button.isEnabled()
    assert panel.setup.calls == []


def test_failed_status_clears_old_ready_origin_and_never_shows_exception(panel, app, monkeypatch):
    paired(panel, monkeypatch)
    def failure():
        raise RuntimeError("https://private.test/token-secret")
    monkeypatch.setattr(panel.setup, "status", failure)
    panel.refresh_button.click()
    settled(app, panel)
    assert panel._origin is None and not panel.copy_link_button.isEnabled()
    assert "token-secret" not in panel.status_label.text()


def test_malformed_status_is_safe_literal_fixed_feedback(panel, app):
    panel.setup.status_result = {"reason": {"secret": "private"}, "message": '<img src="private">'}
    panel.refresh_button.click()
    settled(app, panel)
    assert "private" not in panel.status_label.text() and "безопасно" in panel.status_label.text()


def test_browser_clipboard_failures_are_fixed_without_logging_secrets(panel, monkeypatch, capsys):
    paired(panel, monkeypatch)
    def failed(*_args):
        raise OSError(PLAN_KEY + CONTROL_KEY + CONSENT)
    monkeypatch.setattr(module, "_write_clipboard", failed)
    panel._copy_link()
    panel._copy_control()
    panel._consent_url = CONSENT
    monkeypatch.setattr(module.QDesktopServices, "openUrl", failed)
    panel._open_consent()
    output = capsys.readouterr()
    assert not output.err and not output.out
    assert all(value not in panel.status_label.text() for value in (PLAN_KEY, CONTROL_KEY, CONSENT))


def test_shutdown_tracks_actual_worker_and_drops_late_callback_without_core_reads(panel, app, monkeypatch):
    owner_verified(panel)
    entered, release = threading.Event(), threading.Event()
    def slow_configure(approved=False, *, approval_guard):
        assert approved and approval_guard()
        entered.set()
        assert release.wait(2)
        assert not approval_guard()
        return ready()
    monkeypatch.setattr(panel.setup, "configure_serve", slow_configure)
    panel.configure_button.click()
    try:
        assert entered.wait(1)
        panel.shutdown()
        before = panel.status_label.text()
        assert not panel.busy and not panel.shutdown_complete
        assert not panel.access_timer.isActive()
        assert not panel._run("status", lambda: pytest.fail("Started after shutdown"))
        # Even the live authorization guard short-circuits before accessing a
        # Core that its caller has mistakenly released early.
        panel.core.owner.closed = True
    finally:
        release.set()
    settled(app, panel, condition=lambda: panel.shutdown_complete)
    assert panel._worker_threads == {} and panel.configured == []
    assert panel.status_label.text() == before


def test_shutdown_cancels_inflight_download_and_waits_for_actual_cleanup(panel, app, monkeypatch):
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    closing = panel._closing
    def download(*, cancel_event):
        assert cancel_event is closing
        entered.set()
        assert cancel_event.wait(2)
        cancelled.set()
        assert release.wait(2)
        return {"ok": False, "reason": "download_failed", "launched": False}
    monkeypatch.setattr(panel.setup, "download_installer", download)
    panel.download_button.click()
    try:
        assert entered.wait(1)
        panel.shutdown()
        assert cancelled.wait(1)
        assert not panel.shutdown_complete
        before = panel.status_label.text()
    finally:
        release.set()
    settled(app, panel, condition=lambda: panel.shutdown_complete)
    assert panel.status_label.text() == before and panel.opened == []


def test_late_direct_handlers_after_shutdown_have_no_side_effects_or_core_reads(panel, app, monkeypatch):
    paired(panel, monkeypatch)
    panel._consent_url = CONSENT
    panel.shutdown()
    panel.core.owner.closed = True
    before = panel.status_label.text()
    for handler in (panel.refresh, panel._download, panel._open_installer, panel._configure,
                    panel._open_consent, panel._copy_link, panel._copy_control):
        handler()
    app.processEvents()
    assert panel.status_label.text() == before
    assert panel.setup.calls == panel.opened == panel.copied == panel.configured == []
    assert panel.shutdown_complete


def test_shutdown_waits_for_done_worker_actual_thread_unwinding(panel, app, monkeypatch):
    release, function_done = threading.Event(), threading.Event()
    original = threading.Thread.run
    def delayed_thread_exit(thread):
        original(thread)
        if thread.name == "JarvisRemoteSetup":
            function_done.set()
            assert release.wait(2)
    monkeypatch.setattr(threading.Thread, "run", delayed_thread_exit)
    panel.refresh_button.click()
    try:
        assert function_done.wait(1)
        panel.shutdown()
        assert not panel.shutdown_complete
        assert next(iter(panel._worker_threads.values()))[1].is_set()
    finally:
        release.set()
    settled(app, panel, condition=lambda: panel.shutdown_complete)


def test_thread_start_failure_restores_controls_and_does_not_block_shutdown(panel, monkeypatch):
    def fail(_thread):
        raise RuntimeError("private-thread-error")
    monkeypatch.setattr(threading.Thread, "start", fail)
    panel.refresh_button.click()
    assert not panel.busy and panel.refresh_button.isEnabled()
    assert "private-thread-error" not in panel.status_label.text()
    panel.shutdown()
    assert panel.shutdown_complete and panel._worker_threads == {}


@pytest.mark.parametrize("handler,backend", [("refresh", "status"), ("_configure", "configure_serve"),
                                           ("_download", "download_installer"), ("_open_installer", "verified_installer")])
def test_pending_worker_does_not_retain_widget_and_cannot_emit_after_native_deletion(app, tmp_path, handler, backend):
    entered, release = threading.Event(), threading.Event()
    core = SimpleNamespace(owner=FakeOwner(), store=SimpleNamespace(state_dir=tmp_path / "state"))
    setup = FakeSetup(core.store.state_dir)
    def operation(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return ready()
    setattr(setup, backend, operation)
    widget = module.RemoteSetupPanel(core, setup=setup)
    owner_verified(widget)
    getattr(widget, handler)()
    assert entered.wait(1)
    widget.shutdown()
    thread = next(iter(widget._worker_threads.values()))[0]
    reference = weakref.ref(widget)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    del widget
    release.set()
    thread.join(2)
    app.processEvents()
    assert not thread.is_alive()
    # Qt signal connections may retain the Python wrapper until collection;
    # the destroyed bridge must nevertheless remain unusable and uncalled.
    import gc
    gc.collect()
    assert reference() is None


def test_repeated_create_refresh_shutdown_disposal_has_no_late_ui_callbacks(app, tmp_path):
    for index in range(8):
        core = SimpleNamespace(owner=FakeOwner(), store=SimpleNamespace(state_dir=tmp_path / str(index)))
        widget = module.RemoteSetupPanel(core, setup=FakeSetup(core.store.state_dir))
        widget.refresh()
        settled(app, widget)
        widget.shutdown()
        settled(app, widget, condition=lambda: widget.shutdown_complete)
        core.owner.closed = True
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
