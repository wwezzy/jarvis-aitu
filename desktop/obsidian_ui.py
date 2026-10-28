"""Explicit local vault UI; note text never enters a command/model route.

Construction performs no vault access. The parent calls refresh() on opening
this section, and must wait for shutdown_complete before closing its Core.
Workers hold backend/data only; results cross a weak, queued Qt bridge.
"""
from __future__ import annotations

import re
import threading
import weakref

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)


_LIMIT = 30
_MAX_BODY = 20000
_PREVIEW = 2000
_MESSAGES = {
    "configured": "Хранилище подключено. Заметки Jarvis остаются в своём разделе.",
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
        widget.clicked.connect(handler)
        return widget

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label("Obsidian · локальные заметки"))
        layout.addWidget(_label("Поиск по именам и кратким фрагментам Markdown. Содержимое отображается как текст; команды и ссылки из заметок не выполняются и не отправляются ИИ."))
        self.vault_status = _label("Хранилище ещё не проверено. Выбери папку или нажми «Обновить».")
        layout.addWidget(self.vault_status)
        row = QHBoxLayout()
        self.choose_button = self._button("Выбрать хранилище…", self.choose_vault)
        self.refresh_button = self._button("Обновить", self.refresh)
        row.addWidget(self.choose_button)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.open_vault_button = self._button("Открыть Obsidian", self.open_vault)
        layout.addWidget(self.open_vault_button)
        row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setMaxLength(500)
        self.search_input.setPlaceholderText("Название или текст в начале заметки")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self.refresh)
        self.search_button = self._button("Найти", self.refresh)
        row.addWidget(self.search_input, 1)
        row.addWidget(self.search_button)
        layout.addLayout(row)
        self.results_status = _label("Показываются до 30 заметок; поиск не сканирует весь компьютер.")
        layout.addWidget(self.results_status)
        self.notes_list = QListWidget()
        self.notes_list.setMinimumHeight(140)
        self.notes_list.setMaximumHeight(240)
        self.notes_list.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.notes_list)
        self.preview_title = _label("Выбери заметку для просмотра.")
        self.preview_path = _label("")
        layout.addWidget(self.preview_title)
        layout.addWidget(self.preview_path)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(90)
        self.preview.setMaximumHeight(200)
        layout.addWidget(self.preview)
        self.preview_hint = _label("Показан только текст. Полную заметку можно открыть отдельной кнопкой.")
        layout.addWidget(self.preview_hint)
        self.open_note_button = self._button("Открыть выбранную заметку", self.open_note)
        layout.addWidget(self.open_note_button)
        layout.addWidget(_label("Новая заметка · существующие файлы не перезаписываются"))
        self.title_input = QLineEdit()
        self.title_input.setMaxLength(100)
        self.title_input.setPlaceholderText("Заголовок заметки")
        self.title_input.textChanged.connect(self._sync)
        layout.addWidget(self.title_input)
        self.body_input = QPlainTextEdit()
        self.body_input.setPlaceholderText("Текст новой заметки")
        self.body_input.setMinimumHeight(120)
        self.body_input.textChanged.connect(self._sync)
        layout.addWidget(self.body_input)
        self.body_count = _label("0 / 20 000 символов")
        layout.addWidget(self.body_count)
        self.create_button = self._button("Создать новую заметку", self.create_note)
        layout.addWidget(self.create_button)
        layout.addWidget(_label("Черновик этой формы не сохраняется автоматически. Выбор папки, создание и открытие требуют действующего разрешения владельца, если защита включена."))
        self.feedback = _label("")
        layout.addWidget(self.feedback)

    def _selected_id(self):
        item = self.notes_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _sync(self, *_args):
        active = not self.busy and not self._closing.is_set()
        for widget in (self.choose_button, self.refresh_button, self.search_button):
            widget.setEnabled(active)
        self.notes_list.setEnabled(active and self._configured)
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
        self.preview_title.setText("Выбери заметку для просмотра.")
        self.preview_path.setText("")
        self.preview.clear()
        self.preview_hint.setText("Показан только текст. Полную заметку можно открыть отдельной кнопкой.")

    def _apply_listing(self, result):
        state, rows, query = result
        self._configured = isinstance(state, dict) and state.get("configured") is True and state.get("status") == "configured"
        text = _MESSAGES["configured"] if self._configured else _MESSAGES.get(state.get("status") if isinstance(state, dict) and isinstance(state.get("status"), str) else "unavailable", _MESSAGES["unavailable"])
        if self._configured and isinstance(state.get("vault"), str):
            text += "\nПапка: " + state["vault"][:2048]
        self.vault_status.setText(text)
        old_id = self._selected_id()
        blocked = self.notes_list.blockSignals(True)
        self.notes_list.clear()
        seen = set()
        for row in rows[:_LIMIT] if self._configured and isinstance(rows, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not re.fullmatch(r"[a-f0-9]{24}", row["id"]) or row["id"] in seen:
                continue
            seen.add(row["id"])
            title = row.get("title") if isinstance(row.get("title"), str) else "Заметка"
            path = row.get("relative_path") if isinstance(row.get("relative_path"), str) else ""
            item = QListWidgetItem(title[:200] + ("\n" + path[:2048] if path else ""))
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
            prefix = f"Поиск «{query[:80]}»: {count}. " if query else f"В списке: {count}. "
            message = prefix + "Показаны до 30 заметок; поиск учитывает только краткие фрагменты."
        elif not self._configured:
            message = "Список недоступен, пока хранилище не подключено."
        else:
            message = "По этому запросу заметок нет." if query else "В доступной части хранилища заметок пока нет."
        self.results_status.setText(message)
        self._preview_pending = self._selected_id() is not None

    def _selection_changed(self, *_args):
        if self._closing.is_set():
            return
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
                self.preview_hint.setText("Показан краткий фрагмент. Полный текст можно открыть в Obsidian." if result.get("truncated") is True else "Текстовый просмотр. Ссылки и инструкции здесь не выполняются.")
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
