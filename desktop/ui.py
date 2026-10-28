"""Local desktop interface with explicit persisted voice and startup opt-ins."""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QDate, QObject, QRunnable, QTime, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QKeySequence, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget,
    QSlider, QSpinBox, QSystemTrayIcon, QTimeEdit, QVBoxLayout, QWidget,
)


STYLE = """
QWidget { color: #dbe6f3; font-family: 'Inter'; font-size: 14px; }
QMainWindow, #root { background: #080e18; }
QFrame#sidebar { background: #0b1421; border-right: 1px solid #1b2b40; }
QFrame#card, QFrame#message, QFrame#confirmation { background: #101c2c; border: 1px solid #203349; border-radius: 14px; }
QFrame#confirmation { border: 1px solid #b89149; background: #211e1a; }
QLabel#brand { color: #f1f7ff; font-size: 25px; font-weight: 700; letter-spacing: 3px; }
QLabel#eyebrow { color: #6c879f; font-size: 11px; font-weight: 600; letter-spacing: 2px; }
QLabel#title { color: #f1f7ff; font-size: 29px; font-weight: 600; }
QLabel#subtitle, QLabel#muted { color: #91a6bd; }
QLabel#section { font-size: 17px; font-weight: 600; color: #f1f7ff; }
QLabel#status { color: #71d8cc; background: #102e31; border: 1px solid #235252; border-radius: 13px; padding: 5px 12px; font-size: 12px; }
QPushButton { background: #18283c; border: 1px solid #2b4057; padding: 10px 15px; border-radius: 9px; font-weight: 500; }
QPushButton:hover { background: #223851; border-color: #4e718a; }
QPushButton:pressed { background: #304b65; }
QPushButton:disabled { color: #5b7187; background: #111c2b; border-color: #1d3044; }
QPushButton#primary { color: #062425; background: #79e2d2; border-color: #79e2d2; font-weight: 700; }
QPushButton#primary:hover { background: #a4f5e8; }
QPushButton#nav { text-align: left; background: transparent; color: #91a6bd; border: none; padding: 13px 16px; }
QPushButton#nav:checked { color: #d4fff6; background: #16343b; border: 1px solid #255253; }
QPushButton#nav:hover { background: #122637; color: #e5f4ff; }
QPushButton#quiet { background: transparent; border-color: #283b50; color: #a8bed1; font-size: 12px; }
QLineEdit, QPlainTextEdit { background: #0b1523; border: 1px solid #2b4057; border-radius: 9px; padding: 11px; selection-background-color: #286f72; color: #edf7ff; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #72d9cc; }
QDateEdit, QTimeEdit, QSpinBox { background: #0b1523; border: 1px solid #2b4057; border-radius: 8px; padding: 8px; }
QCalendarWidget QAbstractItemView { background: #101c2c; color: #dbe6f3; selection-background-color: #255253; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: #18283c; }
QMenu { background: #101c2c; color: #dbe6f3; }
QMenu::item:selected { background: #255253; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 4px 0; }
QScrollBar::handle:vertical { background: #294157; border-radius: 3px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCheckBox { spacing: 10px; padding: 8px 0; }
QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #4d6880; border-radius: 4px; background: #0b1523; }
QCheckBox::indicator:checked { background: #79e2d2; border-color: #79e2d2; }
QComboBox { background: #0b1523; border: 1px solid #2b4057; border-radius: 9px; padding: 9px; }
QComboBox QAbstractItemView { background: #101c2c; color: #dbe6f3; selection-background-color: #255253; }
QSlider::groove:horizontal { height: 4px; background: #2b4057; border-radius: 2px; }
QSlider::handle:horizontal { background: #79e2d2; border: 1px solid #a4f5e8; width: 16px; margin: -6px 0; border-radius: 8px; }
QToolTip { background: #18283c; color: #edf7ff; border: 1px solid #375572; padding: 6px; }
"""


def label(text: str, name: str | None = None, wrap: bool = False) -> QLabel:
    value = QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setWordWrap(wrap)
    if name:
        value.setObjectName(name)
    return value


def button(text: str, callback, name: str | None = None) -> QPushButton:
    value = QPushButton(text)
    value.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        value.setObjectName(name)
    value.clicked.connect(callback)
    return value


def app_icon() -> QIcon:
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#0b1728"))
    painter.setPen(QPen(QColor("#79e2d2"), 5))
    painter.drawRoundedRect(8, 8, 112, 112, 25, 25)
    painter.drawEllipse(28, 28, 72, 72)
    painter.setPen(QPen(QColor("#79e2d2"), 8))
    painter.drawLine(49, 43, 77, 43)
    painter.drawLine(70, 43, 70, 75)
    painter.drawArc(44, 61, 27, 24, 180 * 16, 180 * 16)
    painter.end()
    return QIcon(pixmap)


_font_loaded = False


def load_application_font():
    """Bundle a licensed font so Windows/offscreen rendering needs no web fonts."""
    global _font_loaded
    if not _font_loaded:
        font_id = QFontDatabase.addApplicationFont(str(Path(__file__).parent / "assets" / "Inter.ttf"))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                QApplication.instance().setFont(QFont(families[0], 10))
        _font_loaded = True


class Orb(QWidget):
    """Quiet identity graphic; it does not invent an audio amplitude meter."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(160, 160)
        self.active = False

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.rect().center()
        glow = QRadialGradient(center, 79)
        glow.setColorAt(0, QColor(77, 185, 187, 55 if self.active else 24))
        glow.setColorAt(1, QColor(77, 185, 187, 0))
        painter.setBrush(glow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, 160, 160)
        for radius, color, width in [(61, "#25495d", 1), (53, "#477f8f", 1), (42, "#79e2d2", 2)]:
            painter.setPen(QPen(QColor(color), width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius, radius)
        painter.setPen(QPen(QColor("#79e2d2"), 3))
        painter.drawArc(19, 19, 122, 122, 15 * 16, 68 * 16)
        painter.drawArc(19, 19, 122, 122, 195 * 16, 68 * 16)
        gradient = QLinearGradient(50, 70, 110, 90)
        gradient.setColorAt(0, QColor("#86f4de"))
        gradient.setColorAt(1, QColor("#519edc"))
        painter.setPen(QPen(gradient, 3))
        for index, height in enumerate([9, 18, 28, 40, 25, 15, 7]):
            x = 55 + index * 8
            painter.drawLine(x, 80 - height // 2, x, 80 + height // 2)


class Events(QObject):
    recognized = Signal(int, str)
    voice_state = Signal(str)
    progress = Signal(str)
    stream_chunk = Signal(int, str)


class WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.done.emit(self.function())
        except Exception as error:
            # Never display arbitrary exception text: adapters may include data/paths.
            self.signals.failed.emit(type(error).__name__)


class PlanPanel(QWidget):
    """Editable, user-owned day plans; a view never reports completion."""

    def __init__(self, core, on_changed):
        super().__init__()
        self.core, self.on_changed = core, on_changed
        self._relative_day = 0
        self._today = QDate.currentDate()
        self._editing_id = None
        self._gym_dirty = False
        self._loading_gym = False
        self.items = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(label("План", "title"))
        outer.addWidget(label("Дела с датой и гибкие тренировки. Быстрые задачи остаются в разделе «Мои задачи».", "subtitle", True))
        dates = QHBoxLayout()
        self.today_button = button("Сегодня", lambda: self.select_relative(0), "quiet")
        self.tomorrow_button = button("Завтра", lambda: self.select_relative(1), "quiet")
        dates.addWidget(self.today_button)
        dates.addWidget(self.tomorrow_button)
        self.view_date = QDateEdit(QDate.currentDate())
        self.view_date.setObjectName("planViewDate")
        self.view_date.setDisplayFormat("dd.MM.yyyy")
        self.view_date.setCalendarPopup(True)
        self.view_date.dateChanged.connect(self._date_selected)
        dates.addWidget(self.view_date)
        dates.addStretch()
        outer.addLayout(dates)
        self.feedback = label("", "muted", True)
        self.feedback.setObjectName("planFeedback")
        outer.addWidget(self.feedback)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 5, 7, 0)
        body.setSpacing(14)
        self.summary = label("", "muted", True)
        body.addWidget(self.summary)
        self.evidence_off_hint = label("Подсказки об активности выключены; их можно включить в настройках.", "muted", True)
        body.addWidget(self.evidence_off_hint)
        evidence = QFrame()
        evidence.setObjectName("card")
        self.evidence_card = evidence
        evidence_layout = QVBoxLayout(evidence)
        evidence_layout.setContentsMargins(16, 12, 16, 12)
        self.evidence_heading = label("Наблюдения сегодня · по желанию", "section")
        self.evidence_totals = label("Наблюдение выключено. Его можно включить в настройках.", "muted", True)
        evidence_layout.addWidget(self.evidence_heading)
        evidence_layout.addWidget(self.evidence_totals)
        evidence_layout.addWidget(label("Это подсказки по открытым программам, а не доказательство учёбы или зала. Браузер остаётся неизвестным; выполнение отмечаете только вы.", "muted", True))
        evidence_actions = QHBoxLayout()
        self.pause_evidence = button("Пауза наблюдений", self._pause_evidence, "quiet")
        self.pause_evidence.setEnabled(False)
        self.clear_evidence = button("Очистить наблюдения", self._clear_evidence, "quiet")
        self.clear_evidence.setEnabled(False)
        evidence_actions.addWidget(self.pause_evidence)
        evidence_actions.addWidget(self.clear_evidence)
        evidence_actions.addStretch()
        evidence_layout.addLayout(evidence_actions)
        body.addWidget(evidence)
        items_holder = QWidget()
        self.items_layout = QVBoxLayout(items_holder)
        self.items_layout.setContentsMargins(0, 0, 0, 0)
        self.items_layout.setSpacing(10)
        body.addWidget(items_holder)
        body.addWidget(self._item_form())
        body.addWidget(self._gym_form())
        body.addWidget(label("«Сделано» и «Пропущено» — только ваш явный отчёт. Без отчёта статус остаётся «Не подтверждено»; время за компьютером или окончание таймера не доказывают выполнения.", "muted", True))
        body.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.scroll = scroll

    @staticmethod
    def _set_date(control, value):
        blocked = control.blockSignals(True)
        control.setDate(value)
        control.blockSignals(blocked)

    @staticmethod
    def _clear(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()

    def refresh_evidence(self, data):
        date = QDate.fromString(data.get("date", ""), "yyyy-MM-dd")
        self.evidence_heading.setText("Наблюдения сегодня" + (" · " + date.toString("dd.MM.yyyy") if date.isValid() else ""))
        enabled = bool(data.get("enabled", False))
        observed = data.get("observed_seconds", 0)
        has_records = bool(data.get("has_records", observed > 0))
        self.evidence_card.setVisible(enabled or has_records)
        self.evidence_off_hint.setVisible(not enabled and not has_records)
        if not observed:
            self.evidence_totals.setText(("Сбор разрешён. " if enabled else "Наблюдение выключено. ")
                                        + "Записанных интервалов пока нет; это не означает, что вы ничего не делали.")
        else:
            names = {"work": "Рабочие программы", "gaming": "Игровые", "media": "Медиаплееры", "idle": "Бездействие", "unknown": "Неизвестно"}
            values = data.get("seconds", {})
            totals = " · ".join(f"{title}: {round(values.get(key, 0) / 60, 1)} мин" for key, title in names.items())
            self.evidence_totals.setText(("Сбор включён. " if enabled else "Сбор приостановлен. ")
                                        + f"Записано около {round(observed / 60, 1)} мин: " + totals)
        self.pause_evidence.setEnabled(enabled)
        self.clear_evidence.setEnabled(has_records)

    def _pause_evidence(self):
        try:
            self.core.store.set_preferences({"activity_monitor_enabled": False})
            self.core.reset_observations()
        except Exception:
            self.feedback.setText("Не удалось сохранить паузу наблюдений.")
            return
        self.on_changed()
        self.feedback.setText("Наблюдения приостановлены; сохранённые интервалы можно очистить отдельно.")

    def _clear_evidence(self):
        try:
            self.core.clear_observations()
        except Exception:
            self.feedback.setText("Не удалось очистить наблюдения. Данные плана не изменены.")
            return
        self.on_changed()
        self.feedback.setText("Наблюдения очищены. Новые интервалы появятся только при включённом сборе.")

    def _item_form(self):
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 18)
        self.form_heading = label("Добавить в план", "section")
        layout.addWidget(self.form_heading)
        self.item_title = QLineEdit()
        self.item_title.setObjectName("planItemTitle")
        self.item_title.setMaxLength(1000)
        self.item_title.setPlaceholderText("Один понятный следующий шаг")
        layout.addWidget(self.item_title)
        row = QGridLayout()
        row.addWidget(label("Дата плана", "muted"), 0, 0)
        row.addWidget(label("Тип", "muted"), 0, 1)
        self.item_date = QDateEdit(QDate.currentDate())
        self.item_date.setObjectName("planItemDate")
        self.item_date.setDisplayFormat("dd.MM.yyyy")
        self.item_date.setCalendarPopup(True)
        row.addWidget(self.item_date, 1, 0)
        self.item_kind = QComboBox()
        for title, value in [("Цель", "goal"), ("Дело", "task"), ("Учёба", "study"), ("Тренировка", "workout"), ("Отдых", "rest")]:
            self.item_kind.addItem(title, value)
        row.addWidget(self.item_kind, 1, 1)
        self.item_has_time = QCheckBox("Указать время")
        self.item_has_time.setObjectName("planItemHasTime")
        self.item_time = QTimeEdit(QTime.currentTime())
        self.item_time.setDisplayFormat("HH:mm")
        self.item_time.hide()
        self.item_has_time.toggled.connect(self.item_time.setVisible)
        row.addWidget(self.item_has_time, 2, 0)
        row.addWidget(self.item_time, 3, 0)
        self.item_has_minutes = QCheckBox("Указать длительность")
        self.item_minutes = QSpinBox()
        self.item_minutes.setRange(1, 1440)
        self.item_minutes.setValue(25)
        self.item_minutes.setSuffix(" мин")
        self.item_minutes.hide()
        self.item_has_minutes.toggled.connect(self.item_minutes.setVisible)
        row.addWidget(self.item_has_minutes, 2, 1)
        row.addWidget(self.item_minutes, 3, 1)
        layout.addLayout(row)
        self.edit_hint = label("Время и длительность необязательны. Дата плана сама по себе не означает жёсткий дедлайн.", "muted", True)
        layout.addWidget(self.edit_hint)
        controls = QHBoxLayout()
        self.save_item = button("Добавить в план", self._save_item, "primary")
        self.save_item.setObjectName("savePlanItem")
        self.cancel_edit = button("Отменить изменение", self.reset_form, "quiet")
        self.cancel_edit.hide()
        controls.addWidget(self.save_item)
        controls.addWidget(self.cancel_edit)
        controls.addStretch()
        layout.addLayout(controls)
        return card

    def _gym_form(self):
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.addWidget(label("Тренировки на неделю", "section"))
        self.gym_enabled = QCheckBox("Планировать тренировки")
        self.gym_enabled.setObjectName("gymEnabled")
        layout.addWidget(self.gym_enabled)
        days = QHBoxLayout()
        self.gym_days = []
        for index, title in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]):
            control = QCheckBox(title)
            control.setObjectName(f"gymDay{index}")
            control.toggled.connect(self._gym_changed)
            self.gym_days.append(control)
            days.addWidget(control)
        layout.addLayout(days)
        row = QGridLayout()
        self.gym_has_time = QCheckBox("Указать время")
        self.gym_time = QTimeEdit(QTime.currentTime())
        self.gym_time.setDisplayFormat("HH:mm")
        self.gym_time.hide()
        self.gym_has_time.toggled.connect(self.gym_time.setVisible)
        self.gym_has_time.toggled.connect(self._gym_changed)
        self.gym_time.timeChanged.connect(self._gym_changed)
        row.addWidget(self.gym_has_time, 0, 0)
        row.addWidget(self.gym_time, 1, 0)
        self.gym_has_minutes = QCheckBox("Указать длительность")
        self.gym_minutes = QSpinBox()
        self.gym_minutes.setRange(1, 1440)
        self.gym_minutes.setValue(45)
        self.gym_minutes.setSuffix(" мин")
        self.gym_minutes.hide()
        self.gym_has_minutes.toggled.connect(self.gym_minutes.setVisible)
        self.gym_has_minutes.toggled.connect(self._gym_changed)
        self.gym_minutes.valueChanged.connect(self._gym_changed)
        row.addWidget(self.gym_has_minutes, 0, 1)
        row.addWidget(self.gym_minutes, 1, 1)
        layout.addLayout(row)
        layout.addWidget(label("Можно выбрать только дни и оставить время свободным. Перенос или пропуск одной тренировки меняет только выбранный день; остальные недели сохраняются.", "muted", True))
        self.gym_unsaved = label("Есть несохранённые изменения", "muted")
        self.gym_unsaved.hide()
        layout.addWidget(self.gym_unsaved)
        controls = QHBoxLayout()
        self.save_gym = button("Сохранить неделю", self._save_gym, "primary")
        self.save_gym.setObjectName("saveWeeklyGym")
        self.save_gym.setEnabled(False)
        controls.addWidget(self.save_gym)
        controls.addWidget(button("Сбросить изменения", self._reset_gym, "quiet"))
        controls.addStretch()
        layout.addLayout(controls)
        self.gym_enabled.toggled.connect(self._gym_changed)
        return card

    def _date_selected(self, _date):
        self._relative_day = None
        self.refresh()

    def select_relative(self, offset):
        self._relative_day = offset
        self.refresh()

    def refresh(self):
        organizer = getattr(self.core, "organizer", None)
        if organizer is None:
            self.feedback.setText("План пока недоступен. Быстрые задачи и заметки сохраняются отдельно.")
            return
        try:
            snapshot = organizer.snapshot()
            today = QDate.fromString(snapshot["today"]["date"], "yyyy-MM-dd")
            if not today.isValid():
                raise ValueError("Invalid plan date")
            self._today = today
            if self._relative_day is not None:
                self._set_date(self.view_date, today.addDays(self._relative_day))
            if self._editing_id is None and not self.item_title.text().strip():
                self._set_date(self.item_date, self.view_date.date())
            day = organizer.day(self.view_date.date().toString("yyyy-MM-dd"))
            self.items = day["items"]
            self._render_items()
            if not self._gym_dirty:
                self._load_gym(organizer.gym_settings())
        except Exception:
            self._clear(self.items_layout)
            self.items = []
            self.feedback.setText("Не удалось прочитать текущий план. Данные не подменены исторической копией.")

    def _render_items(self):
        self._clear(self.items_layout)
        counts = {status: sum(item["status"] == status for item in self.items) for status in ("done", "missed", "unknown")}
        self.summary.setText(f"Сделано: {counts['done']} · Пропущено: {counts['missed']} · Не подтверждено: {counts['unknown']}")
        if not self.items:
            self.items_layout.addWidget(label("На эту дату пока ничего не запланировано. Можно добавить одно дело или выбрать дни тренировок.", "muted", True))
        for item in self.items:
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(16, 13, 16, 13)
            title = label(item["title"], wrap=True)
            title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(title)
            details = item.get("time") or "Время не задано"
            if item.get("minutes") is not None:
                details += f" · План: {item['minutes']} мин"
            if item.get("reported_minutes") is not None:
                details += f" · Ваш отчёт: {item['reported_minutes']} мин"
            layout.addWidget(label(details, "muted", True))
            actions = QHBoxLayout()
            status = QComboBox()
            status.setObjectName("planStatus_" + item["id"])
            for text, value in [("Не подтверждено", "unknown"), ("Сделано", "done"), ("Пропущено", "missed")]:
                status.addItem(text, value)
            status.setCurrentIndex(max(0, status.findData(item["status"])))
            status.currentIndexChanged.connect(lambda _index, item_id=item["id"], control=status: self._report(item_id, control.currentData()))
            actions.addWidget(status)
            edit = button("Изменить", lambda _checked=False, value=dict(item): self.edit_item(value), "quiet")
            edit.setObjectName("editPlan_" + item["id"])
            actions.addWidget(edit)
            remove = button("Убрать", lambda _checked=False, item_id=item["id"]: self._remove(item_id), "quiet")
            remove.setObjectName("removePlan_" + item["id"])
            actions.addWidget(remove)
            actions.addStretch()
            layout.addLayout(actions)
            self.items_layout.addWidget(card)

    def edit_item(self, item):
        self._editing_id = item["id"]
        self.item_title.setText(item["title"])
        self._set_date(self.item_date, QDate.fromString(item["date"], "yyyy-MM-dd"))
        routine = item["kind"] == "routine"
        self.item_kind.setCurrentIndex(max(0, self.item_kind.findData("workout" if routine else item["kind"])))
        self.item_kind.setEnabled(False)
        self.item_has_time.setChecked(item.get("time") is not None)
        if item.get("time"):
            self.item_time.setTime(QTime.fromString(item["time"], "HH:mm"))
        self.item_has_minutes.setChecked(item.get("minutes") is not None)
        if item.get("minutes") is not None:
            self.item_minutes.setValue(item["minutes"])
        self.form_heading.setText("Изменить выбранный день")
        self.save_item.setText("Сохранить изменения")
        self.cancel_edit.show()
        self.edit_hint.setText("Изменение относится только к выбранному дню. Повторяющаяся неделя настраивается ниже." if routine else "Можно изменить дату, время, название и длительность. Выполнение не предполагается автоматически.")
        self.scroll.ensureWidgetVisible(self.item_title)
        self.item_title.setFocus()

    def reset_form(self):
        self._editing_id = None
        self.item_title.clear()
        self._set_date(self.item_date, self.view_date.date())
        self.item_has_time.setChecked(False)
        self.item_has_minutes.setChecked(False)
        self.item_kind.setEnabled(True)
        self.form_heading.setText("Добавить в план")
        self.save_item.setText("Добавить в план")
        self.cancel_edit.hide()
        self.edit_hint.setText("Время и длительность необязательны. Дата плана сама по себе не означает жёсткий дедлайн.")

    def _changed(self, message):
        self.refresh()
        self.feedback.setText(message)
        self.on_changed()

    def _save_item(self):
        organizer = getattr(self.core, "organizer", None)
        if organizer is None:
            return
        fields = {"title": self.item_title.text().strip(), "on_date": self.item_date.date().toString("yyyy-MM-dd"),
                  "start_time": self.item_time.time().toString("HH:mm") if self.item_has_time.isChecked() else None,
                  "minutes": self.item_minutes.value() if self.item_has_minutes.isChecked() else None}
        try:
            if self._editing_id is None:
                organizer.add_item(**fields, kind=self.item_kind.currentData())
            else:
                organizer.update_item(self._editing_id, **fields)
        except ValueError:
            self.feedback.setText("Проверьте название, дату, время и длительность.")
            return
        except Exception:
            self.feedback.setText("Не удалось сохранить план. Успех не подтверждён.")
            return
        self._relative_day = None
        self._set_date(self.view_date, self.item_date.date())
        self.reset_form()
        self._changed("План сохранён. Статус выполнения требует вашего отчёта.")

    def _report(self, item_id, status):
        try:
            self.core.organizer.report_item(item_id, status=status)
        except Exception:
            self.feedback.setText("Не удалось сохранить отчёт. Статус не подтверждён.")
            self.refresh()
            return
        self._changed("Ваш отчёт сохранён.")

    def _remove(self, item_id):
        try:
            changed = self.core.organizer.delete_item(item_id)
        except Exception:
            self.feedback.setText("Не удалось убрать запись из плана.")
            return
        if not changed:
            self.feedback.setText("Запись уже изменилась. Обновите план.")
        if self._editing_id == item_id:
            self.reset_form()
        self._changed("Запись убрана только из выбранного дня." if changed else "План обновлён.")

    def _gym_changed(self, *_args):
        if not self._loading_gym:
            self._gym_dirty = True
            self.gym_unsaved.show()
            self.save_gym.setEnabled(True)

    def _load_gym(self, value):
        self._loading_gym = True
        try:
            self.gym_enabled.setChecked(value["enabled"])
            for index, control in enumerate(self.gym_days):
                control.setChecked(index in value["days"])
            self.gym_has_time.setChecked(value.get("time") is not None)
            if value.get("time"):
                self.gym_time.setTime(QTime.fromString(value["time"], "HH:mm"))
            self.gym_has_minutes.setChecked(value.get("minutes") is not None)
            if value.get("minutes") is not None:
                self.gym_minutes.setValue(value["minutes"])
        finally:
            self._loading_gym = False
        self.gym_unsaved.hide()
        self.save_gym.setEnabled(False)

    def _reset_gym(self):
        self._gym_dirty = False
        self.refresh()

    def _save_gym(self):
        try:
            self.core.organizer.set_weekly_gym(days=[index for index, control in enumerate(self.gym_days) if control.isChecked()],
                time=self.gym_time.time().toString("HH:mm") if self.gym_has_time.isChecked() else None,
                minutes=self.gym_minutes.value() if self.gym_has_minutes.isChecked() else None,
                enabled=self.gym_enabled.isChecked())
        except ValueError:
            self.feedback.setText("Выберите дни тренировок или отключите планирование. Проверьте время и длительность.")
            return
        except Exception:
            self.feedback.setText("Не удалось сохранить тренировочную неделю.")
            return
        self._gym_dirty = False
        self._changed("Тренировочная неделя сохранена. Время можно оставить свободным.")


class MainWindow(QMainWindow):
    SETTINGS_PAGE = 4
    PLAN_PAGE = 5
    CONTROLS_PAGE = 6
    MAX_PREVIEW_CHARS = 22000

    def __init__(self, core, voice_factory=None, model_dir: Path | None = None, enable_tray: bool = True, personal_path: Path | None = None, *, enable_integrations: bool = False, quiet_start: bool = False):
        super().__init__()
        load_application_font()
        self.core = core
        self.model_dir = model_dir
        self.personal_path = personal_path
        self.events = Events()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._workers = set()
        self._integrations_enabled = bool(enable_integrations)
        self._integration_pool = QThreadPool(self)
        self._integration_pool.setMaxThreadCount(1)
        self._integration_workers = set()
        self._integration_busy = False
        self._phone_server = self._phone_delivery = None
        self._retired_phone_servers = []
        self._remote_controller = None
        self._remote_configuration_pending = False
        self._phone_state, self._delivery_state = {}, {}
        self._phone_notifications = []
        self._phone_stop_complete = not self._integrations_enabled
        self.busy = False
        self.listening = False
        self._capture_session = 0
        self._capture_requested = False
        self._quitting = False
        self._resources_closed = False
        self._session_notifications_registered = False
        self._capture_failed = False
        self._capture_error = None
        self._request_serial = 0
        self._active_stream_id = None
        self._stream_buffer = ""
        self._stream_timer = QTimer(self)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.setInterval(50)
        self._stream_timer.timeout.connect(self._flush_stream_preview)
        self._voice_rate_timer = QTimer(self)
        self._voice_rate_timer.setSingleShot(True)
        self._voice_rate_timer.setInterval(200)
        self._voice_rate_timer.timeout.connect(self._commit_voice_rate)
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self._finish_close)
        self._confirmation = None
        self._tray_notice_shown = False
        self._view_index = 0
        self._startup_pending = False
        self.tray = None
        self.voice_factory = voice_factory
        self.preferences = self.core.snapshot().get("preferences", {})
        self.setWindowTitle("JARVIS — личный ассистент")
        self.setWindowIcon(app_icon())
        self.resize(1120, 790)
        self.setMinimumSize(860, 620)
        self.setStyleSheet(STYLE)
        from desktop.owner_auth import register_session_notifications
        self._session_notifications_registered = register_session_notifications(int(self.winId()))
        self._build()
        self.events.recognized.connect(self._recognized_for_session)
        self.events.voice_state.connect(self._voice_state)
        self.events.progress.connect(self._model_progress)
        self.events.stream_chunk.connect(self._receive_stream_chunk)
        self.voice = self._new_voice()
        if enable_tray and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.windowIcon(), self)
            menu = QMenu(self)
            menu.addAction("Открыть JARVIS", self.restore_window)
            menu.addAction("Выключить микрофон", self.stop_listening)
            menu.addSeparator()
            menu.addAction("Завершить работу", self.quit_application)
            self.tray.setContextMenu(menu)
            self.tray.setToolTip("JARVIS · микрофон выключен")
            self.tray.activated.connect(lambda reason: self.restore_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
            self.tray.show()
        self._shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._shortcut.activated.connect(lambda: (self.navigate(0), self.command.setFocus()))
        self.refresh()
        self.refresh_personal()
        self._reminder_timer = QTimer(self)
        self._reminder_timer.setInterval(1000)
        self._reminder_timer.timeout.connect(self._poll_reminders)
        self._reminder_timer.start()
        self._integration_timer = QTimer(self)
        self._integration_timer.setInterval(5000)
        self._integration_timer.timeout.connect(self._poll_phone_integrations)
        if self._integrations_enabled:
            self._start_phone_integrations()
            self._integration_timer.start()
        if self.preferences.get("voice_enabled", False):
            QTimer.singleShot(0, self.start_listening)
        if not quiet_start:
            QTimer.singleShot(0, lambda: self._poll_organizer_events("startup"))

    def _new_voice(self):
        factory = self.voice_factory
        if factory is None:
            from desktop.voice import VoiceService
            factory = VoiceService
        voice = factory(on_text=self._recognition_callback(self._capture_session), on_state=self.events.voice_state.emit, model_dir=self.model_dir)
        voice.speech_enabled = bool(self.preferences.get("speech_enabled", True))
        apply_preferences = getattr(voice, "apply_preferences", None)
        if callable(apply_preferences):
            apply_preferences(dict(self.preferences))
        prepare = getattr(voice, "prepare", None)
        if callable(prepare):
            prepare()
        return voice

    def _recognition_callback(self, session_id):
        def recognized(text):
            if isinstance(text, str):
                self.events.recognized.emit(session_id, text)
        return recognized

    def _recognized_for_session(self, session_id, text):
        if session_id == self._capture_session:
            self._voice_command(text)

    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(22, 29, 18, 23)
        side.setSpacing(8)
        side.addWidget(label("JARVIS", "brand"))
        side.addWidget(label("ЛИЧНЫЙ АССИСТЕНТ", "eyebrow"))
        side.addSpacing(35)
        self.nav_buttons = []
        self.nav_targets = [(0, "Ассистент"), (self.PLAN_PAGE, "План"), (1, "Мои задачи"), (2, "Заметки"), (3, "Мои данные"), (self.CONTROLS_PAGE, "Управление"), (self.SETTINGS_PAGE, "Настройки")]
        for index, text in self.nav_targets:
            nav = button(text, lambda _checked=False, value=index: self.navigate(value), "nav")
            nav.setCheckable(True)
            self.nav_buttons.append(nav)
            side.addWidget(nav)
        side.addStretch()
        side.addWidget(label("НА ЭТОМ КОМПЬЮТЕРЕ", "eyebrow"))
        side.addWidget(label("Задачи и заметки доступны\nбез интернета.", "muted", True))
        side.addSpacing(15)
        side.addWidget(label("DESKTOP 4.4", "eyebrow"))
        shell.addWidget(sidebar)
        content = QWidget()
        main = QVBoxLayout(content)
        main.setContentsMargins(30, 23, 30, 22)
        main.setSpacing(19)
        top = QHBoxLayout()
        top.addWidget(label("ВАШЕ ПРОСТРАНСТВО", "eyebrow"))
        top.addStretch()
        self.status = label("Микрофон выключен", "status")
        self.status.setObjectName("status")
        top.addWidget(self.status)
        main.addLayout(top)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._assistant_page())
        self.pages.addWidget(self._tasks_page())
        self.pages.addWidget(self._notes_page())
        self.pages.addWidget(self._personal_page())
        self.pages.addWidget(self._settings_page())
        self.plan = PlanPanel(self.core, self._plan_changed)
        self.pages.addWidget(self.plan)
        from desktop.control_panel import ControlsPanel
        self.controls = ControlsPanel(self.core, main_window=self, on_reply=self._reply,
                                      remote_status=self._remote_status)
        from desktop.remote_setup_ui import RemoteSetupPanel
        self.remote_setup = RemoteSetupPanel(self.core, on_configured=self._apply_remote_setup)
        self.controls.layout().addWidget(self.remote_setup)
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setWidget(self.controls)
        self.pages.addWidget(controls_scroll)
        main.addWidget(self.pages, 1)
        footer = QHBoxLayout()
        footer.addWidget(label("Управление голосом — только после включения микрофона", "muted"))
        footer.addStretch()
        footer.addWidget(label("Ctrl + K", "eyebrow"))
        main.addLayout(footer)
        shell.addWidget(content, 1)
        self.navigate(0)

    def _assistant_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        hero = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("К вашим услугам.", "title"))
        titles.addWidget(label("Один следующий шаг. Всё важное — под рукой.", "subtitle", True))
        self.timer_hint = label("", "muted")
        self.timer_hint.hide()
        titles.addWidget(self.timer_hint)
        titles.addSpacing(12)
        quick = QHBoxLayout()
        for text, command in [("Следующий шаг", "следующий шаг"), ("Мои задачи", "задачи"), ("Что ты умеешь?", "помощь")]:
            quick.addWidget(button(text, lambda _checked=False, value=command: self.submit(value), "quiet"))
        quick.addStretch()
        titles.addLayout(quick)
        hero.addLayout(titles, 1)
        self.orb = Orb()
        hero.addWidget(self.orb)
        layout.addLayout(hero)
        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history_container = QWidget()
        self.history_layout = QVBoxLayout(self.history_container)
        self.history_layout.setContentsMargins(0, 0, 7, 0)
        self.history_layout.setSpacing(10)
        self.history_layout.addStretch()
        self.history_scroll.setWidget(self.history_container)
        layout.addWidget(self.history_scroll, 1)
        self.stream_preview = QFrame()
        self.stream_preview.setObjectName("message")
        preview_layout = QVBoxLayout(self.stream_preview)
        preview_layout.setContentsMargins(16, 12, 16, 12)
        preview_layout.addWidget(label("ПРЕДВАРИТЕЛЬНЫЙ ТЕКСТ · ОТВЕТ ЕЩЁ ФОРМИРУЕТСЯ", "eyebrow", True))
        preview_scroll = QScrollArea()
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setMinimumHeight(48)
        preview_scroll.setMaximumHeight(150)
        self.stream_text = label("", wrap=True)
        self.stream_text.setObjectName("streamPreviewText")
        self.stream_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_scroll.setWidget(self.stream_text)
        preview_layout.addWidget(preview_scroll)
        self.stream_preview.hide()
        layout.addWidget(self.stream_preview)
        self.confirmation = QFrame()
        self.confirmation.setObjectName("confirmation")
        confirm_layout = QVBoxLayout(self.confirmation)
        confirm_layout.setContentsMargins(16, 13, 16, 13)
        self.confirm_text = label("", wrap=True)
        confirm_layout.addWidget(self.confirm_text)
        confirm_buttons = QHBoxLayout()
        self.confirm_button = button("Подтвердить действие", self.confirm_action, "primary")
        self.cancel_button = button("Отмена", self.cancel_action)
        confirm_buttons.addWidget(self.confirm_button)
        confirm_buttons.addWidget(self.cancel_button)
        confirm_buttons.addStretch()
        confirm_layout.addLayout(confirm_buttons)
        self.confirmation.hide()
        layout.addWidget(self.confirmation)
        command_row = QHBoxLayout()
        command_row.setSpacing(9)
        self.command = QLineEdit()
        self.command.setObjectName("commandInput")
        self.command.setPlaceholderText("Напишите команду или скажите «Джарвис»…")
        self.command.setMaxLength(5000)
        self.command.setMinimumHeight(49)
        self.command.returnPressed.connect(self.submit_current)
        command_row.addWidget(self.command, 1)
        self.send_button = button("Отправить", self.submit_current, "primary")
        command_row.addWidget(self.send_button)
        self.mic_button = button("Микрофон", self.toggle_listening)
        self.mic_button.setObjectName("microphoneButton")
        command_row.addWidget(self.mic_button)
        layout.addLayout(command_row)
        return page

    def _tasks_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Мои задачи", "title"))
        layout.addWidget(label("Начните с одного понятного действия.", "subtitle"))
        layout.addSpacing(15)
        row = QHBoxLayout()
        self.task_input = QLineEdit()
        self.task_input.setObjectName("taskInput")
        self.task_input.setPlaceholderText("Например, прочитать 10 страниц")
        self.task_input.setMaxLength(500)
        self.task_input.returnPressed.connect(self.add_task)
        row.addWidget(self.task_input, 1)
        self.task_add = button("Добавить задачу", self.add_task, "primary")
        row.addWidget(self.task_add)
        layout.addLayout(row)
        self.task_scroll = QScrollArea()
        self.task_scroll.setWidgetResizable(True)
        holder = QWidget()
        self.task_layout = QVBoxLayout(holder)
        self.task_layout.setContentsMargins(0, 13, 6, 0)
        self.task_layout.setSpacing(10)
        self.task_scroll.setWidget(holder)
        layout.addWidget(self.task_scroll, 1)
        return page

    def _notes_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Заметки", "title"))
        layout.addWidget(label("Сохраните мысль сейчас — найдите, когда понадобится.", "subtitle"))
        self.note_input = QPlainTextEdit()
        self.note_input.setObjectName("noteInput")
        self.note_input.setPlaceholderText("Идея, полезный факт или материал…")
        self.note_input.setMaximumHeight(110)
        layout.addWidget(self.note_input)
        row = QHBoxLayout()
        self.note_search = QLineEdit()
        self.note_search.setObjectName("noteSearch")
        self.note_search.setPlaceholderText("Найти в заметках")
        self.note_search.textChanged.connect(self.refresh_notes)
        row.addWidget(self.note_search, 1)
        self.note_save = button("Сохранить заметку", self.add_note, "primary")
        row.addWidget(self.note_save)
        layout.addLayout(row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.note_layout = QVBoxLayout(holder)
        self.note_layout.setContentsMargins(0, 12, 6, 0)
        self.note_layout.setSpacing(10)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        return page

    def _settings_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(label("Просто и под вашим контролем", "title"))
        outer.addWidget(label("Настройки, которые действительно нужны.", "subtitle"))
        outer.addSpacing(15)
        card = QFrame()
        card.setObjectName("card")
        content = QVBoxLayout(card)
        content.setContentsMargins(22, 20, 22, 20)
        content.setSpacing(10)
        content.addWidget(label("Голос и микрофон", "section"))
        self.speech_toggle = QCheckBox("Отвечать голосом")
        self.speech_toggle.setObjectName("speechToggle")
        self.speech_toggle.setChecked(bool(self.preferences.get("speech_enabled", True)))
        self.speech_toggle.toggled.connect(lambda value: self.save_preference("speech_enabled", value))
        content.addWidget(self.speech_toggle)
        engine_row = QHBoxLayout()
        engine_row.addWidget(label("Голос", "muted"))
        self.voice_engine = QComboBox()
        self.voice_engine.setObjectName("voiceEngineChoice")
        self.voice_engine.addItem("Базовый голос: Дмитрий (Piper) / Windows", "auto")
        self.voice_engine.addItem("Голос Windows", "windows")
        self.voice_engine.addItem("Выразительный голос (личное использование)", "expressive")
        self.voice_engine.addItem("Голос по вашему образцу (RU/EN)", "clone")
        engine_index = self.voice_engine.findData(self.preferences.get("voice_engine", "auto"))
        self.voice_engine.setCurrentIndex(max(0, engine_index))
        self.voice_engine.currentIndexChanged.connect(self._voice_engine_changed)
        engine_row.addWidget(self.voice_engine, 1)
        content.addLayout(engine_row)
        self.voice_character_label = label("Тембр выразительного голоса", "muted")
        self.voice_character = QComboBox()
        self.voice_character.setObjectName("voiceCharacterChoice")
        self.voice_character.addItem("Айдар", "aidar")
        self.voice_character.addItem("Евгений", "eugene")
        self.voice_character.setCurrentIndex(max(0, self.voice_character.findData(self.preferences.get("voice_character", "aidar"))))
        self.voice_character.currentIndexChanged.connect(lambda _index: self.save_preference("voice_character", self.voice_character.currentData()))
        self.voice_personal_license = label("Этот вариант предназначен только для личного использования. Модель имеет некоммерческую лицензию и не подходит для публичной продажи приложения.", "muted", True)
        for widget in (self.voice_character_label, self.voice_character):
            widget.setVisible(self.voice_engine.currentData() == "expressive")
            content.addWidget(widget)
        self.voice_personal_license.setVisible(self.voice_engine.currentData() in {"expressive", "clone"})
        content.addWidget(self.voice_personal_license)
        content.addWidget(label("Режим по образцу не подменяет тембр другим голосом. Пока он готовится или недоступен, ответ остаётся текстом. Сходство и произношение можно проверить кнопкой ниже.", "muted", True))
        self.voice_backend_status = label("Источник голоса определяется при подготовке.", "muted", True)
        self.voice_backend_status.setObjectName("voiceBackendStatus")
        content.addWidget(self.voice_backend_status)
        self.voice_rate_label = label("Темп речи", "muted")
        content.addWidget(self.voice_rate_label)
        speed_row = QHBoxLayout()
        speed_row.addWidget(label("Медленнее", "muted"))
        self.voice_rate = QSlider(Qt.Orientation.Horizontal)
        self.voice_rate.setObjectName("voiceRateSlider")
        self.voice_rate.setRange(90, 240)
        self.voice_rate.setSingleStep(5)
        self.voice_rate.setPageStep(15)
        self.voice_rate.setValue(int(self.preferences.get("voice_rate", 185)))
        self.voice_rate.valueChanged.connect(lambda _value: self._voice_rate_timer.start())
        self.voice_rate.sliderReleased.connect(self._commit_voice_rate)
        speed_row.addWidget(self.voice_rate, 1)
        speed_row.addWidget(label("Быстрее", "muted"))
        content.addLayout(speed_row)
        self.voice_sample = button("Послушать пример голоса", self._sample_voice, "quiet")
        self.voice_sample.setObjectName("voiceSampleButton")
        self.voice_sample.setEnabled(bool(self.preferences.get("speech_enabled", True)))
        content.addWidget(self.voice_sample, alignment=Qt.AlignmentFlag.AlignLeft)
        self.tray_toggle = QCheckBox("При закрытии оставлять JARVIS в трее")
        self.tray_toggle.setChecked(bool(self.preferences.get("close_to_tray", True)))
        self.tray_toggle.toggled.connect(lambda value: self.save_preference("close_to_tray", value))
        content.addWidget(self.tray_toggle)
        self.auto_listen_toggle = QCheckBox("Слушать «Джарвис» автоматически после запуска")
        self.auto_listen_toggle.setObjectName("autoListenToggle")
        self.auto_listen_toggle.setChecked(bool(self.preferences.get("voice_enabled", False)))
        self.auto_listen_toggle.toggled.connect(self._auto_listen_changed)
        content.addWidget(self.auto_listen_toggle)
        self.multilingual_toggle = QCheckBox("Понимать русскую и английскую речь")
        self.multilingual_toggle.setChecked(bool(self.preferences.get("multilingual_stt_enabled", False)))
        self.multilingual_toggle.toggled.connect(self._multilingual_changed)
        content.addWidget(self.multilingual_toggle)
        content.addWidget(label("Двуязычный режим использует отдельно установленную локальную модель. Если её нет, продолжает работать русский режим; аудио не отправляется в облако.", "muted", True))
        self.launch_at_login_toggle = QCheckBox("Запускать JARVIS при входе в Windows")
        self.launch_at_login_toggle.setObjectName("launchAtLoginToggle")
        self.launch_at_login_toggle.setChecked(bool(self.preferences.get("launch_at_login", False)))
        self.launch_at_login_toggle.toggled.connect(self._configure_startup_setting)
        content.addWidget(self.launch_at_login_toggle)
        content.addWidget(label("Без автослушания микрофон включается только кнопкой. Автослушание работает, пока JARVIS запущен в вашей пользовательской сессии. Ноутбук должен быть включён: голос не будит его из сна и не включает выключенный компьютер. Микрофон можно выключить кнопкой или в трее.", "muted", True))
        content.addWidget(label("После включения скажите «Джарвис», затем команду. Распознавание работает локально. Голосовое выключение компьютера потребует отдельного нажатия подтверждения.", "muted", True))
        content.addSpacing(8)
        self.model_status = label("Офлайн-модель распознавания устанавливается отдельно.", "muted", True)
        content.addWidget(self.model_status)
        self.model_button = button("Установить русскую голосовую модель", self.install_model)
        self.model_button.setObjectName("installModelButton")
        content.addWidget(self.model_button, alignment=Qt.AlignmentFlag.AlignLeft)
        content.addWidget(label("Однократная загрузка модели Vosk с официального сайта. После установки распознавание работает без интернета; звук в облако не отправляется.", "muted", True))
        outer.addWidget(card)
        daily = QFrame()
        daily.setObjectName("card")
        daily_layout = QVBoxLayout(daily)
        daily_layout.setContentsMargins(22, 20, 22, 20)
        daily_layout.addWidget(label("Утро и вечер", "section"))
        self.organizer_toggle = QCheckBox("Помогать с дневным планом")
        self.morning_toggle = QCheckBox("Утренний обзор при начале дня")
        self.evening_toggle = QCheckBox("Вечером предложить обсудить завтра")
        self.organizer_pause_toggle = QCheckBox("Пауза инициативных обращений")
        self.organizer_dnd_toggle = QCheckBox("Не беспокоить")
        for key, control, default in [
            ("organizer_enabled", self.organizer_toggle, True),
            ("morning_brief_enabled", self.morning_toggle, True),
            ("evening_plan_enabled", self.evening_toggle, True),
            ("organizer_paused", self.organizer_pause_toggle, False),
            ("organizer_dnd", self.organizer_dnd_toggle, False),
        ]:
            control.setChecked(bool(self.preferences.get(key, default)))
            control.toggled.connect(lambda value, name=key: self._organizer_preference_changed(name, value))
            daily_layout.addWidget(control)
        evening_row = QHBoxLayout()
        evening_row.addWidget(label("Время вечернего обращения", "muted"))
        self.evening_time = QTimeEdit(QTime.fromString(self.preferences.get("evening_plan_time", "22:00"), "HH:mm"))
        self.evening_time.setObjectName("eveningPlanTime")
        self.evening_time.setDisplayFormat("HH:mm")
        self.evening_time.timeChanged.connect(lambda value: self._organizer_preference_changed("evening_plan_time", value.toString("HH:mm")))
        evening_row.addWidget(self.evening_time)
        evening_row.addStretch()
        daily_layout.addLayout(evening_row)
        daily_layout.addWidget(label("Утром — разово при запуске или первом обращении. Вечером — в выбранное время. JARVIS должен быть запущен, а ноутбук — включён. План формируется из ваших актуальных записей, без выдуманных занятий или выполнения.", "muted", True))
        outer.addWidget(daily)
        observations = QFrame()
        observations.setObjectName("card")
        observation_layout = QVBoxLayout(observations)
        observation_layout.setContentsMargins(22, 20, 22, 20)
        observation_layout.addWidget(label("Подсказки об активности · по желанию", "section"))
        self.activity_monitor_toggle = QCheckBox("Учитывать категории активности на компьютере")
        self.activity_monitor_toggle.setObjectName("activityMonitorToggle")
        self.activity_monitor_toggle.setChecked(bool(self.preferences.get("activity_monitor_enabled", False)))
        self.activity_monitor_toggle.toggled.connect(self._activity_monitor_changed)
        observation_layout.addWidget(self.activity_monitor_toggle)
        observation_layout.addWidget(label("Раз в 5 секунд определяется категория активного приложения и бездействие. Сохраняются только секунды категорий по минутам, на 7 дней. Названия приложений сразу отбрасываются; заголовки окон, адреса сайтов, снимки экрана и нажатия клавиш не читаются. Эти интервалы не включаются в резервные копии приложения.", "muted", True))
        observation_layout.addWidget(label("Рабочая программа не доказывает учёбу. Браузер остаётся неизвестным; чтение, видео и геймпад могут выглядеть как бездействие. Компьютер не подтверждает посещение зала, а игровые минуты не означают пропущенной цели.", "muted", True))
        outer.addWidget(observations)
        data = QFrame()
        data.setObjectName("card")
        data_layout = QVBoxLayout(data)
        data_layout.setContentsMargins(22, 20, 22, 20)
        data_layout.addWidget(label("Личные данные остаются здесь", "section"))
        data_layout.addWidget(label("Задачи, заметки и история хранятся на этом компьютере. Telegram и облачные сервисы не нужны для основных команд. Удаление приложения не удаляет ваши данные.", "muted", True))
        outer.addWidget(data)
        phone = QFrame()
        phone.setObjectName("card")
        phone_layout = QVBoxLayout(phone)
        phone_layout.setContentsMargins(22, 20, 22, 20)
        phone_layout.addWidget(label("iPhone и уведомления · по желанию", "section"))
        self.phone_status = label("Локальный мост не настроен.", "muted", True)
        self.phone_status.setObjectName("phoneIntegrationStatus")
        self.phone_delivery_status = label("Telegram: не подключён. Звонки: не подключены.", "muted", True)
        self.phone_delivery_status.setObjectName("phoneDeliveryStatus")
        phone_layout.addWidget(self.phone_status)
        phone_layout.addWidget(self.phone_delivery_status)
        phone_layout.addWidget(label("План доступен на телефоне, пока ноутбук включён и работает защищённое соединение. Готовность моста не подтверждает подключение телефона. Отправка сообщения или звонка не означает, что вы его прочитали или услышали.", "muted", True))
        phone_layout.addWidget(button("Настройка iPhone и уведомлений", self._open_phone_setup, "quiet"), alignment=Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(phone)
        local_chat = QFrame()
        local_chat.setObjectName("card")
        chat_layout = QVBoxLayout(local_chat)
        chat_layout.setContentsMargins(22, 20, 22, 20)
        chat_layout.addWidget(label("Разговоры с локальным ИИ · необязательно", "section"))
        self.local_chat_toggle = QCheckBox("Включить разговоры через Ollama на этом компьютере")
        self.local_chat_toggle.setChecked(bool(self.preferences.get("local_chat_enabled", False)))
        self.local_chat_toggle.toggled.connect(lambda value: self.save_preference("local_chat_enabled", value))
        chat_layout.addWidget(self.local_chat_toggle)
        chat_layout.addWidget(label("Нужны отдельно установленная Ollama и загруженная модель. ИИ отвечает текстом и не управляет компьютером. Обращения в облако не отправляются.", "muted", True))
        self.local_chat_history_toggle = QCheckBox("Помнить последние реплики локального чата")
        self.local_chat_history_toggle.setObjectName("localChatHistoryToggle")
        self.local_chat_history_toggle.setChecked(bool(self.preferences.get("local_chat_history", False)))
        self.local_chat_history_toggle.toggled.connect(lambda value: self.save_preference("local_chat_history", value))
        chat_layout.addWidget(self.local_chat_history_toggle)
        chat_layout.addWidget(label("До 6 сообщений, только этот компьютер; команды, заметки и личная копия автоматически не передаются.", "muted", True))
        self.local_chat_plan_toggle = QCheckBox("Разрешить локальному ИИ видеть актуальный план")
        self.local_chat_plan_toggle.setChecked(bool(self.preferences.get("local_chat_plan_context", False)))
        self.local_chat_plan_toggle.toggled.connect(lambda value: self.save_preference("local_chat_plan_context", value))
        chat_layout.addWidget(self.local_chat_plan_toggle)
        chat_layout.addWidget(label("Только план на сегодня и завтра с явными статусами. Заметки, архив и управление компьютером не передаются; ИИ не получает права выполнять команды.", "muted", True))
        model_row = QHBoxLayout()
        self.chat_model = QLineEdit(str(self.preferences.get("local_chat_model", "qwen3:4b-instruct-2507-q4_K_M")))
        self.chat_model.setPlaceholderText("Имя установленной модели Ollama")
        self.chat_model.setMaxLength(100)
        model_row.addWidget(self.chat_model, 1)
        model_row.addWidget(button("Сохранить модель", lambda: self.save_preference("local_chat_model", self.chat_model.text().strip())))
        chat_layout.addLayout(model_row)
        self.usage_status = label("Статистика локального ИИ пока недоступна.", "muted", True)
        self.usage_status.setObjectName("localAIUsageStatus")
        chat_layout.addWidget(self.usage_status)
        chat_layout.addWidget(label("Учёт хранит только числа, без текстов бесед. Недоступные значения не считаются нулевыми; стоимость не оценивается.", "muted", True))
        outer.addWidget(local_chat)
        self.exit_button = button("Завершить Jarvis", self.quit_application, "quiet")
        self.exit_button.setObjectName("exitApplication")
        outer.addWidget(self.exit_button)
        outer.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _personal_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(label("Мои данные", "title"))
        top.addStretch()
        top.addWidget(button("Обновить из сохранённой копии", self.refresh_personal, "quiet"))
        layout.addLayout(top)
        layout.addWidget(label("Сохранённая копия · 10 октября 2026. Изменения после этой даты здесь не отражены.", "subtitle", True))
        layout.addWidget(label("Telegram — дополнительный канал. Облачное подключение сейчас недоступно; эта страница читает восстановленные данные на компьютере.", "muted", True))
        self.personal_status = label("Проверяю сохранённые данные…", "muted", True)
        self.personal_status.setObjectName("personalStatus")
        layout.addWidget(self.personal_status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.personal_layout = QVBoxLayout(holder)
        self.personal_layout.setContentsMargins(0, 9, 7, 0)
        self.personal_layout.setSpacing(12)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        return page

    def refresh_personal(self):
        if not hasattr(self, "personal_layout"):
            return
        from desktop.personal import display_due, load_personal_snapshot
        snapshot = load_personal_snapshot(self.personal_path)
        self._clear_layout(self.personal_layout)
        statuses = {
            "missing": "Сохранённая копия пока не найдена. Новые задачи и заметки уже можно создавать в приложении.",
            "selection_required": "В копии несколько профилей. Для показа личных данных сначала нужно явно выбрать владельца; чужие данные не открываются.",
            "identity_missing": "В этой копии не удалось определить владельца. Личные записи не показаны.",
            "unavailable": "Не удалось прочитать сохранённую копию. Исходный файл не изменён.",
            "ready": "Данные восстановлены из резервной копии. Это исторический снимок, а не текущая синхронизация.",
        }
        self.personal_status.setText(statuses.get(snapshot["status"], statuses["unavailable"]))
        if snapshot["status"] != "ready":
            self.personal_layout.addStretch()
            return
        days = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
        schedule = [f"{days.get(item.get('weekday'), 'День не указан')} · {item.get('start', '')}–{item.get('end', '')}\n{item.get('title', '')}" for item in snapshot["schedule"]]
        assignments = [f"{item.get('title', '')}\n{item.get('course') or 'Без предмета'} · {display_due(item.get('due_at'))}" for item in snapshot["assignments"]]
        memory = [f"{item.get('key', '')}\n{item.get('value', '')}" for item in snapshot["memory"]]
        tasks = [str(item.get("title", "")) for item in snapshot["tasks"]]
        sections = [("schedule", "Расписание из копии", schedule), ("assignments", "Открытые задания на дату копии", assignments), ("memory", "Что было сохранено обо мне", memory)]
        if tasks:
            sections.append(("tasks", "Другие сохранённые задачи", tasks))
        for key, title, items in sections:
            card = QFrame()
            card.setObjectName("card")
            content = QVBoxLayout(card)
            content.setContentsMargins(18, 15, 18, 18)
            content.setSpacing(10)
            content.addWidget(label(title, "section"))
            if key in snapshot["unavailable"]:
                content.addWidget(label("Этот раздел отсутствует в сохранённой копии.", "muted", True))
            elif not items:
                content.addWidget(label("Нет сохранённых записей.", "muted"))
            for text in items:
                body = label(text, wrap=True)
                body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                content.addWidget(body)
            if key in snapshot["truncated"]:
                content.addWidget(label("Показаны первые 200 записей этого раздела.", "muted", True))
            self.personal_layout.addWidget(card)
        self.personal_layout.addWidget(label("Время сроков показано для Кызылорды · UTC+5. Исходная копия открывается только для чтения. Эти записи не запускают команды и не передаются ИИ.", "muted", True))
        self.personal_layout.addStretch()

    def navigate(self, index):
        self._view_index = index
        self.pages.setCurrentIndex(index)
        for (target, _title), nav in zip(self.nav_targets, self.nav_buttons):
            nav.setChecked(target == index)
        if hasattr(self, "voice"):
            self.refresh()

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()

    def _message(self, role, text):
        card = QFrame()
        card.setObjectName("message")
        content = QVBoxLayout(card)
        content.setContentsMargins(16, 12, 16, 14)
        content.setSpacing(7)
        content.addWidget(label("ВЫ" if role == "user" else "JARVIS", "eyebrow"))
        displayed = str(text)
        if role == "assistant" and displayed.startswith("Локальная модель: "):
            displayed = displayed[len("Локальная модель: "):]
        body = label(displayed, wrap=True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content.addWidget(body)
        return card

    def refresh(self):
        snapshot = self.core.snapshot()
        self.preferences = snapshot.get("preferences", {})
        reminder_count = len(snapshot.get("reminders", []))
        self.timer_hint.setVisible(bool(reminder_count))
        self.timer_hint.setText(f"Активные таймеры и напоминания: {reminder_count}")
        self._clear_layout(self.history_layout)
        self.history_layout.addStretch()
        recent = snapshot.get("recent", [])[-12:]
        if not recent:
            recent = [{"role": "assistant", "text": "Я готов помочь. Сохраню задачу или заметку, открою нужное приложение и подскажу следующий шаг. Напишите команду — или включите микрофон."}]
        self._rendered_messages = {(item.get("role"), item.get("text")) for item in recent}
        for item in recent:
            self.history_layout.addWidget(self._message(item.get("role", "assistant"), item.get("text", "")))
        QTimer.singleShot(0, lambda: self.history_scroll.verticalScrollBar().setValue(self.history_scroll.verticalScrollBar().maximum()))
        self.refresh_tasks(snapshot)
        self.refresh_notes(snapshot=snapshot)
        self.plan.refresh()
        self.plan.refresh_evidence(snapshot.get("observations", {}))
        self.controls.refresh()
        self._sync_organizer_preferences()
        self._set_checked_silently(self.activity_monitor_toggle, self.preferences.get("activity_monitor_enabled", False))
        self._refresh_usage_status()
        if hasattr(self, "voice"):
            state = self.voice.available()
            self._update_voice_backend_status(state)
            self.model_status.setText("Офлайн-модель установлена. Можно включить микрофон." if state.get("model") else "Для голосового вызова нужна русская офлайн-модель.")
            self.model_button.setEnabled(not state.get("model", False))

    def refresh_tasks(self, snapshot=None):
        snapshot = snapshot or self.core.snapshot()
        self._clear_layout(self.task_layout)
        tasks = snapshot.get("tasks", [])
        if not tasks:
            self.task_layout.addWidget(label("Пока нет задач. Добавьте одну — этого достаточно для начала.", "muted", True))
        for item in tasks:
            card = QFrame()
            card.setObjectName("card")
            row = QHBoxLayout(card)
            row.setContentsMargins(16, 12, 16, 12)
            done = bool(item.get("done"))
            title = label(str(item.get("title", "")), wrap=True)
            title.setMinimumWidth(200)
            if done:
                title.setStyleSheet("color: #71889e;")
            row.addWidget(title, 1)
            control = button("Готово" if done else "Отметить", lambda _checked=False, task_id=item["id"]: self.submit(f"готово {task_id}"), "quiet")
            control.setObjectName(f"completeTask_{item['id']}")
            control.setEnabled(not done and not self.busy)
            row.addWidget(control)
            self.task_layout.addWidget(card)
        self.task_layout.addStretch()

    def refresh_notes(self, _text=None, snapshot=None):
        snapshot = snapshot or self.core.snapshot()
        query = self.note_search.text().strip().casefold()
        notes = [item for item in snapshot.get("notes", []) if query in str(item.get("text", "")).casefold()]
        self._clear_layout(self.note_layout)
        if not notes:
            self.note_layout.addWidget(label("Ничего не найдено." if query else "Здесь будут ваши сохранённые мысли и материалы.", "muted", True))
        for item in notes:
            card = QFrame()
            card.setObjectName("card")
            content = QVBoxLayout(card)
            content.setContentsMargins(16, 13, 16, 15)
            content.addWidget(label(f"ЗАМЕТКА {item['id']}", "eyebrow"))
            body = label(str(item.get("text", "")), wrap=True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            content.addWidget(body)
            self.note_layout.addWidget(card)
        self.note_layout.addStretch()

    def _refresh_usage_status(self):
        reader = getattr(self.core, "usage_snapshot", None)
        if not callable(reader):
            return
        try:
            data = reader()
            names = ("requests", "successful", "failed", "known_input_tokens", "known_output_tokens",
                     "known_input_requests", "known_output_requests", "unknown_usage_requests")
            if not isinstance(data, dict) or any(type(data.get(key)) is not int or data[key] < 0 for key in names):
                raise ValueError("Invalid usage counters")
            inputs = str(data["known_input_tokens"]) if data["known_input_requests"] else "—"
            outputs = str(data["known_output_tokens"]) if data["known_output_requests"] else "—"
            latency = data.get("mean_latency_ms")
            timing = (f"{latency / 1000:.2f} с" if type(latency) in {int, float} and math.isfinite(latency)
                      and latency >= 0 else "нет данных")
            self.usage_status.setText(
                f"Локальный ИИ · сегодня · UTC+5\nЗапросы: {data['requests']} · Успешно: {data['successful']} · Ошибки: {data['failed']}\n"
                f"Известные токены: вход {inputs} / выход {outputs} · Неполные данные: {data['unknown_usage_requests']}\n"
                f"Среднее время ответа: {timing}")
        except Exception:
            self.usage_status.setText("Статистика локального ИИ недоступна. Основные команды продолжают работать.")

    def _run_integration(self, function, done):
        """Keep optional provider I/O off both Qt and the command worker."""
        if self._integration_busy:
            return False
        self._integration_busy = True
        worker = Worker(function)
        self._integration_workers.add(worker)

        def finished(value):
            self._integration_workers.discard(worker)
            self._integration_busy = False
            done(value)
            if self._quitting:
                self._stop_phone_integrations()

        def failed(_error_class):
            self._integration_workers.discard(worker)
            self._integration_busy = False
            self._phone_state = {"reason": "unavailable"}
            self._delivery_state = {"telegram": "unavailable", "calls": "unavailable"}
            if self._quitting:
                self._stop_phone_integrations()
            else:
                self._update_phone_status()

        worker.signals.done.connect(finished)
        worker.signals.failed.connect(failed)
        self._integration_pool.start(worker)
        return True

    def _start_phone_integrations(self):
        def initialize():
            from desktop.phone import OrganizerPhoneAdapter, PhoneServer
            from desktop.phone_delivery import PhoneDelivery
            from desktop.remote_control import RemoteController
            controller = RemoteController(self.core.store.state_dir,
                action_handler=lambda action, **kwargs: self.core.request_control(action, source="remote", **kwargs),
                project_catalog=self.core.projects, app_catalog=self.core.apps,
                approved=self._remote_approved, project_editor=None)
            server = PhoneServer(api=OrganizerPhoneAdapter(self.core.organizer),
                                 state_dir=self.core.store.state_dir, remote=controller)
            delivery = PhoneDelivery(self.core.store.state_dir)
            if not self._quitting:
                server.start()  # An absent/invalid opt-in leaves this disabled.
            return server, delivery, server.status(), delivery.status(), controller

        def ready(result):
            (self._phone_server, self._phone_delivery, self._phone_state,
             self._delivery_state, self._remote_controller) = result
            if not self._quitting:
                self._update_phone_status()
                self.controls.refresh()
                self._schedule_phone_work()

        self.phone_status.setText("Проверяю настройки локального моста…")
        self._run_integration(initialize, ready)

    def _remote_approved(self):
        # Pairing is a durable, separate permission. A phone away from home
        # must not depend on the short-lived local Windows Hello grant.
        try:
            return (not self._quitting and not self.core._closed.is_set()
                    and self.core.store.preferences().get("remote_control_approved") is True)
        except Exception:
            return False

    def _remote_status(self):
        controller = self._remote_controller
        if controller is None:
            return {"status": "not_configured", "approved": False}
        state = controller.status()
        return {"status": ("ready" if self._phone_state.get("running") else "unavailable")
                if state.get("configured") else "not_configured",
                "approved": state.get("approved") is True}

    def _remote_setup_authorized(self):
        try:
            return (not self._quitting and self.core.owner.enabled is True
                    and self.core.owner.allowed() is True)
        except Exception:
            return False

    def _apply_remote_setup(self, origin):
        """Commit an explicitly confirmed private route, then restart loopback.

        Called by the setup panel only after a real Serve inspection. All file
        and server work stays off Qt; neither a model nor imported text calls it.
        """
        from desktop.remote_setup_ui import _origin
        if (not self._integrations_enabled or self._integration_busy or self._remote_configuration_pending
                or not self._remote_setup_authorized() or _origin(origin) is None or _origin(origin) != origin):
            return False
        self._remote_configuration_pending = True

        def configure():
            try:
                import os
                from desktop.runtime_env import load_phone_environment, update_phone_environment
                result = update_phone_environment(self.core.store.state_dir, origin=origin,
                    phone_enabled=True, remote_enabled=True, authorization_guard=self._remote_setup_authorized)
                if result not in {"updated", "preserved"} or not self._remote_setup_authorized():
                    return False
                load_phone_environment(self.core.store.state_dir)
                os.environ.update({"JARVIS_PHONE_ENABLED": "1", "JARVIS_REMOTE_ENABLED": "1",
                                   "JARVIS_PHONE_ORIGIN": origin})
                if self._phone_server is not None:
                    self._retired_phone_servers.append(self._phone_server)
                    self._phone_server.stop()
                return True
            except Exception:
                return False

        def configured(accepted):
            self._remote_configuration_pending = False
            if self._quitting:
                return
            if accepted is True:
                self._start_phone_integrations()
                self.remote_setup.status_label.setText(
                    "Частный HTTPS сохранён. Разреши удалённое управление выше, затем подключи iPhone. Получение команд телефоном ещё не проверено.")
            else:
                self.remote_setup.status_label.setText(
                    "Не удалось подтвердить сохранение настройки. Проверь владельца и повтори настройку; готовность iPhone не подтверждена.")
        started = self._run_integration(configure, configured)
        if not started:
            self._remote_configuration_pending = False
        return started

    def _update_phone_status(self):
        # Project fixed labels only. Provider dictionaries can contain URLs;
        # none of their values are copied verbatim into the interface or logs.
        if self._phone_state.get("running"):
            message = ("Локальный мост готов; защищённое подключение ещё не проверено."
                       if self._phone_state.get("transport") == "https_proxy_not_verified" else
                       "Локальный мост готов; для iPhone нужно зашифрованное подключение.")
        elif self._phone_state.get("enabled") or self._phone_state.get("reason") == "unavailable":
            message = "Локальный мост недоступен. Проверьте его настройку по инструкции."
        else:
            message = "Локальный мост не настроен. Основные функции JARVIS работают без телефона."
        self.phone_status.setText(message)
        telegram = ("Telegram: настроен для отправки; получение не подтверждено."
                    if self._delivery_state.get("telegram") == "configured" else "Telegram: не подключён.")
        calls = ("Звонки: настроены; ответ и прослушивание не подтверждены."
                 if self._delivery_state.get("calls") == "configured" else "Звонки: не подключены.")
        self.phone_delivery_status.setText(telegram + "\n" + calls)

    def _open_phone_setup(self):
        self.navigate(self.CONTROLS_PAGE)
        self.remote_setup.refresh()

    def _poll_phone_integrations(self):
        if self._quitting or not self._integrations_enabled:
            return
        if self._phone_state.get("running") and self._view_index == self.PLAN_PAGE:
            # PlanPanel preserves unsaved item/routine editors. Phone mutations
            # only refresh the actual dated plan, not the whole chat or form.
            self.plan.refresh()
        self._schedule_phone_work()

    def _schedule_phone_work(self):
        if self._quitting or self._integration_busy or self._phone_delivery is None:
            return
        configured = any(self._delivery_state.get(key) == "configured" for key in ("telegram", "calls"))
        if not configured:
            self._phone_notifications.clear()
            return
        pending = self._delivery_state.get("counts", {}).get("pending", 0)
        if not self._phone_notifications and not (type(pending) is int and pending > 0):
            return
        notifications, self._phone_notifications = self._phone_notifications, []
        delivery = self._phone_delivery

        def deliver():
            state = delivery.status()
            for event_id, text, validator, evening in notifications:
                if validator():
                    delivery.enqueue(event_id, text, is_current=validator,
                                     call=evening and state.get("calls") == "configured")
            state = delivery.status()
            if (any(state.get(key) == "configured" for key in ("telegram", "calls"))
                    and state.get("counts", {}).get("pending", 0) > 0):
                state = delivery.process_pending()
            return state

        def delivered(state):
            self._delivery_state = state
            if not self._quitting:
                self._update_phone_status()
                if self._phone_notifications:
                    self._schedule_phone_work()

        self._run_integration(deliver, delivered)

    def _stop_phone_integrations(self):
        if self._phone_stop_complete or self._integration_busy:
            return
        if self._phone_server is None:
            self._phone_stop_complete = True
            return

        def stopped(_result):
            self._phone_stop_complete = True

        def stop():
            try:
                self._phone_server.stop()
            except Exception:
                pass  # Optional cleanup must not trap the window in shutdown.

        self._run_integration(stop, stopped)

    def _multilingual_changed(self, enabled):
        previous = bool(self.preferences.get("multilingual_stt_enabled", False))
        was_listening = self.listening or self._capture_requested
        if was_listening:
            self.stop_listening()  # Invalidate callbacks from the old ASR session.
        if not self.save_preference("multilingual_stt_enabled", enabled):
            self._set_checked_silently(self.multilingual_toggle, previous)
        if was_listening:
            self.start_listening()

    def save_preference(self, key, value):
        try:
            self.preferences = self.core.store.set_preferences({key: value})
        except ValueError:
            self.status.setText("Проверьте значение настройки")
            return False
        except Exception:
            self.status.setText("Не удалось сохранить настройку")
            return False
        if key == "speech_enabled" and hasattr(self, "voice"):
            self.voice.speech_enabled = bool(value)
            self.voice_sample.setEnabled(bool(value))
        if key in {"speech_enabled", "voice_engine", "voice_character", "voice_rate", "multilingual_stt_enabled"} and hasattr(self, "voice"):
            apply_preferences = getattr(self.voice, "apply_preferences", None)
            if callable(apply_preferences):
                apply_preferences(dict(self.preferences))
            prepare = getattr(self.voice, "prepare", None)
            if callable(prepare):
                prepare()
        return True

    def _voice_engine_changed(self, _index):
        personal = self.voice_engine.currentData() == "expressive"
        for widget in (self.voice_character_label, self.voice_character):
            widget.setVisible(personal)
        self.voice_personal_license.setVisible(self.voice_engine.currentData() in {"expressive", "clone"})
        self.save_preference("voice_engine", self.voice_engine.currentData())

    def _activity_monitor_changed(self, enabled):
        previous = bool(self.preferences.get("activity_monitor_enabled", False))
        if self.save_preference("activity_monitor_enabled", enabled):
            self.core.reset_observations()
            self.plan.refresh_evidence(self.core.observation_summary())
        else:
            self._set_checked_silently(self.activity_monitor_toggle, previous)

    def _plan_changed(self):
        self.refresh()
        self._poll_organizer_events("interaction")

    def _organizer_preference_changed(self, key, value):
        if self.save_preference(key, value):
            self._sync_organizer_preferences()
            self._poll_organizer_events("interaction")

    def _sync_organizer_preferences(self):
        controls = [("organizer_enabled", self.organizer_toggle, True),
                    ("morning_brief_enabled", self.morning_toggle, True),
                    ("evening_plan_enabled", self.evening_toggle, True),
                    ("organizer_paused", self.organizer_pause_toggle, False),
                    ("organizer_dnd", self.organizer_dnd_toggle, False)]
        for key, control, default in controls:
            self._set_checked_silently(control, self.preferences.get(key, default))
        blocked = self.evening_time.blockSignals(True)
        self.evening_time.setTime(QTime.fromString(self.preferences.get("evening_plan_time", "22:00"), "HH:mm"))
        self.evening_time.blockSignals(blocked)
        self.evening_time.setEnabled(self.preferences.get("evening_plan_enabled", True))

    @staticmethod
    def _set_checked_silently(control, value):
        blocked = control.blockSignals(True)
        control.setChecked(bool(value))
        control.blockSignals(blocked)

    def _auto_listen_changed(self, enabled):
        previous = bool(self.preferences.get("voice_enabled", False))
        if not enabled:
            self.stop_listening()
        if not self.save_preference("voice_enabled", enabled):
            self._set_checked_silently(self.auto_listen_toggle, previous)
            if not enabled:
                self.status.setText("Микрофон выключен, но настройку автослушания не удалось сохранить")
            return
        if enabled:
            self.start_listening()

    def _configure_startup_setting(self, enabled):
        if self._startup_pending:
            return
        previous = bool(self.preferences.get("launch_at_login", False))
        self._startup_pending = True
        self.launch_at_login_toggle.setEnabled(False)
        self.status.setText("Настраиваю запуск при входе в Windows…")
        def configure():
            from desktop.startup import set_startup_enabled
            result = set_startup_enabled(enabled)
            if not result.success:
                return False, result.message, None
            try:
                preferences = self.core.store.set_preferences({"launch_at_login": enabled})
            except Exception:
                try:
                    reverted = set_startup_enabled(previous).success
                except Exception:
                    reverted = False
                message = ("Автозапуск возвращён к прежнему состоянию: настройку не удалось сохранить." if reverted else
                           "Не удалось сохранить настройку и проверить автозапуск. Проверьте его перед повторным входом.")
                return False, message, None
            return True, result.message, preferences
        self._run(configure, lambda result: self._startup_configured(enabled, previous, result),
                  on_error=lambda _error: self._startup_configured(enabled, previous, (False, "Не удалось подтвердить настройку автозапуска.", None)))

    def _startup_configured(self, enabled, previous, result):
        self._startup_pending = False
        success, message, preferences = result
        if success:
            self.preferences = preferences
        if self._quitting:
            return
        self._set_checked_silently(self.launch_at_login_toggle, enabled if success else previous)
        self.launch_at_login_toggle.setEnabled(True)
        self.status.setText(str(message)[:300])

    def _sample_voice(self):
        if not self.preferences.get("speech_enabled", True):
            self.status.setText("Включите голосовые ответы для проверки")
            return
        self._commit_voice_rate()
        self.voice.say("К вашим услугам, сэр. С чего начнём?")

    def _update_voice_backend_status(self, state=None):
        if not hasattr(self, "voice"):
            return
        state = self.voice.available() if state is None else state
        labels = {
            "not_prepared": "Голос ещё подготавливается; ответы доступны текстом.",
            "neural": "Используется базовый голос Дмитрий (Piper).",
            "windows": "Используется установленный голос Windows.",
            "expressive": "Используется выразительный голос для личного использования.",
            "clone": "Используется голос по личному образцу (личное использование).",
            "clone_warming": "Голос по образцу готовится. Ответ ожидает озвучки.",
            "clone_unavailable": "Голос по образцу недоступен. Ответы доступны текстом; другой тембр не подставляется.",
            "personal_cue": "Личное приветствие из записи.",
            "unavailable": "Озвучивание недоступно; ответ доступен текстом.",
        }
        self.voice_backend_status.setText(labels.get(state.get("speech_backend"), "Источник голоса определяется при подготовке."))

    def _commit_voice_rate(self):
        self._voice_rate_timer.stop()
        value = self.voice_rate.value()
        if value != self.preferences.get("voice_rate", 185):
            self.save_preference("voice_rate", value)

    def _clear_stream_preview(self):
        self._active_stream_id = None
        self._stream_timer.stop()
        self._stream_buffer = ""
        self.stream_text.setText("")
        self.stream_preview.hide()

    def _receive_stream_chunk(self, request_id, delta):
        if self._quitting or not self.busy or request_id != self._active_stream_id:
            return
        if not isinstance(delta, str) or not delta:
            return
        remaining = self.MAX_PREVIEW_CHARS - len(self._stream_buffer)
        if remaining <= 0:
            return
        self._stream_buffer += delta[:remaining]
        if not self._stream_timer.isActive():
            self._stream_timer.start()

    def _flush_stream_preview(self):
        if self._quitting or not self.busy or self._active_stream_id is None or not self._stream_buffer:
            return
        # This is literal, provisional display only: no persistence, speech,
        # confirmation metadata or command dispatch can originate here.
        self.stream_text.setText(self._stream_buffer)
        self.stream_preview.show()
        self.status.setText("Формирую ответ…")

    def _poll_reminders(self):
        if self._quitting:
            return
        sample = getattr(self.core, "poll_observations", None)
        if callable(sample):
            changed = sample()
            if changed and self._view_index == self.PLAN_PAGE:
                self.plan.refresh_evidence(self.core.observation_summary())
        if self.busy:
            return
        due = getattr(self.core, "due_reminders", None)
        try:
            replies = due() if callable(due) else []
        except Exception:
            self.status.setText("Не удалось проверить напоминания")
            replies = []
        self._deliver_notifications(replies)
        self._poll_organizer_events("tick")

    def _poll_organizer_events(self, trigger="tick"):
        if self._quitting or self.busy:
            return
        due = getattr(self.core, "due_events", None)
        if not callable(due):
            return
        try:
            replies = due(trigger=trigger)
        except Exception:
            self.status.setText("Не удалось подготовить обращение по плану")
            return
        self._deliver_notifications(replies)

    def _deliver_notifications(self, replies):
        if not replies or self._quitting:
            return
        self.refresh()
        for reply in replies:
            event = getattr(reply, "organizer_event", None)
            def is_current(message=reply):
                if self._quitting:
                    return False
                checker = getattr(self.core, "event_is_current", None)
                if not callable(checker):
                    return False
                try:
                    return bool(checker(message))
                except Exception:
                    return False
            if event is not None and not is_current():
                continue
            if (self._integrations_enabled and isinstance(event, dict)
                    and isinstance(event.get("id"), str)):
                # Only explicit organizer events are forwarded. No commands,
                # notes, model previews or general chat enter this outbox.
                self._phone_notifications.append((event["id"], reply.text, is_current, event.get("kind") == "evening"))
                self._phone_notifications = self._phone_notifications[-100:]
            if self.tray:
                title = "JARVIS · план" if event is not None else "JARVIS · напоминание"
                self.tray.showMessage(title, reply.text[:500], QSystemTrayIcon.MessageIcon.Information, 8000)
            if reply.speak and self.preferences.get("speech_enabled", True):
                say_event = getattr(self.voice, "say_event", None)
                if event is not None and callable(say_event):
                    say_event(reply.text, is_current=is_current)
                else:
                    # Legacy/fake voices still receive only events validated
                    # immediately above; current speech backends recheck at play.
                    self.voice.say(reply.text)
        self._schedule_phone_work()

    def _run(self, function, done, *, on_error=None):
        worker = Worker(function)
        self._workers.add(worker)
        def finished(value):
            self._workers.discard(worker)
            done(value)
        def failed(_error_class):
            self._workers.discard(worker)
            if self._quitting:
                return
            if on_error is not None:
                on_error(_error_class)
                return
            from desktop.core import Reply
            self._clear_stream_preview()
            self.busy = False
            self.send_button.setEnabled(True)
            self.command.setEnabled(True)
            self._confirmation = None
            self.confirmation.hide()
            failure = Reply("Не удалось выполнить действие. Повтори команду или напиши её иначе.")
            # A database failure must not call refresh()/record again from its
            # error handler. The fixed feedback uses only the cached UI state.
            self.history_layout.addWidget(self._message("assistant", failure.text))
            self._speak_reply(failure)
            self.mic_button.setEnabled(True)
            self.model_button.setEnabled(True)
            self.status.setText("Не удалось выполнить действие")
        worker.signals.done.connect(finished)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)

    def submit_current(self):
        text = self.command.text().strip()
        if text:
            self.command.clear()
            self.submit(text)

    def submit(self, text, source="typed"):
        if self.busy or not text.strip():
            return
        interrupt = getattr(self.voice, "interrupt", None)
        if callable(interrupt):
            interrupt()
        self.busy = True
        self.command.setEnabled(False)
        self.send_button.setEnabled(False)
        self.status.setText("Выполняю…")
        self._clear_stream_preview()
        self._request_serial += 1
        request_id = self._request_serial
        self._active_stream_id = request_id
        # Echo the original request immediately. The next persisted refresh
        # replaces this temporary card without duplicating database records.
        self.history_layout.addWidget(self._message("user", text))
        QTimer.singleShot(0, lambda: self.history_scroll.verticalScrollBar().setValue(self.history_scroll.verticalScrollBar().maximum()))
        def on_chunk(delta):
            if self._quitting:
                return False
            if isinstance(delta, str) and delta:
                self.events.stream_chunk.emit(request_id, delta[:self.MAX_PREVIEW_CHARS])
        self._run(lambda: self.core.handle(text, source=source, on_chunk=on_chunk), self._reply)

    def _reply(self, reply):
        if self._quitting:
            return
        self._clear_stream_preview()
        self.busy = False
        self.command.setEnabled(True)
        self.send_button.setEnabled(True)
        self._confirmation = reply.confirmation_id
        if self._confirmation:
            self.confirm_text.setText(reply.text)
            self.confirmation.show()
            self.navigate(0)
        else:
            self.confirmation.hide()
        self.refresh()
        if ("assistant", reply.text) not in self._rendered_messages:
            # Consent/validation feedback deliberately need not persist any
            # user request. It must still be visible when speech is disabled.
            self.history_layout.addWidget(self._message("assistant", reply.text))
        self.status.setText("Ожидаю «Джарвис»" if self.listening else self._capture_error or "Микрофон выключен")
        self._speak_reply(reply)
        self.command.setFocus()
        self._poll_organizer_events("interaction")

    def _speak_reply(self, reply):
        if not self._quitting and reply.speak and self.preferences.get("speech_enabled", True):
            play_cue = getattr(self.voice, "play_cue", None)
            queued = False
            if getattr(reply, "media_cue", None) == "welcome_home" and callable(play_cue):
                try:
                    queued = play_cue("welcome_home") is True
                except Exception:
                    pass
            if not queued:
                self.voice.say(reply.text)

    def confirm_action(self):
        if not self._confirmation or self.busy:
            return
        token, self._confirmation = self._confirmation, None
        self.confirmation.hide()
        self.busy = True
        self.command.setEnabled(False)
        self.send_button.setEnabled(False)
        self._run(lambda: self.core.confirm(token, source="button"), self._reply)

    def cancel_action(self):
        if not self._confirmation or self.busy:
            return
        token, self._confirmation = self._confirmation, None
        self.confirmation.hide()
        self.busy = True
        self._run(lambda: self.core.cancel(token), self._reply)

    def add_task(self):
        text = self.task_input.text().strip()
        if text and not self.busy:
            self.task_input.clear()
            self.submit("добавь задачу " + text)

    def add_note(self):
        text = self.note_input.toPlainText().strip()
        if text and not self.busy:
            if len(text) > 5000:
                self.status.setText("Заметка слишком длинная: максимум 5000 символов")
                return
            self.note_input.clear()
            self.submit("запомни " + text)

    def _voice_command(self, text):
        if self._quitting or not self.listening:
            return
        if self.busy:
            self.status.setText("Дождитесь завершения текущего действия")
            return
        self.navigate(0)
        self.submit(text, source="voice")

    def _voice_state(self, text):
        if self._quitting:
            return
        if text.startswith("Голос недоступен ("):
            # Model/audio initialization is asynchronous. A successful start()
            # only means the listener thread started, not that capture works.
            self._capture_failed = True
            self._capture_error = text
            self._capture_requested = False
            self.listening = False
            self.orb.active = False
            self.orb.update()
            self.mic_button.setText("Микрофон")
            self.mic_button.setEnabled(True)
        self.status.setText(text)
        self._update_voice_backend_status()
        if self.tray:
            self.tray.setToolTip("JARVIS · " + text[:90])

    def toggle_listening(self):
        if self.listening:
            self.stop_listening()
            return
        state = self.voice.available()
        if not state.get("ready"):
            self.navigate(self.SETTINGS_PAGE)
            reasons = {
                "dependencies_missing": "Голосовые компоненты пока не установлены",
                "model_missing": "Установите голосовую модель в настройках",
                "microphone_missing": "Микрофон не найден — проверьте настройки Windows",
            }
            self.status.setText(reasons.get(state.get("reason"), "Голос пока недоступен — можно писать команды"))
            return
        self.mic_button.setEnabled(False)
        self._capture_requested = True
        self._capture_session += 1
        session_id = self._capture_session
        self.voice.on_text = self._recognition_callback(session_id)
        self._capture_failed = False
        self._capture_error = None
        self.status.setText("Запускаю микрофон…")
        def start_capture():
            if self._quitting or session_id != self._capture_session:
                return False
            return self.voice.start()
        self._run(start_capture, lambda result: self._microphone_started(result, session_id))

    def start_listening(self):
        if self._quitting or self.listening or self._capture_requested:
            return
        self.toggle_listening()

    def _microphone_started(self, result, session_id=None):
        if self._quitting or (session_id is not None and session_id != self._capture_session):
            if not self._capture_requested:
                self.voice.stop()
            return
        self.mic_button.setEnabled(True)
        self.listening = bool(result) and not self._capture_failed
        self._capture_requested = self.listening
        self.orb.active = self.listening
        self.orb.update()
        self.mic_button.setText("Выключить микрофон" if self.listening else "Микрофон")
        if self.listening:
            self.status.setText("Ожидаю «Джарвис»")
        elif not self._capture_failed and self.status.text() == "Запускаю микрофон…":
            self.status.setText("Микрофон не запущен. Повторите включение, когда прежний сеанс завершится.")

    def stop_listening(self):
        self._capture_requested = False
        self._capture_session += 1
        self.listening = False
        self.voice.stop()
        self._capture_error = None
        self.listening = False
        self.orb.active = False
        self.orb.update()
        self.mic_button.setText("Микрофон")
        self.mic_button.setEnabled(True)
        self.status.setText("Микрофон выключен")

    def install_model(self):
        self.model_button.setEnabled(False)
        self.model_status.setText("Подготавливаю загрузку…")
        from desktop.model_setup import default_model_dir, install_model
        destination = self.model_dir or default_model_dir()
        self._run(lambda: install_model(destination, progress_callback=self.events.progress.emit), self._model_installed)

    def _model_progress(self, text):
        if self._quitting:
            return
        self.model_status.setText(text)

    def _model_installed(self, path):
        if self._quitting:
            return
        self.stop_listening()
        close_voice = getattr(self.voice, "close", None)
        if callable(close_voice):
            close_voice()
        self.model_dir = Path(path)
        self.voice = self._new_voice()
        self.refresh()
        self.status.setText("Модель готова. Включите микрофон, когда будете готовы.")

    def restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_application(self):
        self._quitting = True
        self.close()

    def nativeEvent(self, event_type, message):
        # Session lock/disconnect invalidates the in-memory Hello grant even
        # when the assistant has no focus. No biometric material is accessed.
        if self._session_notifications_registered:
            try:
                from ctypes.wintypes import MSG
                native = MSG.from_address(int(message))
                if native.message == 0x02B1:  # WM_WTSSESSION_CHANGE
                    self.core.owner.handle_session_change(int(native.wParam))
                    self.controls._poll_owner()
            except (TypeError, ValueError, OSError):
                self.core.owner.revoke()
        return super().nativeEvent(event_type, message)

    def _finish_close(self):
        if (self.pool.activeThreadCount() == 0 and not self._integration_workers
                and self._integration_pool.activeThreadCount() == 0
                and self._phone_stop_complete and self.controls.shutdown_complete
                and self.remote_setup.shutdown_complete
                and self._phone_handlers_drained()):
            self._close_timer.stop()
            self.close()

    def _phone_handlers_drained(self):
        self._retired_phone_servers = [server for server in self._retired_phone_servers
                                      if not getattr(server, "shutdown_complete", True)]
        return (not self._retired_phone_servers
                and (self._phone_server is None or getattr(self._phone_server, "shutdown_complete", True)))

    def closeEvent(self, event):
        if not self._quitting and self.tray and self.preferences.get("close_to_tray", True):
            self.hide()
            event.ignore()
            if not self._tray_notice_shown:
                self.tray.showMessage("JARVIS остаётся в трее", "Откройте меню значка, чтобы выключить микрофон или завершить работу.", QSystemTrayIcon.MessageIcon.Information, 4000)
                self._tray_notice_shown = True
            return
        self._quitting = True
        self.controls.shutdown()
        self.remote_setup.shutdown()
        prevent = getattr(self._phone_server, "prevent_requests", None)
        if callable(prevent):
            prevent()
        if self._session_notifications_registered:
            from desktop.owner_auth import unregister_session_notifications
            unregister_session_notifications(int(self.winId()))
            self._session_notifications_registered = False
        self._reminder_timer.stop()
        self._integration_timer.stop()
        self._phone_notifications.clear()
        self._stop_phone_integrations()
        self._clear_stream_preview()
        self.stop_listening()
        if self._voice_rate_timer.isActive():
            self._voice_rate_timer.stop()
            # Save the final slider position without queueing voice warmup
            # while the application is shutting down.
            try:
                self.preferences = self.core.store.set_preferences({"voice_rate": self.voice_rate.value()})
            except Exception:
                # An optional final preference must not keep capture running
                # or prevent shutdown when local storage is unavailable.
                pass
        # Stop queued work immediately; let an already-running bounded adapter
        # finish without freezing the UI or closing its database under it.
        self.pool.clear()
        if (self.pool.activeThreadCount() or self._integration_workers or self._integration_pool.activeThreadCount()
                or not self._phone_stop_complete or not self.controls.shutdown_complete
                or not self.remote_setup.shutdown_complete
                or not self._phone_handlers_drained()):
            self.setEnabled(False)
            self.status.setText("Завершаю текущее действие. Микрофон выключен.")
            self._close_timer.start()
            event.ignore()
            return
        if not self._resources_closed:
            close_voice = getattr(self.voice, "close", None)
            if callable(close_voice):
                close_voice()
            self.core.close()
            self._resources_closed = True
        if self.tray:
            self.tray.hide()
        event.accept()
        QApplication.instance().quit()
