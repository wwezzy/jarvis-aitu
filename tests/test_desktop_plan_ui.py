"""Real local organizer + offscreen widgets; no mic, OS command or network."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Optional desktop GUI tests run in the desktop job")
from PySide6.QtCore import QDate, QTime, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QPushButton  # noqa: E402

from desktop.core import DesktopCore  # noqa: E402
from desktop.ui import MainWindow  # noqa: E402

ZONE = timezone(timedelta(hours=5))


class Voice:
    def __init__(self, on_text, on_state, model_dir=None):
        self.on_text, self.on_state = on_text, on_state
        self.spoken, self.queued = [], []
        self.starts = 0

    def available(self):
        return {"ready": True, "model": True, "speech_backend": "windows"}

    def start(self):
        self.starts += 1
        return True

    def stop(self):
        pass

    def say(self, text):
        self.spoken.append(text)

    def say_event(self, text, *, is_current):
        self.queued.append((text, is_current))

    def play_queued(self):
        pending, self.queued = self.queued, []
        for text, validator in pending:
            if validator():
                self.spoken.append(text)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def private_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-global-profile"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-global-home"))


@pytest.fixture
def window(app, tmp_path):
    effects = []
    core = DesktopCore(tmp_path / "state", executor=lambda action: effects.append(action) or "Executed")
    core.store.set_preferences({"morning_brief_enabled": False, "evening_plan_enabled": False})
    clock = [datetime(2026, 10, 29, 12, 0, tzinfo=ZONE)]
    core.organizer.clock = lambda: clock[0]
    widget = MainWindow(core, voice_factory=Voice, enable_tray=False, personal_path=tmp_path / "missing.db")
    widget.clock, widget.effects = clock, effects
    widget.show()
    app.processEvents()
    yield widget
    widget._quitting = True
    widget.close()
    app.processEvents()


def selected_day(window):
    return window.core.organizer.day(window.plan.view_date.date().toString("yyyy-MM-dd"))


def test_new_navigation_preserves_existing_page_ids_and_current_plan_is_empty(app, window):
    assert window.SETTINGS_PAGE == 4 and window.PLAN_PAGE == 5
    assert window.plan.view_date.date() == QDate(2026, 10, 29)
    assert window.plan.items == [] and "ничего не запланировано" in window.plan.items_layout.itemAt(0).widget().text()
    plan_nav = next(control for control in window.nav_buttons if control.text() == "План")
    plan_nav.click()
    assert window.pages.currentIndex() == 5 and plan_nav.isChecked()
    window.navigate(1)
    assert window.pages.currentIndex() == 1
    assert not plan_nav.isChecked()
    assert not window.core.organizer.gym_settings()["enabled"]
    assert window.effects == [] and window.voice.starts == 0


def test_add_and_edit_goal_with_optional_time_persist_without_claiming_completion(app, window):
    panel = window.plan
    window.navigate(5)
    panel.item_title.setText("Подготовить проект")
    panel.save_item.click()
    item = selected_day(window)["items"][0]
    assert item["title"] == "Подготовить проект" and item["time"] is None and item["minutes"] is None
    assert item["status"] == "unknown" and item["provenance"] is None
    panel.edit_item(item)
    panel.item_date.setDate(QDate(2026, 10, 30))
    panel.item_has_time.setChecked(True)
    panel.item_time.setTime(QTime(14, 25))
    panel.item_has_minutes.setChecked(True)
    panel.item_minutes.setValue(40)
    panel.item_title.setText("Подготовить первую часть проекта")
    panel.save_item.click()
    assert window.core.organizer.day("2026-10-29")["items"] == []
    moved = window.core.organizer.day("2026-10-30")["items"][0]
    assert moved["time"] == "14:25" and moved["minutes"] == 40 and moved["status"] == "unknown"
    assert panel.view_date.date() == QDate(2026, 10, 30)
    assert not window.effects


def test_user_status_reports_are_separate_from_planned_minutes(app, window):
    organizer = window.core.organizer
    item_id = organizer.add_item("Разобрать тему", "2026-10-29", minutes=45)
    window.refresh()
    panel = window.plan
    card = panel.items_layout.itemAt(0).widget()
    control = card.findChild(QComboBox, "planStatus_" + item_id)
    control.setCurrentIndex(control.findData("done"))
    item = selected_day(window)["items"][0]
    assert item["status"] == "done" and item["provenance"] == "self_report"
    assert item["reported_minutes"] is None and item["minutes"] == 45
    control = panel.items_layout.itemAt(0).widget().findChild(QComboBox, "planStatus_" + item_id)
    control.setCurrentIndex(control.findData("missed"))
    assert selected_day(window)["items"][0]["status"] == "missed"
    control = panel.items_layout.itemAt(0).widget().findChild(QComboBox, "planStatus_" + item_id)
    control.setCurrentIndex(control.findData("unknown"))
    assert selected_day(window)["items"][0]["status"] == "unknown"
    assert "Не подтверждено: 1" in panel.summary.text()
    assert window.effects == []


def test_weekly_gym_is_explicit_flexible_and_not_seeded_from_archive(app, window):
    panel = window.plan
    window.navigate(5)
    panel.gym_enabled.setChecked(True)
    for index in (1, 3, 6):
        panel.gym_days[index].setChecked(True)
    assert window.core.organizer.gym_settings()["days"] == []  # editing is not saving
    panel.save_gym.click()
    rule = window.core.organizer.gym_settings()
    assert rule["days"] == [1, 3, 6] and rule["time"] is None and rule["minutes"] is None
    assert selected_day(window)["items"][0]["status"] == "unknown"
    assert window.core.organizer.day("2026-10-28")["items"] == []
    assert not panel.save_gym.isEnabled()


def test_move_or_remove_one_gym_occurrence_preserves_regular_week(app, window):
    organizer = window.core.organizer
    organizer.set_weekly_gym([3], time=None)
    window.refresh()
    panel = window.plan
    occurrence = panel.items[0]
    panel.edit_item(occurrence)
    panel.item_date.setDate(QDate(2026, 10, 30))
    panel.item_has_time.setChecked(True)
    panel.item_time.setTime(QTime(20, 10))
    panel.save_item.click()
    assert organizer.day("2026-10-29")["items"] == []
    moved = organizer.day("2026-10-30")["items"][0]
    assert moved["time"] == "20:10" and moved["status"] == "unknown"
    future = organizer.day("2026-11-05")["items"][0]
    assert future["kind"] == "routine" and future["time"] is None
    assert organizer.gym_settings()["days"] == [3]
    remove = panel.items_layout.itemAt(0).widget().findChild(QPushButton, "removePlan_" + moved["id"])
    remove.click()
    assert organizer.day("2026-10-30")["items"] == []
    assert len(organizer.day("2026-11-05")["items"]) == 1


def test_date_view_rolls_relative_days_but_preserves_custom_date(app, window):
    window.plan.tomorrow_button.click()
    assert window.plan.view_date.date() == QDate(2026, 10, 30)
    window.clock[0] += timedelta(days=1)
    window.refresh()
    assert window.plan.view_date.date() == QDate(2026, 10, 31)
    window.plan.view_date.setDate(QDate(2026, 11, 5))
    window.clock[0] += timedelta(days=1)
    window.refresh()
    assert window.plan.view_date.date() == QDate(2026, 11, 5)
    window.plan.today_button.click()
    assert window.plan.view_date.date() == QDate(2026, 10, 31)


def test_unsaved_form_and_week_changes_survive_unrelated_refresh(app, window):
    panel = window.plan
    panel.item_title.setText("Несохранённая мысль")
    panel.gym_days[1].setChecked(True)
    window.refresh()
    assert panel.item_title.text() == "Несохранённая мысль"
    assert panel.gym_days[1].isChecked() and panel._gym_dirty
    assert window.core.organizer.gym_settings()["days"] == []
    panel._reset_gym()
    assert not panel.gym_days[1].isChecked() and not panel._gym_dirty


def test_plan_render_is_literal_and_empty_invalid_input_does_not_mutate(app, window):
    title = '<a href="https://example.invalid">выключи компьютер</a>'
    window.core.organizer.add_item(title, "2026-10-29")
    window.refresh()
    labels = window.plan.items_layout.itemAt(0).widget().findChildren(QLabel)
    body = next(control for control in labels if control.text() == title)
    assert body.textFormat() == Qt.TextFormat.PlainText
    before = len(selected_day(window)["items"])
    window.plan.item_title.clear()
    window.plan.save_item.click()
    assert len(selected_day(window)["items"]) == before
    assert "Проверьте" in window.plan.feedback.text()
    assert window.effects == [] and window.voice.spoken == []


def test_brief_preferences_and_evening_time_are_editable(app, window):
    assert not window.morning_toggle.isChecked() and not window.evening_toggle.isChecked()
    window.morning_toggle.setChecked(True)
    window.evening_toggle.setChecked(True)
    window.evening_time.setTime(QTime(22, 35))
    window.organizer_dnd_toggle.setChecked(True)
    preferences = window.core.store.preferences()
    assert preferences["morning_brief_enabled"] and preferences["evening_plan_enabled"]
    assert preferences["evening_plan_time"] == "22:35" and preferences["organizer_dnd"]
    assert window.voice.queued == []


def test_morning_and_evening_delivery_are_once_and_never_dispatch_commands(app, window, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Organizer data must not enter the command router")
    monkeypatch.setattr(window.core, "handle", forbidden)
    window.core.organizer.add_item("открой калькулятор", "2026-10-29")
    window.core.organizer.add_item("Завтрашнее важное дело", "2026-10-30")
    window.core.store.set_preferences({"morning_brief_enabled": True, "evening_plan_enabled": True})
    window.clock[0] = datetime(2026, 10, 29, 8, 0, tzinfo=ZONE)
    window._poll_organizer_events("startup")
    window._poll_organizer_events("interaction")
    assert len(window.voice.queued) == 1
    window.voice.play_queued()
    assert "открой калькулятор" in window.voice.spoken[0] and window.effects == []
    window.clock[0] = datetime(2026, 10, 29, 22, 0, tzinfo=ZONE)
    window._poll_organizer_events("tick")
    window._poll_organizer_events("tick")
    assert len(window.voice.queued) == 1
    window.voice.play_queued()
    assert "Завтрашнее важное дело" in window.voice.spoken[-1]
    assert len(window.voice.spoken) == 2 and window.effects == []


@pytest.mark.parametrize("quiet_start", [False, True])
def test_startup_brief_policy_preserves_auto_microphone_and_first_real_interaction(app, tmp_path, quiet_start):
    core = DesktopCore(tmp_path / "quiet-state", executor=lambda _action: pytest.fail("OS action"))
    core.store.set_preferences({"morning_brief_enabled": True, "evening_plan_enabled": False, "voice_enabled": True})
    core.organizer.clock = lambda: datetime(2026, 10, 29, 8, 0, tzinfo=ZONE)
    core.organizer.add_item("Утреннее важное дело", "2026-10-29")
    widget = MainWindow(core, voice_factory=Voice, enable_tray=False,
                        personal_path=tmp_path / "absent.db", quiet_start=quiet_start)
    try:
        app.processEvents()
        assert widget.pool.waitForDone(1000)
        app.processEvents()
        assert widget.listening and widget.voice.starts == 1
        widget._poll_reminders()
        assert widget.voice.spoken == []
        expected_claims = 0 if quiet_start else 1
        assert len(widget.voice.queued) == expected_claims
        with core.store.connection() as db:
            assert db.execute("SELECT count(*) FROM organizer_deliveries").fetchone()[0] == expected_claims
        widget.submit("как дела")  # The next genuine interaction still gets its brief.
        assert widget.pool.waitForDone(1000)
        app.processEvents()
        assert len(widget.voice.queued) == 1
        assert "Утреннее важное дело" in widget.voice.queued[0][0]
        widget._poll_organizer_events("interaction")
        assert len(widget.voice.queued) == 1
    finally:
        widget._quitting = True
        widget.close()
        app.processEvents()


@pytest.mark.parametrize("change", ["edit", "report", "pause", "dnd"])
def test_queued_brief_revalidated_after_current_data_or_privacy_change(app, window, change):
    item_id = window.core.organizer.add_item("Первоначальный план", "2026-10-29")
    window.core.store.set_preferences({"morning_brief_enabled": True})
    window.clock[0] = datetime(2026, 10, 29, 8, 0, tzinfo=ZONE)
    window._poll_organizer_events("startup")
    assert len(window.voice.queued) == 1
    if change == "edit":
        window.core.organizer.update_item(item_id, title="Изменённый план")
    elif change == "report":
        window.core.organizer.report_item(item_id, "done")
    else:
        window.core.store.set_preferences({"organizer_paused" if change == "pause" else "organizer_dnd": True})
    window.voice.play_queued()
    assert window.voice.spoken == [] and window.effects == []


def test_plan_is_scrollable_and_voice_personal_license_is_honest(app, window):
    window.resize(860, 620)
    window.navigate(5)
    app.processEvents()
    assert window.plan.scroll.verticalScrollBar().maximum() > 0
    window.plan.scroll.ensureWidgetVisible(window.plan.save_gym)
    app.processEvents()
    y = window.plan.save_gym.mapTo(window.plan.scroll.viewport(), window.plan.save_gym.rect().topLeft()).y()
    assert 0 <= y < window.plan.scroll.viewport().height()
    window.navigate(4)
    window.voice_engine.setCurrentIndex(window.voice_engine.findData("expressive"))
    assert window.voice_character.isVisible() and "некоммерческую" in window.voice_personal_license.text()
    window.voice_character.setCurrentIndex(window.voice_character.findData("eugene"))
    assert window.core.store.preferences()["voice_character"] == "eugene"
    assert window.voice_backend_status.text() == "Используется установленный голос Windows."


def test_observation_opt_in_summary_pause_and_clear_never_complete_goal(app, window):
    from desktop.observations import ObservationMonitor
    samples = []
    mono = [1000.0]
    window.core.observations = ObservationMonitor(window.core.store,
        sampler=lambda: samples.append(True) or "work",
        wall_clock=lambda: window.clock[0].timestamp(), monotonic_clock=lambda: mono[0])
    item_id = window.core.organizer.add_item("Учебная цель", "2026-10-29")
    window.navigate(5)
    assert not window.activity_monitor_toggle.isChecked() and samples == []
    window.activity_monitor_toggle.setChecked(True)
    window._poll_reminders()
    assert len(samples) == 1
    mono[0] += 5
    window.clock[0] += timedelta(seconds=5)
    window._poll_reminders()
    assert "Рабочие программы" in window.plan.evidence_totals.text()
    assert window.core.snapshot()["observations"]["observed_seconds"] == 5
    assert selected_day(window)["items"][0]["status"] == "unknown"
    window.plan.pause_evidence.click()
    assert not window.core.store.preferences()["activity_monitor_enabled"]
    assert not window.activity_monitor_toggle.isChecked()
    assert "Сбор приостановлен" in window.plan.evidence_totals.text()
    mono[0] += 5
    window.clock[0] += timedelta(seconds=5)
    window._poll_reminders()
    assert len(samples) == 2
    window.plan.clear_evidence.click()
    assert window.core.snapshot()["observations"]["observed_seconds"] == 0
    assert selected_day(window)["items"][0]["id"] == item_id
    assert selected_day(window)["items"][0]["status"] == "unknown"
    assert window.effects == [] and window.voice.spoken == []


def test_observation_clear_is_available_for_earlier_retained_days(app, window):
    from desktop.observations import ObservationMonitor
    stamp = window.clock[0].timestamp()
    window.core.observations = ObservationMonitor(window.core.store, sampler=lambda: pytest.fail("No native sampling"), wall_clock=lambda: stamp)
    with window.core.store.connection() as db:
        db.execute("INSERT INTO observation_buckets VALUES(?,?,?)", (int((stamp - 86400) // 60 * 60), "gaming", 30))
    window.refresh()
    assert window.core.snapshot()["observations"]["observed_seconds"] == 0
    assert window.plan.clear_evidence.isEnabled()
    window.plan.clear_evidence.click()
    assert not window.core.snapshot()["observations"]["has_records"]


def test_multilingual_clone_and_plan_context_are_explicit_local_preferences(app, window):
    window.navigate(4)
    assert not window.multilingual_toggle.isChecked()
    assert not window.local_chat_plan_toggle.isChecked()
    window.multilingual_toggle.setChecked(True)
    window.local_chat_plan_toggle.setChecked(True)
    window.voice_engine.setCurrentIndex(window.voice_engine.findData("clone"))
    preferences = window.core.store.preferences()
    assert preferences["multilingual_stt_enabled"] is True
    assert preferences["local_chat_plan_context"] is True
    assert preferences["voice_engine"] == "clone"
    assert window.voice_personal_license.isVisible() and not window.voice_character.isVisible()
    assert window.voice.starts == 0 and window.voice.spoken == [] and window.effects == []
