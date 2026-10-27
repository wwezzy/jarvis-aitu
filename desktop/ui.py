"""Local, explicitly activated desktop interface. No cloud or microphone at startup."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QKeySequence, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget,
    QSystemTrayIcon, QVBoxLayout, QWidget,
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
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 4px 0; }
QScrollBar::handle:vertical { background: #294157; border-radius: 3px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCheckBox { spacing: 10px; padding: 8px 0; }
QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #4d6880; border-radius: 4px; background: #0b1523; }
QCheckBox::indicator:checked { background: #79e2d2; border-color: #79e2d2; }
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
    recognized = Signal(str)
    voice_state = Signal(str)
    progress = Signal(str)


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


class MainWindow(QMainWindow):
    SETTINGS_PAGE = 4

    def __init__(self, core, voice_factory=None, model_dir: Path | None = None, enable_tray: bool = True, personal_path: Path | None = None):
        super().__init__()
        load_application_font()
        self.core = core
        self.model_dir = model_dir
        self.personal_path = personal_path
        self.events = Events()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._workers = set()
        self.busy = False
        self.listening = False
        self._quitting = False
        self._capture_failed = False
        self._capture_error = None
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self._finish_close)
        self._confirmation = None
        self._tray_notice_shown = False
        self._view_index = 0
        self.tray = None
        self.voice_factory = voice_factory
        self.preferences = self.core.snapshot().get("preferences", {})
        self.setWindowTitle("JARVIS — личный ассистент")
        self.setWindowIcon(app_icon())
        self.resize(1120, 790)
        self.setMinimumSize(860, 620)
        self.setStyleSheet(STYLE)
        self._build()
        self.events.recognized.connect(self._voice_command)
        self.events.voice_state.connect(self._voice_state)
        self.events.progress.connect(self._model_progress)
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

    def _new_voice(self):
        factory = self.voice_factory
        if factory is None:
            from desktop.voice import VoiceService
            factory = VoiceService
        voice = factory(on_text=self.events.recognized.emit, on_state=self.events.voice_state.emit, model_dir=self.model_dir)
        voice.speech_enabled = bool(self.preferences.get("speech_enabled", True))
        return voice

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
        for index, text in enumerate(["Ассистент", "Мои задачи", "Заметки", "Мои данные", "Настройки"]):
            nav = button(text, lambda _checked=False, value=index: self.navigate(value), "nav")
            nav.setCheckable(True)
            self.nav_buttons.append(nav)
            side.addWidget(nav)
        side.addStretch()
        side.addWidget(label("НА ЭТОМ КОМПЬЮТЕРЕ", "eyebrow"))
        side.addWidget(label("Задачи и заметки доступны\nбез интернета.", "muted", True))
        side.addSpacing(15)
        side.addWidget(label("DESKTOP 4.2", "eyebrow"))
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
        self.tray_toggle = QCheckBox("При закрытии оставлять JARVIS в трее")
        self.tray_toggle.setChecked(bool(self.preferences.get("close_to_tray", True)))
        self.tray_toggle.toggled.connect(lambda value: self.save_preference("close_to_tray", value))
        content.addWidget(self.tray_toggle)
        content.addWidget(label("Микрофон всегда выключен при запуске. Включите его кнопкой, скажите «Джарвис», затем команду. Голосовое выключение компьютера потребует нажатия подтверждения.", "muted", True))
        content.addSpacing(8)
        self.model_status = label("Офлайн-модель распознавания устанавливается отдельно.", "muted", True)
        content.addWidget(self.model_status)
        self.model_button = button("Установить русскую голосовую модель", self.install_model)
        self.model_button.setObjectName("installModelButton")
        content.addWidget(self.model_button, alignment=Qt.AlignmentFlag.AlignLeft)
        content.addWidget(label("Однократная загрузка модели Vosk с официального сайта. После установки распознавание работает без интернета; звук в облако не отправляется.", "muted", True))
        outer.addWidget(card)
        data = QFrame()
        data.setObjectName("card")
        data_layout = QVBoxLayout(data)
        data_layout.setContentsMargins(22, 20, 22, 20)
        data_layout.addWidget(label("Личные данные остаются здесь", "section"))
        data_layout.addWidget(label("Задачи, заметки и история хранятся на этом компьютере. Telegram и облачные сервисы не нужны для основных команд. Удаление приложения не удаляет ваши данные.", "muted", True))
        outer.addWidget(data)
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
        model_row = QHBoxLayout()
        self.chat_model = QLineEdit(str(self.preferences.get("local_chat_model", "qwen3:4b-instruct-2507-q4_K_M")))
        self.chat_model.setPlaceholderText("Имя установленной модели Ollama")
        self.chat_model.setMaxLength(100)
        model_row.addWidget(self.chat_model, 1)
        model_row.addWidget(button("Сохранить модель", lambda: self.save_preference("local_chat_model", self.chat_model.text().strip())))
        chat_layout.addLayout(model_row)
        outer.addWidget(local_chat)
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
        for position, nav in enumerate(self.nav_buttons):
            nav.setChecked(position == index)
        if hasattr(self, "voice"):
            self.refresh()

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _message(self, role, text):
        card = QFrame()
        card.setObjectName("message")
        content = QVBoxLayout(card)
        content.setContentsMargins(16, 12, 16, 14)
        content.setSpacing(7)
        content.addWidget(label("ВЫ" if role == "user" else "JARVIS", "eyebrow"))
        body = label(str(text), wrap=True)
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
        for item in recent:
            self.history_layout.addWidget(self._message(item.get("role", "assistant"), item.get("text", "")))
        QTimer.singleShot(0, lambda: self.history_scroll.verticalScrollBar().setValue(self.history_scroll.verticalScrollBar().maximum()))
        self.refresh_tasks(snapshot)
        self.refresh_notes(snapshot=snapshot)
        if hasattr(self, "voice"):
            state = self.voice.available()
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

    def save_preference(self, key, value):
        try:
            self.preferences = self.core.store.set_preferences({key: value})
        except ValueError:
            self.status.setText("Проверьте значение настройки")
            return
        if key == "speech_enabled" and hasattr(self, "voice"):
            self.voice.speech_enabled = bool(value)

    def _poll_reminders(self):
        if self._quitting or self.busy:
            return
        due = getattr(self.core, "due_reminders", None)
        if due is None:
            return
        try:
            replies = due()
        except Exception:
            self.status.setText("Не удалось проверить напоминания")
            return
        if not replies:
            return
        # Reminders are data. Never send their text back through command
        # authorization and never disturb a pending PC confirmation card.
        self.refresh()
        for reply in replies:
            if self.tray:
                self.tray.showMessage("JARVIS · напоминание", reply.text[:500], QSystemTrayIcon.MessageIcon.Information, 8000)
            if reply.speak and self.preferences.get("speech_enabled", True):
                self.voice.say(reply.text)

    def _run(self, function, done):
        worker = Worker(function)
        self._workers.add(worker)
        def finished(value):
            self._workers.discard(worker)
            done(value)
        def failed(_error_class):
            self._workers.discard(worker)
            if self._quitting:
                return
            self.busy = False
            self.send_button.setEnabled(True)
            self.command.setEnabled(True)
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
        self.busy = True
        self.command.setEnabled(False)
        self.send_button.setEnabled(False)
        self.status.setText("Выполняю…")
        self._run(lambda: self.core.handle(text, source=source), self._reply)

    def _reply(self, reply):
        if self._quitting:
            return
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
        self.status.setText("Ожидаю «Джарвис»" if self.listening else self._capture_error or "Микрофон выключен")
        if reply.speak and self.preferences.get("speech_enabled", True):
            self.voice.say(reply.text)
        self.command.setFocus()

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
            self.listening = False
            self.orb.active = False
            self.orb.update()
            self.mic_button.setText("Микрофон")
            self.mic_button.setEnabled(True)
        self.status.setText(text)
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
        self._capture_failed = False
        self._capture_error = None
        self.status.setText("Запускаю микрофон…")
        self._run(self.voice.start, self._microphone_started)

    def _microphone_started(self, result):
        self.mic_button.setEnabled(True)
        self.listening = bool(result) and not self._capture_failed
        self.orb.active = self.listening
        self.orb.update()
        self.mic_button.setText("Выключить микрофон" if self.listening else "Микрофон")
        if self.listening:
            self.status.setText("Ожидаю «Джарвис»")

    def stop_listening(self):
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
        self.voice.stop()
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

    def _finish_close(self):
        if self.pool.activeThreadCount() == 0:
            self._close_timer.stop()
            self.close()

    def closeEvent(self, event):
        if not self._quitting and self.tray and self.preferences.get("close_to_tray", True):
            self.hide()
            event.ignore()
            if not self._tray_notice_shown:
                self.tray.showMessage("JARVIS остаётся в трее", "Откройте меню значка, чтобы выключить микрофон или завершить работу.", QSystemTrayIcon.MessageIcon.Information, 4000)
                self._tray_notice_shown = True
            return
        self._quitting = True
        self._reminder_timer.stop()
        self.stop_listening()
        # Stop queued work immediately; let an already-running bounded adapter
        # finish without freezing the UI or closing its database under it.
        self.pool.clear()
        if self.pool.activeThreadCount():
            self.setEnabled(False)
            self.status.setText("Завершаю текущее действие. Микрофон выключен.")
            self._close_timer.start()
            event.ignore()
            return
        self.core.close()
        if self.tray:
            self.tray.hide()
        event.accept()
        QApplication.instance().quit()
