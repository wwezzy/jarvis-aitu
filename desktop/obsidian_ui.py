"""Explicit local vault UI; note text never enters a command/model route.

Construction performs no vault access. The parent calls refresh() on opening
this section, and must wait for shutdown_complete before closing its Core.
Workers hold backend/data only; results cross a weak, queued Qt bridge.
"""
from __future__ import annotations

import re
import threading
import weakref

from PySide6.QtCore import QObject, QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)


_LIMIT = 30
_MAX_BODY = 20000
_PREVIEW = 2000
_MESSAGES = {
    "configured": "Хранилище подключено.",
    "not_configured": "Выбери существующую локальную папку хранилища Obsidian.",
    "unavailable": "Хранилище или заметка недоступны. Проверь папку и обнови список.",
    "refused": "Действие не подтверждено. Проверь владельца, выбранную папку, заголовок и текст; существующие заметки не заменяются.",
    "created": "Новая заметка создана в папке Inbox. Существующие заметки не заменены.",
    "requested": "Запрос открытия передан Obsidian. Если приложение не открылось, проверь его установку и хранилище.",
    "missing": "Заметка больше не найдена. Обнови список и выбери её заново.",
    "changed": "Выбранное хранилище изменилось. Обнови список.",
}
_OWNER = "Сначала подтверди владельца в разделе «Управление», если защита включена."
_FAILURE = "Не удалось подтвердить действие. Сохранённые заметки не сбрасываются; повтори проверку."

_STYLE = """
QWidget#obsidianWorkspace { background: #0b1421; color: #dbe6f3; font-family: 'Inter'; font-size: 13px; }
QLabel { background: transparent; border: none; }
QLabel#notesHeading { color: #edf7ff; font-size: 17px; font-weight: 600; }
QLabel#notesMuted, QLabel#notesFeedback { color: #91a6bd; font-size: 12px; }
QLabel#notesEmptyTitle { color: #dcecf5; font-size: 21px; font-weight: 600; }
QFrame#notesLibrary, QFrame#notesDetail, QFrame#notesDetails { background: #101d2d; border: 1px solid #203349; border-radius: 12px; }
QPushButton { background: #182b3e; color: #dbe6f3; border: 1px solid #2b4057; padding: 8px 11px; border-radius: 8px; }
QPushButton:hover { background: #213b50; border-color: #4b6e82; }
QPushButton:disabled { color: #5c7287; background: #111e2d; border-color: #203146; }
QPushButton#primary { color: #072829; background: #79e2d2; border-color: #79e2d2; font-weight: 600; }
QPushButton#primary:hover { background: #a4f5e8; }
QPushButton#primary:disabled { background: #142b34; color: #587d85; border-color: #264452; }
QPushButton#quiet { background: transparent; color: #9cb4c9; border-color: #2a3e51; }
QPushButton#quiet:disabled { background: transparent; color: #52697d; border-color: #203146; }
QLineEdit { background: #0b1624; color: #edf7ff; border: 1px solid #2b4057; border-radius: 8px; padding: 9px 11px; selection-background-color: #286f72; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #72d9cc; }
QPlainTextEdit { background: #0d1928; color: #dbe6f3; border: 1px solid #263c50; border-radius: 8px; padding: 10px; selection-background-color: #286f72; }
QPlainTextEdit#notePreview { background: transparent; border: none; padding: 0; }
QListWidget#notesList { background: transparent; color: #c2d4e2; border: none; outline: 0; padding: 2px; }
QListWidget#notesList::item { padding: 10px 8px; border: 1px solid transparent; border-radius: 7px; }
QListWidget#notesList::item:selected { background: #193d43; color: #e5fff9; border-color: #2d6266; }
QListWidget#notesList::item:hover:!selected { background: #182d40; }
QStackedWidget { background: transparent; border: none; }
QSplitter::handle { background: transparent; width: 10px; }
QSplitter::handle:hover { background: #203a4a; border-radius: 3px; }
QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0; }
QScrollBar::handle:vertical { background: #304d61; min-height: 28px; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def _label(text):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return widget


def _authorized(core, closing):
    try:
        return not closing.is_set() and core._catalog_authorized() is True
    except Exception:
        return False


class _Signals(QObject):
    completed = Signal(int, bool, object)


class ObsidianPanel(QWidget):
    def __init__(self, core, on_changed=None):
        super().__init__()
        self.core, self.vault, self.on_changed = core, core.obsidian, on_changed
        self._closing = threading.Event()
        closing = self._closing
        self.destroyed.connect(lambda *_args: closing.set())
        self._signals = _Signals(self)
        self._signals.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._jobs, self._number = {}, 0
        self._thread_lock, self._threads, self._thread_number = threading.Lock(), {}, 0
        self._configured, self._preview_id = False, None
        self._refresh_pending = self._preview_pending = False
        self._editing, self._select_after_refresh = False, None
        self._build()
        self._sync()

    @property
    def busy(self):
        return bool(self._jobs)

    @property
    def shutdown_complete(self):
        if not self._closing.is_set():
            return False
        with self._thread_lock:
            self._prune_threads()
            return not self._threads

    def _prune_threads(self):
        for number in [key for key, (thread, done) in self._threads.items() if done.is_set() and not thread.is_alive()]:
            self._threads.pop(number)

    @staticmethod
    def _button(text, handler):
        widget = QPushButton(text)
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
        widget.clicked.connect(handler)
        return widget

    def _build(self):
        self.setObjectName("obsidianWorkspace")
        self.setStyleSheet(_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 10)
        layout.setSpacing(12)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        heading = _label("Библиотека")
        heading.setObjectName("notesHeading")
        toolbar.addWidget(heading, 1)
        self.new_note_button = self._button("Новая", self.new_note)
        self.new_note_button.setObjectName("primary")
        self.new_note_button.setToolTip("Создать новую заметку")
        toolbar.addWidget(self.new_note_button)
        self.open_vault_button = self._button("Obsidian ↗", self.open_vault)
        self.open_vault_button.setObjectName("quiet")
        self.open_vault_button.setToolTip("Открыть хранилище в Obsidian")
        toolbar.addWidget(self.open_vault_button)
        self.details_button = self._button("Папка", lambda: None)
        self.details_button.setObjectName("quiet")
        self.details_button.setCheckable(True)
        toolbar.addWidget(self.details_button)
        layout.addLayout(toolbar)

        self.details_box = QFrame()
        self.details_box.setObjectName("notesDetails")
        details = QVBoxLayout(self.details_box)
        details.setContentsMargins(14, 12, 14, 12)
        self.vault_status = _label("Хранилище ещё не проверено. Выбери папку или нажми «Обновить».")
        self.vault_status.setObjectName("notesMuted")
        details.addWidget(self.vault_status)
        self.choose_button = self._button("Выбрать хранилище…", self.choose_vault)
        details.addWidget(self.choose_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.preview_path = _label("")
        self.preview_path.setObjectName("notesMuted")
        details.addWidget(self.preview_path)
        privacy = _label("Просмотр ограничен кратким текстом. Записи не выполняются и автоматически не передаются ИИ. Новые заметки создаются без перезаписи существующих файлов.")
        privacy.setObjectName("notesMuted")
        details.addWidget(privacy)
        self.details_box.hide()
        self.details_button.toggled.connect(self.details_box.setVisible)
        layout.addWidget(self.details_box)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.refresh_button = self._button("Обновить", self.refresh)
        self.refresh_button.setObjectName("quiet")
        self.search_input = QLineEdit()
        self.search_input.setMaxLength(500)
        self.search_input.setPlaceholderText("Найти заметку…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self.refresh)
        self.search_button = self._button("Найти", self.refresh)
        self.search_button.setObjectName("quiet")
        row.addWidget(self.search_input, 1)
        row.addWidget(self.search_button)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)

        self.workspace = QSplitter(Qt.Orientation.Horizontal)
        self.workspace.setChildrenCollapsible(False)
        self.workspace.setHandleWidth(10)
        self.workspace.setMinimumHeight(320)
        self.library_box = QFrame()
        self.library_box.setObjectName("notesLibrary")
        self.library_box.setMinimumWidth(155)
        library = QVBoxLayout(self.library_box)
        library.setContentsMargins(10, 12, 10, 10)
        library.setSpacing(8)
        self.results_status = _label("Твои заметки")
        self.results_status.setObjectName("notesMuted")
        library.addWidget(self.results_status)
        self.library_stack = QStackedWidget()
        self.notes_list = QListWidget()
        self.notes_list.setObjectName("notesList")
        self.notes_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.notes_list.setUniformItemSizes(True)
        self.notes_list.currentItemChanged.connect(self._selection_changed)
        self.library_stack.addWidget(self.notes_list)
        self.list_empty = _label("Пока пусто.\nСохрани первую мысль.")
        self.list_empty.setObjectName("notesMuted")
        self.list_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.library_stack.addWidget(self.list_empty)
        library.addWidget(self.library_stack, 1)
        self.workspace.addWidget(self.library_box)

        detail_box = QFrame()
        detail_box.setObjectName("notesDetail")
        detail_box.setMinimumWidth(270)
        detail = QVBoxLayout(detail_box)
        detail.setContentsMargins(18, 16, 18, 14)
        self.detail_stack = QStackedWidget()
        detail.addWidget(self.detail_stack, 1)
        self.reader = QWidget()
        reader = QVBoxLayout(self.reader)
        reader.setContentsMargins(0, 0, 0, 0)
        reader.setSpacing(12)
        self.preview_title = _label("Выбери заметку")
        self.preview_title.setObjectName("notesHeading")
        reader.addWidget(self.preview_title)
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("notePreview")
        self.preview.setReadOnly(True)
        reader.addWidget(self.preview, 1)
        self.preview_hint = _label("Полную заметку можно открыть в Obsidian.")
        self.preview_hint.setObjectName("notesMuted")
        reader.addWidget(self.preview_hint)
        self.open_note_button = self._button("Открыть заметку", self.open_note)
        self.open_note_button.setObjectName("quiet")
        reader.addWidget(self.open_note_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.detail_stack.addWidget(self.reader)

        self.editor = QWidget()
        editor = QVBoxLayout(self.editor)
        editor.setContentsMargins(0, 0, 0, 0)
        editor.setSpacing(10)
        editor_header = QHBoxLayout()
        editor_heading = _label("Новая заметка")
        editor_heading.setObjectName("notesHeading")
        editor_header.addWidget(editor_heading, 1)
        self.back_button = self._button("Назад", self.show_reader)
        self.back_button.setObjectName("quiet")
        self.back_button.setToolTip("Вернуться к просмотру. Черновик останется до закрытия Jarvis.")
        editor_header.addWidget(self.back_button)
        editor.addLayout(editor_header)
        self.title_input = QLineEdit()
        self.title_input.setMaxLength(100)
        self.title_input.setPlaceholderText("Заголовок")
        self.title_input.textChanged.connect(self._sync)
        editor.addWidget(self.title_input)
        self.body_input = QPlainTextEdit()
        self.body_input.setPlaceholderText("Мысль, план или полезная находка…")
        self.body_input.setMinimumHeight(150)
        self.body_input.textChanged.connect(self._sync)
        editor.addWidget(self.body_input, 1)
        editor_footer = QHBoxLayout()
        self.body_count = _label("0 / 20 000 символов")
        self.body_count.setObjectName("notesMuted")
        editor_footer.addWidget(self.body_count, 1)
        self.create_button = self._button("Сохранить", self.create_note)
        self.create_button.setObjectName("primary")
        editor_footer.addWidget(self.create_button)
        editor.addLayout(editor_footer)
        draft_hint = _label("Создастся новая заметка. Черновик хранится до закрытия Jarvis.")
        draft_hint.setObjectName("notesMuted")
        editor.addWidget(draft_hint)
        self.detail_stack.addWidget(self.editor)

        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(10, 16, 10, 16)
        empty_layout.addStretch()
        self.empty_title = _label("Здесь будут твои идеи")
        self.empty_title.setObjectName("notesEmptyTitle")
        empty_layout.addWidget(self.empty_title)
        self.empty_hint = _label("Подключи папку заметок, чтобы собрать важное в одном месте.")
        self.empty_hint.setObjectName("notesMuted")
        empty_layout.addWidget(self.empty_hint)
        self.empty_button = self._button("Выбрать папку", self._empty_action)
        self.empty_button.setObjectName("primary")
        empty_layout.addSpacing(6)
        empty_layout.addWidget(self.empty_button, alignment=Qt.AlignmentFlag.AlignLeft)
        empty_layout.addStretch()
        self.detail_stack.addWidget(empty)
        self.detail_stack.setCurrentIndex(2)
        self.workspace.addWidget(detail_box)
        self.workspace.setStretchFactor(0, 1)
        self.workspace.setStretchFactor(1, 2)
        self.workspace.setSizes([220, 460])
        layout.addWidget(self.workspace, 1)
        self.feedback = _label("")
        self.feedback.setObjectName("notesFeedback")
        layout.addWidget(self.feedback)

    def new_note(self):
        if self._closing.is_set() or not self._configured:
            return
        self._editing = True
        self.detail_stack.setCurrentIndex(1)
        self.title_input.setFocus()

    def show_reader(self):
        if self._closing.is_set():
            return
        self._editing = False
        self.detail_stack.setCurrentIndex(0 if self._preview_id else 2)

    def _empty_action(self):
        if self._configured:
            self.new_note()
        else:
            self.choose_vault()

    def _selected_id(self):
        item = self.notes_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _sync(self, *_args):
        active = not self.busy and not self._closing.is_set()
        for widget in (self.choose_button, self.refresh_button, self.search_button):
            widget.setEnabled(active)
        self.notes_list.setEnabled(active and self._configured)
        self.new_note_button.setEnabled(self._configured and not self._closing.is_set())
        self.empty_button.setEnabled(active)
        self.library_box.setVisible(self._configured)
        self.open_vault_button.setEnabled(active and self._configured)
        self.open_note_button.setEnabled(active and self._configured and self._preview_id is not None and self._preview_id == self._selected_id())
        count = len(self.body_input.toPlainText())
        self.body_count.setText(f"{count:,} / 20 000 символов".replace(",", " "))
        self.create_button.setEnabled(active and self._configured and bool(self.title_input.text().strip())
                                      and bool(self.body_input.toPlainText().strip()) and count <= _MAX_BODY)

    def _run(self, function, callback):
        if self._closing.is_set() or self.busy:
            return False
        self._number += 1
        number, closing, bridge = self._number, self._closing, weakref.ref(self._signals)
        self._jobs[number] = callback
        self._sync()
        done = threading.Event()
        def execute():
            try:
                if closing.is_set():
                    return
                try:
                    value, success = function(), True
                except Exception:
                    value, success = None, False
                if not closing.is_set():
                    receiver = bridge()
                    if receiver is not None:
                        try:
                            receiver.completed.emit(number, success, value)
                        except RuntimeError:
                            pass
            finally:
                done.set()
        thread = threading.Thread(target=execute, name="JarvisObsidian", daemon=True)
        with self._thread_lock:
            self._prune_threads()
            self._thread_number += 1
            thread_number = self._thread_number
            self._threads[thread_number] = (thread, done)
        try:
            thread.start()
        except RuntimeError:
            with self._thread_lock:
                self._threads.pop(thread_number, None)
            self._jobs.pop(number, None)
            self.feedback.setText(_FAILURE)
            self._sync()
            return False
        return True

    @Slot(int, bool, object)
    def _completed(self, number, success, value):
        callback = self._jobs.pop(number, None)
        if self._closing.is_set() or callback is None:
            return
        try:
            if success:
                callback(value)
            else:
                self.feedback.setText(_FAILURE)
        except Exception:
            self.feedback.setText(_FAILURE)
        self._sync()
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh()
        elif self._preview_pending:
            self._preview_pending = False
            self._read_selected()

    def refresh(self):
        if self._closing.is_set():
            return
        if self.busy:
            self._refresh_pending = True
            return
        vault, query = self.vault, self.search_input.text().strip()
        def read():
            state = vault.status()
            notes = []
            if isinstance(state, dict) and state.get("configured") is True:
                notes = vault.search(query, limit=_LIMIT) if query else vault.list_notes(limit=_LIMIT)
            return state, notes, query
        self._run(read, self._apply_listing)

    def _clear_preview(self):
        self._preview_id = None
        self.preview_title.setText("Выбери заметку")
        self.preview_path.setText("")
        self.preview.clear()
        self.preview_hint.setText("Полную заметку можно открыть в Obsidian.")
        if not self._editing:
            self.detail_stack.setCurrentIndex(2)

    def _apply_listing(self, result):
        state, rows, query = result
        self._configured = isinstance(state, dict) and state.get("configured") is True and state.get("status") == "configured"
        text = _MESSAGES["configured"] if self._configured else _MESSAGES.get(state.get("status") if isinstance(state, dict) and isinstance(state.get("status"), str) else "unavailable", _MESSAGES["unavailable"])
        if self._configured and isinstance(state.get("vault"), str):
            text += "\nПапка: " + state["vault"][:2048]
        self.vault_status.setText(text)
        old_id = self._select_after_refresh or self._selected_id()
        self._select_after_refresh = None
        blocked = self.notes_list.blockSignals(True)
        self.notes_list.clear()
        seen = set()
        for row in rows[:_LIMIT] if self._configured and isinstance(rows, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not re.fullmatch(r"[a-f0-9]{24}", row["id"]) or row["id"] in seen:
                continue
            seen.add(row["id"])
            title = row.get("title") if isinstance(row.get("title"), str) else "Заметка"
            item = QListWidgetItem(title[:200])
            item.setSizeHint(QSize(0, 43))
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.notes_list.addItem(item)
            if row["id"] == old_id:
                self.notes_list.setCurrentItem(item)
        if self.notes_list.currentItem() is None and self.notes_list.count():
            self.notes_list.setCurrentRow(0)
        self.notes_list.blockSignals(blocked)
        self._clear_preview()
        count = self.notes_list.count()
        if count:
            message = f"Найдено: {count}" if query else f"Заметки · {count}"
            if count == _LIMIT:
                message += " · уточни поиск"
        elif not self._configured:
            message = "Заметки"
        else:
            message = "Ничего не найдено" if query else "Заметки · 0"
        self.results_status.setText(message)
        self.library_stack.setCurrentIndex(0 if count else 1)
        self.list_empty.setText("Ничего не найдено.\nПопробуй другое слово." if query else "Пока пусто.\nСохрани первую мысль.")
        if self._configured:
            self.empty_title.setText("Сохрани то, что важно")
            self.empty_hint.setText("Идею, план или полезную находку. Здесь они останутся под рукой.")
            self.empty_button.setText("Новая заметка")
        else:
            self.empty_title.setText("Подключи свои заметки")
            self.empty_hint.setText("Выбери папку Obsidian. Прежние заметки Jarvis доступны в соседней вкладке.")
            self.empty_button.setText("Выбрать папку")
        self._preview_pending = self._selected_id() is not None

    def _selection_changed(self, *_args):
        if self._closing.is_set():
            return
        self._editing = False
        self._clear_preview()
        if self.busy:
            self._preview_pending = True
        else:
            self._read_selected()
        self._sync()

    def _read_selected(self):
        note_id = self._selected_id()
        if self._closing.is_set() or not note_id or self.busy:
            return
        vault = self.vault
        def complete(result):
            if self._selected_id() != note_id:
                self._preview_pending = True
                return
            if (isinstance(result, dict) and result.get("status") == "read" and result.get("id") == note_id
                    and isinstance(result.get("preview"), str)):
                self._preview_id = note_id
                self.preview_title.setText(result["title"][:200] if isinstance(result.get("title"), str) else "Заметка")
                self.preview_path.setText(result["relative_path"][:2048] if isinstance(result.get("relative_path"), str) else "")
                self.preview.setPlainText(result["preview"][:_PREVIEW])
                self.preview_hint.setText("Показан краткий фрагмент. Полный текст можно открыть в Obsidian." if result.get("truncated") is True else "Полную заметку можно открыть в Obsidian.")
                if not self._editing:
                    self.detail_stack.setCurrentIndex(0)
            else:
                self._clear_preview()
                self._result_message(result)
        self._run(lambda: vault.read(note_id), complete)

    def _guard(self):
        return _authorized(self.core, self._closing)

    def _result_message(self, result):
        status = result.get("status") if isinstance(result, dict) else None
        self.feedback.setText(_MESSAGES.get(status, _FAILURE) if isinstance(status, str) else _FAILURE)

    def _changed(self):
        if callable(self.on_changed):
            try:
                self.on_changed()
            except Exception:
                self.feedback.setText("Заметка или настройка сохранены, но обновление интерфейса не подтверждено. Нажми «Обновить».")

    def choose_vault(self):
        if self._closing.is_set() or self.busy:
            return
        if not self._guard():
            self.feedback.setText(_OWNER)
            return
        path = QFileDialog.getExistingDirectory(self, "Выбери локальное хранилище Obsidian")
        if not path:
            return
        core, closing, vault = self.core, self._closing, self.vault
        def choose():
            return vault.configure_vault(path, approved=True, authorization_guard=lambda: _authorized(core, closing))
        def complete(result):
            self._result_message(result)
            if isinstance(result, dict) and result.get("status") == "configured":
                self._changed()
                self._refresh_pending = True
        self._run(choose, complete)

    def create_note(self):
        if self._closing.is_set() or self.busy:
            return
        if not self._guard():
            self.feedback.setText(_OWNER)
            return
        title, body = self.title_input.text(), self.body_input.toPlainText()
        if not title.strip() or not body.strip() or len(body) > _MAX_BODY:
            self.feedback.setText("Нужны заголовок и текст от 1 до 20 000 символов.")
            return
        core, closing, vault = self.core, self._closing, self.vault
        def create():
            return vault.create_note(title.strip(), body, authorization_guard=lambda: _authorized(core, closing))
        def complete(result):
            self._result_message(result)
            if isinstance(result, dict) and result.get("status") == "created":
                if self.title_input.text() == title and self.body_input.toPlainText() == body:
                    self.title_input.clear()
                    self.body_input.clear()
                    self._editing = False
                    if isinstance(result.get("id"), str) and re.fullmatch(r"[a-f0-9]{24}", result["id"]):
                        self._select_after_refresh = result["id"]
                self._changed()
                self._refresh_pending = True
        self._run(create, complete)

    def _open(self, note_id=None):
        if self._closing.is_set() or self.busy:
            return
        if not self._guard():
            self.feedback.setText(_OWNER)
            return
        core, closing, vault = self.core, self._closing, self.vault
        def request():
            def guard():
                return _authorized(core, closing)
            return vault.open_note(note_id, authorization_guard=guard) if note_id is not None else vault.open_vault(authorization_guard=guard)
        self._run(request, self._result_message)

    def open_note(self):
        if self._closing.is_set() or self.busy:
            return
        note_id = self._selected_id()
        if note_id is not None and note_id == self._preview_id:
            self._open(note_id)

    def open_vault(self):
        self._open()

    def shutdown(self):
        self._closing.set()
        self._jobs.clear()
        self._refresh_pending = self._preview_pending = False
        self._sync()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
