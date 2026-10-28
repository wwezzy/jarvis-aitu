"""Offscreen controls use fake consent/camera/actions, never real devices."""
import os
import struct
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop UI dependency")
from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from desktop import control_panel as module  # noqa: E402
from desktop.app_catalog import AppCatalog  # noqa: E402
from desktop.core import Reply  # noqa: E402
from desktop.project_catalog import ProjectCatalog  # noqa: E402


class FakeOwner:
    def __init__(self):
        self.enabled, self.verified, self.remaining = True, False, 300
        self.verifications, self.revocations = [], 0
        self.verify_entered = threading.Event()
        self.verify_release = None

    def allowed(self):
        return self.verified if self.enabled else True

    def status(self):
        return {"enabled": self.enabled, "verified": self.enabled and self.allowed(), "remaining_seconds": self.remaining}

    def verify(self, hwnd, cancel=None):
        self.verifications.append((hwnd, threading.get_ident()))
        self.verify_entered.set()
        if self.verify_release is not None:
            while not self.verify_release.wait(0.01):
                if cancel.is_set():
                    return SimpleNamespace(success=False, message="Проверка отменена.")
        self.verified = not cancel.is_set()
        return SimpleNamespace(success=self.verified, message="Владелец подтверждён." if self.verified else "Проверка отменена.")

    def revoke(self):
        self.verified = False
        self.revocations += 1


class FakeStore:
    def __init__(self):
        self.values = {"gestures_enabled": True, "voice_owner_required": True}

    def set_preferences(self, updates):
        self.values.update(updates)
        return dict(self.values)


class FakeCore:
    def __init__(self, state):
        self.projects = ProjectCatalog(state)
        self.apps = AppCatalog(state, inspector=lambda _: "Useful.exe")
        self.owner, self.store = FakeOwner(), FakeStore()
        self.commands, self.controls, self.remote = [], [], False
        self.catalog_requests = []

    def handle(self, text, source="typed"):
        self.commands.append((text, source, threading.get_ident()))
        return Reply("Запрос обработан.")

    def open_catalog_item(self, kind, record_id, *, source="button"):
        if self.owner.enabled and not self.owner.allowed():
            return Reply("Подтверди владельца через Windows Hello.")
        self.catalog_requests.append((kind, record_id, source, threading.get_ident()))
        return Reply("Запрос обработан.")

    def request_control(self, action, source="gesture"):
        if self.owner.enabled and self.owner.allowed():
            self.controls.append((action, source))
        return Reply("Жест обработан.")

    def set_owner_required(self, enabled):
        if not enabled and self.owner.enabled and not self.owner.allowed():
            return False
        self.owner.enabled = enabled
        self.store.values["voice_owner_required"] = enabled
        self.owner.revoke()
        return True

    def approve_remote(self, enabled):
        if enabled and not (self.owner.enabled and self.owner.allowed()):
            return False
        self.remote = enabled
        return True


class FakeGesture:
    instances = []
    stop_result = True

    def __init__(self, on_intent, on_state, *, owner_gate):
        self.on_intent, self.on_state, self.owner_gate = on_intent, on_state, owner_gate
        self.starts = self.stops = 0
        self.instances.append(self)

    def start(self):
        self.starts += 1
        if not self.owner_gate(None):
            return False
        self.on_state("Камера включена.")
        return True

    def stop(self):
        self.stops += 1
        if self.stop_result:
            self.on_state("Камера выключена.")
        return self.stop_result


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_camera(monkeypatch):
    FakeGesture.instances = []
    FakeGesture.stop_result = True
    monkeypatch.setattr(module, "GestureService", FakeGesture)


@pytest.fixture
def panel(app, tmp_path):
    core = FakeCore(tmp_path / "state")
    replies = []
    gui_thread = threading.get_ident()
    def hwnd():
        assert threading.get_ident() == gui_thread
        return 12345
    widget = module.ControlsPanel(core, main_window=SimpleNamespace(winId=hwnd), on_reply=replies.append)
    widget.test_replies = replies
    widget.show()
    settled(app, widget)
    yield widget
    widget.shutdown()
    settled(app, widget, condition=lambda: widget.shutdown_complete)
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def settled(app, panel, *, condition=None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if not panel.busy and (condition is None or condition()):
            app.processEvents()
            return
        time.sleep(0.005)
    raise AssertionError("Controls worker did not settle")


def enable_owner(panel):
    panel.core.owner.verified = True
    panel._poll_owner()


def make_project(path):
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text("untrusted project data", encoding="utf-8")
    return path


def make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    struct.pack_into("<4sHHIIIHH", data, 128, b"PE\x00\x00", 0x8664, 1, 0, 0, 0, 240, 2)
    struct.pack_into("<H", data, 152, 0x20B)
    path.write_bytes(data)
    return path


def test_constructor_and_refresh_never_activate_camera_or_consent(panel, app):
    panel.refresh()
    settled(app, panel)
    assert not panel.gesture_toggle.isChecked()
    assert panel.core.store.values["gestures_enabled"] is False
    assert FakeGesture.instances == [] and panel.core.owner.verifications == []
    assert not panel.app_add.isEnabled() and not panel.remote_approve.isEnabled()
    assert panel.core.commands == panel.core.controls == []


def test_root_picker_scan_editor_save_and_open_use_selected_id_core_route(panel, app, tmp_path, monkeypatch):
    root = tmp_path / "Projects"
    make_project(root / "Jarvis")
    monkeypatch.setattr(module.QFileDialog, "getExistingDirectory", lambda *_a, **_kw: str(root))
    panel.root_picker.click()
    settled(app, panel)
    assert panel.root_combo.count() == 1
    panel.root_scan.click()
    settled(app, panel)
    assert panel.project_combo.count() == 1
    panel.editor_combo.setCurrentIndex(panel.editor_combo.findData("pycharm"))
    settled(app, panel)
    assert panel.core.projects.projects()[0]["editor"] == "pycharm"
    enable_owner(panel)
    panel.project_open.click()
    settled(app, panel)
    kind, record_id, source, worker_thread = panel.core.catalog_requests[-1]
    assert (kind, record_id, source) == ("project", panel.project_combo.currentData()["id"], "button")
    assert panel.core.commands == []
    assert worker_thread != threading.get_ident() and panel.test_replies[-1].text == "Запрос обработан."


def test_manual_project_alias_and_catalog_removal_preserve_files(panel, app, tmp_path, monkeypatch):
    root = tmp_path / "Projects"
    folder = make_project(root / "Saved")
    panel.core.projects.register_root(root)
    monkeypatch.setattr(module.QFileDialog, "getExistingDirectory", lambda *_a, **_kw: str(folder))
    panel.project_add.click()
    settled(app, panel)
    monkeypatch.setattr(module.QInputDialog, "getMultiLineText", lambda *_a, **_kw: ("Мой помощник", True))
    panel.project_alias.click()
    settled(app, panel)
    assert panel.core.projects.resolve("Мой помощник")["status"] == "found"
    panel.project_remove.click()
    settled(app, panel)
    assert panel.core.projects.projects() == [] and (folder / "pyproject.toml").is_file()
    panel.root_remove.click()
    settled(app, panel)
    assert panel.core.projects.roots() == [] and folder.is_dir()


def test_project_selection_loads_editor_without_accidental_write(panel, app, tmp_path):
    root = tmp_path / "Projects"
    folder = make_project(root / "Saved")
    panel.core.projects.register_root(root)
    saved = panel.core.projects.add_project(folder)
    panel.core.projects.set_editor(saved["id"], "idea")
    panel.refresh()
    settled(app, panel)
    assert panel.editor_combo.currentData() == "idea"
    assert not panel.core.commands


def test_app_addition_requires_enabled_verified_owner_not_allowed_when_policy_off(panel, monkeypatch):
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *_a, **_kw: pytest.fail("Unverified picker opened"))
    panel._pick_app()
    panel.core.owner.enabled = False  # Owner.allowed() deliberately returns True then.
    panel._pick_app()
    assert panel.core.apps.apps() == []


def test_explicit_app_picker_registration_open_and_remove(panel, app, tmp_path, monkeypatch):
    enable_owner(panel)
    path = make_exe(tmp_path / "Useful.exe")
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *_a, **_kw: (str(path), ""))
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *_a, **_kw: ("Редактор", True))
    panel.app_add.click()
    settled(app, panel)
    assert panel.core.apps.apps()[0]["name"] == "Редактор"
    panel.app_open.click()
    settled(app, panel)
    assert panel.core.catalog_requests[-1][:3] == ("app", panel.app_combo.currentData()["id"], "button")
    assert panel.core.commands == []
    panel.app_remove.click()
    settled(app, panel)
    assert panel.core.apps.apps() == [] and path.is_file()


def saved_project(panel, tmp_path, name="Saved"):
    root = tmp_path / "Projects"
    folder = make_project(root / name)
    panel.core.projects.register_root(root)
    return panel.core.projects.add_project(folder)


def saved_app(panel, tmp_path):
    path = make_exe(tmp_path / "Useful.exe")
    return panel.core.apps.register_app(path, "Редактор", approval=True)


def test_project_name_and_alias_editor_replace_and_clear_without_renaming_files(panel, app, tmp_path, monkeypatch):
    record = saved_project(panel, tmp_path)
    panel.core.projects.set_aliases(record["id"], ["Старое имя"])
    panel.refresh()
    settled(app, panel)
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *_a, **kw: ("Учебный проект", True))
    panel.project_name.click()
    settled(app, panel)
    assert panel.project_combo.currentData()["id"] == record["id"]
    assert panel.project_combo.currentText() == "Учебный проект"
    assert panel.core.projects.resolve("Учебный проект")["status"] == "found"
    prefilled = []
    def aliases(*args, **_kwargs):
        prefilled.append(args[3])
        return "Помощник\n  Джарвис  ", True
    monkeypatch.setattr(module.QInputDialog, "getMultiLineText", aliases)
    panel.project_alias.click()
    settled(app, panel)
    assert prefilled == ["Старое имя"]
    current = panel.core.projects.projects()[0]
    assert set(current["aliases"]) == {"Помощник", "Джарвис"}
    assert panel.core.projects.resolve("Старое имя")["status"] == "missing"
    monkeypatch.setattr(module.QInputDialog, "getMultiLineText", lambda *_a, **_kw: ("\n  ", True))
    panel.project_alias.click()
    settled(app, panel)
    assert panel.core.projects.projects()[0]["aliases"] == []
    assert (tmp_path / "Projects" / "Saved" / "pyproject.toml").is_file()
    assert not panel.core.catalog_requests and not panel.core.commands


def test_cancelled_or_blank_name_and_cancelled_alias_edit_preserve_record(panel, app, tmp_path, monkeypatch):
    record = saved_project(panel, tmp_path)
    panel.refresh()
    settled(app, panel)
    for response in (("Changed", False), ("   ", True)):
        monkeypatch.setattr(module.QInputDialog, "getText", lambda *_a, result=response, **_kw: result)
        panel.project_name.click()
        settled(app, panel)
        assert panel.core.projects.projects()[0]["name"] == record["name"]
    monkeypatch.setattr(module.QInputDialog, "getMultiLineText", lambda *_a, **_kw: ("Changed", False))
    panel.project_alias.click()
    settled(app, panel)
    assert panel.core.projects.projects()[0]["aliases"] == []


def test_alias_editor_reports_limit_and_invalid_names_without_partial_writes(panel, app, tmp_path, monkeypatch):
    record = saved_project(panel, tmp_path)
    panel.core.projects.set_aliases(record["id"], ["Сохранённое"])
    panel.refresh()
    settled(app, panel)
    for text in ("\n".join(f"Имя {i}" for i in range(11)), "<img src=private-token>"):
        monkeypatch.setattr(module.QInputDialog, "getMultiLineText", lambda *_a, result=text, **_kw: (result, True))
        panel.project_alias.click()
        settled(app, panel)
        assert panel.core.projects.projects()[0]["aliases"] == ["Сохранённое"]
        assert "private-token" not in panel.status.text()
    assert "80" in panel.status.text()


def test_app_name_and_alias_edits_preserve_approval_and_require_current_owner(panel, app, tmp_path, monkeypatch):
    record = saved_app(panel, tmp_path)
    panel.refresh()
    settled(app, panel)
    assert not panel.app_name.isEnabled() and not panel.app_alias.isEnabled()
    enable_owner(panel)
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *_a, **_kw: ("Мой редактор", True))
    panel.app_name.click()
    settled(app, panel)
    monkeypatch.setattr(module.QInputDialog, "getMultiLineText", lambda *_a, **_kw: ("Код\nРедактор кода", True))
    panel.app_alias.click()
    settled(app, panel)
    current = panel.core.apps.apps()[0]
    assert (current["id"], current["path"], current["sha256"], current["approved_at"]) == tuple(record[key] for key in ("id", "path", "sha256", "approved_at"))
    assert current["name"] == "Мой редактор" and set(current["aliases"]) == {"Код", "Редактор кода"}
    assert panel.core.apps.resolve("Мой редактор")["status"] == "found"
    def expired(*_a, **_kw):
        panel.core.owner.verified = False
        return "Нельзя сохранить", True
    monkeypatch.setattr(module.QInputDialog, "getMultiLineText", expired)
    panel.app_alias.click()
    settled(app, panel)
    assert panel.core.apps.apps()[0]["aliases"] == current["aliases"]
    assert not panel.core.catalog_requests


@pytest.mark.parametrize("operation", ["rename", "set_aliases"])
def test_app_metadata_edit_passes_live_guard_for_late_owner_revocation(panel, app, tmp_path, monkeypatch, operation):
    saved_app(panel, tmp_path)
    before = panel.core.apps.apps()[0]
    panel.refresh()
    settled(app, panel)
    enable_owner(panel)
    original = getattr(panel.core.apps, operation)
    guards, errors = [], []
    def revoke_before_write(record_id, value, *, approval_guard):
        guards.append(approval_guard)
        assert approval_guard() is True
        # A queued/blocked catalog operation must consult the current owner,
        # rather than preserve the truth value captured at button click.
        panel.core.owner.verified = False
        assert approval_guard() is False
        try:
            return original(record_id, value, approval_guard=approval_guard)
        except Exception as error:
            errors.append(type(error))
            raise
    monkeypatch.setattr(panel.core.apps, operation, revoke_before_write)
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *_a, **_kw: ("Новое имя", True))
    monkeypatch.setattr(module.QInputDialog, "getMultiLineText", lambda *_a, **_kw: ("Новый псевдоним", True))
    (panel.app_name if operation == "rename" else panel.app_alias).click()
    settled(app, panel)
    assert len(guards) == 1 and callable(guards[0])
    assert errors == [PermissionError]
    assert panel.core.apps.apps()[0] == before
    assert "Проверь подтверждение владельца" in panel.status.text()
    assert panel.core.catalog_requests == []


def test_duplicate_project_names_open_the_selected_id_without_text_resolution(panel, app, tmp_path):
    root = tmp_path / "Projects"
    first, second = make_project(root / "one" / "Same"), make_project(root / "two" / "Same")
    panel.core.projects.register_root(root)
    a, b = panel.core.projects.add_project(first), panel.core.projects.add_project(second)
    assert panel.core.projects.resolve("Same")["status"] == "ambiguous"
    panel.refresh()
    settled(app, panel)
    assert panel.project_combo.itemText(0) != panel.project_combo.itemText(1)
    enable_owner(panel)
    panel.project_combo.setCurrentIndex(1)
    panel.project_open.click()
    settled(app, panel)
    assert panel.core.catalog_requests[-1][:3] == ("project", b["id"], "button")
    assert a["id"] != b["id"] and panel.core.commands == []


def test_duplicate_app_names_open_selected_id_and_refresh_keeps_selection(panel, app, tmp_path):
    first = panel.core.apps.register_app(make_exe(tmp_path / "One.exe"), "Редактор", approval=True)
    second = panel.core.apps.register_app(make_exe(tmp_path / "Two.exe"), "Редактор", approval=True)
    panel.refresh()
    settled(app, panel)
    enable_owner(panel)
    panel.app_combo.setCurrentIndex(1)
    panel.refresh()
    settled(app, panel)
    assert panel.app_combo.currentData()["id"] == second["id"]
    assert panel.app_combo.itemText(0) != panel.app_combo.itemText(1)
    panel.app_open.click()
    settled(app, panel)
    assert panel.core.catalog_requests[-1][:3] == ("app", second["id"], "button")
    assert first["id"] != second["id"] and panel.core.commands == []


def test_selected_project_id_is_captured_before_worker_and_later_selection_changes(panel, app, tmp_path, monkeypatch):
    root = tmp_path / "Projects"
    first, second = make_project(root / "First"), make_project(root / "Second")
    panel.core.projects.register_root(root)
    record = panel.core.projects.add_project(first)
    panel.core.projects.add_project(second)
    panel.refresh()
    settled(app, panel)
    enable_owner(panel)
    entered, release, requests = threading.Event(), threading.Event(), []
    def open_item(kind, record_id, *, source):
        entered.set()
        assert release.wait(2)
        requests.append((kind, record_id, source))
        return Reply("Выбранный проект обработан.")
    monkeypatch.setattr(panel.core, "open_catalog_item", open_item)
    panel.project_open.click()
    try:
        assert entered.wait(1)
        panel.project_combo.setCurrentIndex(1)
    finally:
        release.set()
    settled(app, panel)
    assert requests == [("project", record["id"], "button")]
    assert panel.project_combo.currentData()["id"] != record["id"]


def test_owner_revocation_before_selected_open_is_checked_in_core(panel, app, tmp_path):
    saved_project(panel, tmp_path)
    panel.refresh()
    settled(app, panel)
    enable_owner(panel)
    panel.core.owner.verified = False
    panel.project_open.click()
    settled(app, panel)
    assert panel.core.catalog_requests == [] and panel.core.commands == []
    assert "Подтверди владельца" in panel.status.text()


def test_project_scan_reports_zero_and_bounded_truncation_with_fixed_errors(panel, app, tmp_path, monkeypatch):
    root = tmp_path / "Projects"
    root.mkdir()
    panel.core.projects.register_root(root)
    panel.refresh()
    settled(app, panel)
    calls = []
    def scan(root_id, **kwargs):
        calls.append((root_id, kwargs))
        return {"projects": [], "truncated": False}
    monkeypatch.setattr(panel.core.projects, "scan", scan)
    panel.root_scan.click()
    assert "Ищу" in panel.status.text()
    settled(app, panel)
    assert "Проекты не найдены" in panel.status.text()
    assert calls[0][1] == {"max_depth": 2, "limit": 200}
    monkeypatch.setattr(panel.core.projects, "scan", lambda *_a, **_kw: {"projects": [object()], "truncated": True})
    panel.root_scan.click()
    settled(app, panel)
    assert "1" in panel.status.text() and "предел" in panel.status.text()
    def failed(*_a, **_kw):
        raise OSError("C:/private/token-value")
    monkeypatch.setattr(panel.core.projects, "scan", failed)
    panel.root_scan.click()
    settled(app, panel)
    assert "Не удалось найти проекты" in panel.status.text() and "token-value" not in panel.status.text()


def test_same_named_root_folders_show_selected_path_and_scan_selected_id(panel, app, tmp_path, monkeypatch):
    first, second = tmp_path / "one" / "Projects", tmp_path / "two" / "Projects"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    a, b = panel.core.projects.register_root(first), panel.core.projects.register_root(second)
    panel.refresh()
    settled(app, panel)
    assert panel.root_combo.itemText(0) != panel.root_combo.itemText(1)
    panel.root_combo.setCurrentIndex(1)
    assert str(second) in panel.root_details.text() and panel.root_details.textFormat() == Qt.TextFormat.PlainText
    scanned = []
    def scan(root_id, **_kwargs):
        scanned.append(root_id)
        return {"projects": [], "truncated": False}
    monkeypatch.setattr(panel.core.projects, "scan", scan)
    panel.root_scan.click()
    settled(app, panel)
    assert scanned == [b["id"]] and a["id"] != b["id"]


def test_missing_project_folder_is_shown_and_refresh_button_recovers_status(panel, app, tmp_path):
    saved_project(panel, tmp_path)
    folder = tmp_path / "Projects" / "Saved"
    moved = folder.with_name("Moved")
    folder.rename(moved)
    panel.refresh()
    settled(app, panel)
    assert "недоступна" in panel.project_details.text()
    moved.rename(folder)
    panel.catalog_refresh.click()
    settled(app, panel)
    assert "Папка доступна" in panel.project_details.text()


def test_details_and_names_are_literal_and_do_not_make_registered_hash_claims(panel):
    unsafe = '<img src="https://private.test/token">'
    panel._apply_snapshot(([], [{"id": 7, "name": unsafe, "path": unsafe, "aliases": [unsafe], "editor": "explorer", "available": False}],
                           [{"id": 9, "name": unsafe, "path": unsafe, "aliases": [unsafe]}], None))
    assert panel.project_combo.currentText() == panel.app_combo.currentText() == unsafe
    for label in (panel.project_details, panel.app_details):
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert unsafe in label.text()
    assert "проверяется при каждом запуске" in panel.app_details.text()


def test_editor_dispatch_is_finite_even_if_combo_data_is_injected(panel, app, tmp_path, monkeypatch):
    saved_project(panel, tmp_path)
    panel.refresh()
    settled(app, panel)
    monkeypatch.setattr(panel.core.projects, "set_editor", lambda *_a, **_kw: pytest.fail("Unrecognized editor dispatched"))
    panel.editor_combo.addItem("Не разрешён", "powershell")
    panel.editor_combo.setCurrentIndex(panel.editor_combo.count() - 1)
    assert not panel.busy and panel.core.projects.projects()[0]["editor"] == "explorer"


def test_updated_app_needs_explicit_reapproval_and_never_launches_in_settings(panel, app, tmp_path, monkeypatch):
    record = saved_app(panel, tmp_path)
    path = tmp_path / "Useful.exe"
    path.write_bytes(path.read_bytes() + b"updated")
    launches = []
    assert panel.core.apps.open_app("Редактор", executor=launches.append)["status"] == "changed"
    panel.refresh()
    settled(app, panel)
    assert not panel.app_reapprove.isEnabled()
    enable_owner(panel)
    confirmations = []
    monkeypatch.setattr(panel, "_confirm_reapproval", lambda app: confirmations.append(app["id"]) or True)
    panel.app_reapprove.click()
    settled(app, panel)
    current = panel.core.apps.apps()[0]
    assert confirmations == [record["id"]]
    assert current["id"] == record["id"] and current["aliases"] == record["aliases"]
    assert current["sha256"] != record["sha256"] and current["size"] > record["size"]
    assert launches == [] and panel.core.catalog_requests == []
    assert "не запускалось" in panel.status.text()


def test_cancelled_reapproval_and_owner_expiry_preserve_previous_hash(panel, app, tmp_path, monkeypatch):
    record = saved_app(panel, tmp_path)
    path = tmp_path / "Useful.exe"
    path.write_bytes(path.read_bytes() + b"updated")
    panel.refresh()
    settled(app, panel)
    enable_owner(panel)
    monkeypatch.setattr(panel, "_confirm_reapproval", lambda _app: False)
    panel.app_reapprove.click()
    settled(app, panel)
    assert panel.core.apps.apps()[0]["sha256"] == record["sha256"] and "отменено" in panel.status.text()
    def expired(_app):
        panel.core.owner.verified = False
        return True
    monkeypatch.setattr(panel, "_confirm_reapproval", expired)
    panel.app_reapprove.click()
    settled(app, panel)
    assert panel.core.apps.apps()[0]["sha256"] == record["sha256"]
    assert "подтвердить файл" in panel.status.text()


def test_reapproval_rechecks_owner_after_file_inspection_and_rolls_back(panel, app, tmp_path, monkeypatch):
    record = saved_app(panel, tmp_path)
    path = tmp_path / "Useful.exe"
    path.write_bytes(path.read_bytes() + b"updated")
    panel.refresh()
    settled(app, panel)
    enable_owner(panel)
    monkeypatch.setattr(panel, "_confirm_reapproval", lambda _app: True)
    inspected = []
    def inspector(_path):
        inspected.append(True)
        panel.core.owner.verified = False
        return "Useful.exe"
    panel.core.apps.inspector = inspector
    panel.app_reapprove.click()
    settled(app, panel)
    assert inspected == [True]
    assert panel.core.apps.apps()[0]["sha256"] == record["sha256"]
    assert panel.core.catalog_requests == []


def test_reapproval_requires_policy_enabled_even_if_owner_allowed_returns_true(panel, app, tmp_path, monkeypatch):
    saved_app(panel, tmp_path)
    panel.refresh()
    settled(app, panel)
    panel.core.owner.enabled = False
    monkeypatch.setattr(panel, "_confirm_reapproval", lambda _app: pytest.fail("Approval offered without owner policy"))
    panel._reapprove_app()
    assert "подтверди владельца" in panel.status.text()


def test_reapproval_cannot_recreate_deleted_record_or_start_a_program(panel, app, tmp_path, monkeypatch):
    record = saved_app(panel, tmp_path)
    panel.refresh()
    settled(app, panel)
    enable_owner(panel)
    def removed(_app):
        panel.core.apps.remove(record["id"])
        return True
    monkeypatch.setattr(panel, "_confirm_reapproval", removed)
    panel.app_reapprove.click()
    settled(app, panel)
    assert panel.core.apps.apps() == [] and panel.core.catalog_requests == []
    assert "Не удалось подтвердить файл" in panel.status.text()


def test_reapproval_dialog_displays_literal_data_and_defaults_to_cancel(panel, monkeypatch):
    dialogs = []
    def execute(dialog):
        dialogs.append(dialog)
        assert dialog.textFormat() == Qt.TextFormat.PlainText
        assert dialog.defaultButton() == dialog.button(module.QMessageBox.StandardButton.Cancel)
        assert '<img src="private">' in dialog.text()
        return module.QMessageBox.StandardButton.Cancel
    monkeypatch.setattr(module.QMessageBox, "exec", execute)
    assert panel._confirm_reapproval({"name": '<img src="private">', "path": "C:/local/Useful.exe"}) is False
    assert len(dialogs) == 1


def test_owner_expiring_during_dialog_cannot_approve_app(panel, app, tmp_path, monkeypatch):
    enable_owner(panel)
    path = make_exe(tmp_path / "Useful.exe")
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *_a, **_kw: (str(path), ""))
    def alias(*_a, **_kw):
        panel.core.owner.verified = False
        return "Редактор", True
    monkeypatch.setattr(module.QInputDialog, "getText", alias)
    panel.app_add.click()
    settled(app, panel)
    assert panel.core.apps.apps() == [] and "Не удалось" in panel.status.text()


def test_shutdown_during_app_file_inspection_rolls_back_approval(panel, app, tmp_path, monkeypatch):
    enable_owner(panel)
    path = make_exe(tmp_path / "Useful.exe")
    entered, released = threading.Event(), threading.Event()
    def inspect(_):
        entered.set()
        assert released.wait(2)
        return "Useful.exe"
    panel.core.apps.inspector = inspect
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *_a, **_kw: (str(path), ""))
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *_a, **_kw: ("Редактор", True))
    panel.app_add.click()
    assert entered.wait(1)
    panel.shutdown()
    released.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert panel.core.apps.apps() == [] and path.is_file()


def test_disabling_owner_policy_is_guarded_and_reverted_when_not_verified(panel, app):
    assert panel.owner_toggle.isChecked()
    panel.owner_toggle.setChecked(False)
    settled(app, panel)
    assert panel.owner_toggle.isChecked() and panel.core.owner.enabled
    assert panel.core.store.values["voice_owner_required"] is True
    assert "подтверди владельца" in panel.status.text()


def test_only_explicit_verify_button_prompts_with_gui_obtained_hwnd(panel, app):
    assert panel.core.owner.verifications == []
    panel.owner_verify.click()
    settled(app, panel)
    hwnd, worker_thread = panel.core.owner.verifications[-1]
    assert hwnd == 12345 and worker_thread != threading.get_ident()
    assert panel.core.owner.verified and "300" in panel.owner_status.text()
    assert panel.app_add.isEnabled()


def test_close_cancels_pending_verification_and_ignores_late_result(panel, app):
    owner = panel.core.owner
    owner.verify_release = threading.Event()
    panel.owner_verify.click()
    assert owner.verify_entered.wait(1)
    panel.shutdown()
    before = panel.status.text()
    owner.verify_release.set()
    time.sleep(0.03)
    app.processEvents()
    assert not owner.verified and owner.revocations >= 1
    assert panel.status.text() == before and not panel.owner_timer.isActive()


def test_gestures_require_explicit_owner_then_finite_rechecked_intents(panel, app):
    enable_owner(panel)
    panel.gesture_toggle.setChecked(True)
    settled(app, panel)
    service = FakeGesture.instances[-1]
    assert service.starts == 1 and service.owner_gate(None)
    assert panel.core.store.values["gestures_enabled"] is True
    service.on_intent("volume_up")
    settled(app, panel, condition=lambda: len(panel.core.controls) == 1)
    assert panel.core.controls == [("volume_up", "gesture")]
    service.on_intent("shutdown")
    app.processEvents()
    assert len(panel.core.controls) == 1
    panel.core.owner.verified = False
    service.on_intent("volume_down")
    app.processEvents()
    assert len(panel.core.controls) == 1
    panel._poll_owner()
    settled(app, panel, condition=lambda: service.stops > 0 and not panel._stopping)
    assert not panel.gesture_toggle.isChecked() and not service.owner_gate(None)


def test_stop_blocks_queued_and_previous_generation_gestures(panel, app):
    enable_owner(panel)
    panel.gesture_toggle.setChecked(True)
    settled(app, panel)
    old = FakeGesture.instances[-1]
    old.on_intent("minimize")  # Queued, not yet handled by GUI.
    panel.gesture_toggle.setChecked(False)
    settled(app, panel, condition=lambda: not panel._stopping)
    assert panel.core.controls == [] and panel.core.store.values["gestures_enabled"] is False
    panel.gesture_toggle.setChecked(True)
    settled(app, panel)
    old.on_intent("volume_up")
    old.on_state("Камера включена.")
    app.processEvents()
    assert panel.core.controls == []


def test_delayed_camera_stop_is_reported_honestly_until_completion(panel, app):
    enable_owner(panel)
    FakeGesture.stop_result = False
    panel.gesture_toggle.setChecked(True)
    settled(app, panel)
    service = FakeGesture.instances[-1]
    panel.gesture_toggle.setChecked(False)
    settled(app, panel, condition=lambda: service.stops > 0)
    assert "останавливается" in panel.gesture_status.text() and not panel.gesture_toggle.isChecked()
    assert not panel.gesture_toggle.isEnabled()
    service.on_state("Камера выключена.")
    settled(app, panel, condition=lambda: not panel._stopping)
    assert panel.gesture_status.text() == "Камера выключена." and panel.gesture_toggle.isEnabled()


def test_shutdown_stops_gestures_revokes_remote_and_blocks_future_dispatch(panel, app):
    enable_owner(panel)
    panel.gesture_toggle.setChecked(True)
    settled(app, panel)
    service = FakeGesture.instances[-1]
    panel.core.remote = True
    panel.shutdown()
    service.on_intent("volume_up")
    service.on_state("Камера включена.")
    app.processEvents()
    assert not panel.core.remote and not panel.core.owner.verified
    assert panel.core.controls == [] and not panel.core.store.values["gestures_enabled"]
    assert not service.owner_gate(None)


def test_remote_rendering_uses_status_allowlist_and_no_secret_payload(panel):
    panel._remote_report({"status": "ready", "approved": True, "url": "https://secret.test/private?token=secret"})
    assert "готово" in panel.remote_status_label.text() and "выдано" in panel.remote_status_label.text()
    assert "secret" not in panel.remote_status_label.text()
    panel._remote_report({"status": {"token": "secret"}, "error": "secret"})
    assert "secret" not in panel.remote_status_label.text()
    assert panel.remote_status_label.textFormat() == Qt.TextFormat.PlainText


def test_remote_approval_requires_owner_and_revoke_does_not(panel, app):
    panel._approve_remote()
    assert not panel.core.remote
    enable_owner(panel)
    panel.remote_approve.click()
    settled(app, panel)
    assert panel.core.remote
    panel.core.owner.verified = False
    panel.remote_revoke.click()
    settled(app, panel)
    assert not panel.core.remote


def test_remote_status_failure_does_not_break_other_catalog_refresh(panel, app):
    def failure():
        raise RuntimeError("do not display private endpoint/token")
    panel.remote_status = failure
    panel.refresh()
    settled(app, panel)
    assert "недоступно" in panel.remote_status_label.text()
    assert "token" not in panel.remote_status_label.text()


def test_callback_and_error_text_are_plain_and_do_not_expose_adapter_errors(panel, app):
    panel._reply(Reply('<img src="https://example.test/leak">'))
    assert panel.status.textFormat() == Qt.TextFormat.PlainText
    def failure():
        raise ValueError("C:/private/secrets/owner-token")
    panel._run(failure, refresh=False)
    settled(app, panel)
    assert "owner-token" not in panel.status.text() and "Не удалось" in panel.status.text()


def test_repeated_create_refresh_and_gui_thread_disposal_have_no_live_callbacks(app, tmp_path):
    for number in range(12):
        core = FakeCore(tmp_path / str(number))
        widget = module.ControlsPanel(core)
        settled(app, widget)
        assert widget._signals.parent() is widget
        widget.shutdown()
        settled(app, widget, condition=lambda: widget.shutdown_complete)
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not core.owner.verified and not core.remote


def test_shutdown_complete_waits_for_actual_control_thread_not_cleared_gui_jobs(panel, app):
    entered, released, returned = threading.Event(), threading.Event(), []
    def blocked_control():
        entered.set()
        assert released.wait(2)
        return Reply("Must not reach closed UI")
    assert panel.shutdown_complete is False
    panel._run(blocked_control, returned.append, refresh=False)
    assert entered.wait(1)
    panel.shutdown()
    assert not panel.busy and not panel.shutdown_complete
    assert not panel._run(lambda: pytest.fail("Work started after shutdown"))
    released.set()
    settled(app, panel, condition=lambda: panel.shutdown_complete)
    assert returned == [] and panel._worker_threads == {}


def test_shutdown_complete_waits_for_canceled_verification_thread_exit(panel, app):
    entered, released, canceled = threading.Event(), threading.Event(), []
    def verify(hwnd, cancel=None):
        entered.set()
        assert released.wait(2)
        canceled.append(cancel.is_set())
        return SimpleNamespace(success=False, message="Late verification result")
    panel.core.owner.verify = verify
    panel.owner_verify.click()
    assert entered.wait(1)
    panel.shutdown()
    before = panel.status.text()
    assert not panel.shutdown_complete
    released.set()
    settled(app, panel, condition=lambda: panel.shutdown_complete)
    assert canceled == [True] and not panel.core.owner.verified
    assert panel.status.text() == before


def test_shutdown_requests_camera_stop_without_waiting_but_reports_cleanup_thread(panel, app):
    enable_owner(panel)
    panel.gesture_toggle.setChecked(True)
    settled(app, panel)
    service = FakeGesture.instances[-1]
    entered, released = threading.Event(), threading.Event()
    def slow_stop():
        entered.set()
        assert released.wait(2)
        return True
    service.stop = slow_stop
    panel.shutdown()
    assert entered.wait(1)
    assert not panel.shutdown_complete and not service.owner_gate(None)
    released.set()
    settled(app, panel, condition=lambda: panel.shutdown_complete)
    assert not panel.core.store.values["gestures_enabled"]
