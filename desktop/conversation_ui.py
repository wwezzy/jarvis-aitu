"""Two literal conversation views and explicit local attachment controls."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)


def _label(text):
    value = QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setWordWrap(True)
    return value


class ConversationPane(QWidget):
    def __init__(self, channel, parent=None):
        super().__init__(parent)
        self.channel = channel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        hint = "Здесь остаются сказанные вами запросы и голосовые ответы." if channel == "voice" else "Отдельная переписка. Написанные ответы не озвучиваются."
        layout.addWidget(_label(hint))
        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setMinimumHeight(65)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history_container = QWidget()
        self.history_layout = QVBoxLayout(self.history_container)
        self.history_layout.setContentsMargins(0, 0, 7, 0)
        self.history_layout.setSpacing(10)
        self.history_layout.addStretch()
        self.history_scroll.setWidget(self.history_container)
        self.body = QStackedWidget()
        self.body.addWidget(self.history_scroll)
        self.stream_preview = QFrame()
        self.stream_preview.setObjectName("message")
        preview = QVBoxLayout(self.stream_preview)
        preview.setContentsMargins(10, 6, 10, 6)
        preview.setSpacing(4)
        preview.addWidget(_label("Ответ ещё формируется…"))
        self.stream_text = _label("")
        self.stream_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(130)
        scroll.setMinimumHeight(45)
        scroll.setWidget(self.stream_text)
        preview.addWidget(scroll)
        self.stream_preview.hide()
        self.body.addWidget(self.stream_preview)
        layout.addWidget(self.body, 1)


class ConversationWorkspace(QWidget):
    attach_requested = Signal()
    analyze_requested = Signal()
    remove_requested = Signal(str)
    archive_requested = Signal()
    files_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("conversationTabs")
        self.voice = ConversationPane("voice")
        self.text = ConversationPane("text")
        self.tabs.addTab(self.voice, "Голос")
        self.tabs.addTab(self.text, "Переписка")
        self.tabs.setCurrentWidget(self.text)
        layout.addWidget(self.tabs, 1)
        attachments = QWidget()
        attachment_layout = QVBoxLayout(attachments)
        attachment_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.attach_button = QPushButton("Прикрепить")
        self.attach_button.setToolTip("TXT, Markdown, PDF или DOCX")
        self.attach_button.setObjectName("attachDocumentButton")
        self.attach_button.clicked.connect(self.attach_requested)
        self.analyze_button = QPushButton("Анализировать")
        self.analyze_button.setObjectName("analyzeDocumentsButton")
        self.analyze_button.clicked.connect(self.analyze_requested)
        self.analyze_button.setEnabled(False)
        self.archive_button = QPushButton("Архив")
        self.archive_button.setToolTip("Старая история · только чтение")
        self.archive_button.clicked.connect(self.archive_requested)
        self.archive_button.hide()
        row.addWidget(self.attach_button)
        row.addWidget(self.analyze_button)
        files = QPushButton("Файлы")
        files.setToolTip("Все сохранённые локальные копии")
        files.clicked.connect(self.files_requested)
        row.addWidget(files)
        row.addStretch()
        row.addWidget(self.archive_button)
        attachment_layout.addLayout(row)
        self.attachment_list = QListWidget()
        self.attachment_list.setObjectName("conversationAttachments")
        self.attachment_list.setMaximumHeight(86)
        self.attachment_list.itemDoubleClicked.connect(self._preview)
        attachment_layout.addWidget(self.attachment_list)
        controls = QHBoxLayout()
        self.attachment_hint = _label("TXT, Markdown, PDF и DOCX · до четырёх файлов.")
        self.attachment_hint.setObjectName("attachmentStatus")
        controls.addWidget(self.attachment_hint, 1)
        self.remove_button = QPushButton("Удалить")
        self.remove_button.clicked.connect(self._remove_selected)
        controls.addWidget(self.remove_button)
        attachment_layout.addLayout(controls)
        self.text.layout().addWidget(attachments)
        self._attachments = {}
        self.set_attachments([])

    def pane(self, channel):
        return self.voice if channel == "voice" else self.text

    def select(self, channel):
        self.tabs.setCurrentWidget(self.pane(channel))

    def set_attachments(self, rows):
        self._attachments = {row["id"]: dict(row) for row in rows}
        self.attachment_list.clear()
        for row in rows:
            suffix = " · показан фрагмент" if row.get("truncated") else ""
            item = QListWidgetItem(str(row.get("name", "Файл")) + suffix)
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.attachment_list.addItem(item)
        self.attachment_list.setVisible(bool(rows))
        self.attachment_list.setFixedHeight(min(50, 18 * len(rows) + 14))
        self.remove_button.setVisible(bool(rows))
        self.analyze_button.setEnabled(bool(rows))
        if rows:
            self.attachment_list.setCurrentRow(0)

    def _remove_selected(self):
        item = self.attachment_list.currentItem()
        if item is not None:
            self.remove_requested.emit(item.data(Qt.ItemDataRole.UserRole))

    def _preview(self, item):
        row = self._attachments.get(item.data(Qt.ItemDataRole.UserRole))
        if row is not None:
            self.show_text(str(row.get("name", "Предпросмотр")), str(row.get("preview", row.get("text", "")))[:12000])

    def show_text(self, title, text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(620, 430)
        layout = QVBoxLayout(dialog)
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setPlainText(text)
        layout.addWidget(preview)
        close = QPushButton("Закрыть")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog

    def manage_files(self, rows, on_selected, on_delete):
        dialog = QDialog(self)
        dialog.setWindowTitle("Локальные копии файлов")
        dialog.resize(560, 350)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_label("Выберите до четырёх файлов для анализа. Удаление убирает локальную копию и отзывает её из будущих запросов."))
        choices = QListWidget()
        choices.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for row in rows:
            item = QListWidgetItem(row["name"])
            item.setData(Qt.ItemDataRole.UserRole, row)
            choices.addItem(item)
        layout.addWidget(choices)
        status = _label("")
        layout.addWidget(status)
        buttons = QHBoxLayout()
        use = QPushButton("Добавить в переписку")
        def selected():
            items = choices.selectedItems()
            if not 1 <= len(items) <= 4:
                status.setText("Выберите от одного до четырёх файлов.")
                return
            on_selected([item.data(Qt.ItemDataRole.UserRole) for item in items])
            dialog.accept()
        use.clicked.connect(selected)
        delete = QPushButton("Удалить выбранное")
        def remove():
            for item in choices.selectedItems():
                on_delete(item.data(Qt.ItemDataRole.UserRole)["id"])
            status.setText("Удаление запрошено. Результат появится в переписке; откройте список заново для обновления.")
        delete.clicked.connect(remove)
        buttons.addWidget(use)
        buttons.addWidget(delete)
        layout.addLayout(buttons)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog


class MemoryPanel(QFrame):
    """Explicit fact edits; the backend rechecks owner authorization at commit."""
    def __init__(self, core, run, closing, snapshot_path=None, parent=None, *, on_context_changed=None, on_mutation_finished=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.core, self.run, self.closing, self.snapshot_path = core, run, closing, snapshot_path
        self.on_context_changed = on_context_changed
        self.on_mutation_finished = on_mutation_finished
        self._memory_enabled = False
        layout = QVBoxLayout(self)
        expand = QPushButton("Память обо мне")
        expand.setCheckable(True)
        layout.addWidget(expand)
        self.details = QWidget()
        content = QVBoxLayout(self.details)
        content.setContentsMargins(0, 0, 0, 0)
        content.addWidget(_label("Здесь можно проверить, исправить и забыть разрешённые сведения. Старые факты помечаются датой; они не считаются текущими задачами."))
        self.enabled = QCheckBox("Использовать разрешённую память в ответах")
        self.enabled.toggled.connect(lambda value: self._mutate("set_enabled", value))
        content.addWidget(self.enabled)
        self.facts = QListWidget()
        self.facts.setMaximumHeight(140)
        self.facts.currentItemChanged.connect(self._selected)
        content.addWidget(self.facts)
        self.key = QLineEdit()
        self.key.setPlaceholderText("Например, любимый формат обучения")
        self.key.setMaxLength(80)
        content.addWidget(self.key)
        self.value = QPlainTextEdit()
        self.value.setPlaceholderText("Что Jarvis стоит помнить")
        self.value.setMaximumHeight(75)
        content.addWidget(self.value)
        buttons = QHBoxLayout()
        self.save = QPushButton("Сохранить факт")
        self.save.clicked.connect(lambda: self._mutate("upsert", self.key.text(), self.value.toPlainText()))
        self.forget = QPushButton("Забыть выбранное")
        self.forget.clicked.connect(self._forget)
        self.import_button = QPushButton("Импорт старых сведений…")
        self.import_button.clicked.connect(self._preview_import)
        for button in (self.save, self.forget):
            buttons.addWidget(button)
        content.addLayout(buttons)
        content.addWidget(self.import_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.status = _label("")
        content.addWidget(self.status)
        layout.addWidget(self.details)
        self.details.hide()
        expand.toggled.connect(self._expanded)

    def _expanded(self, expanded):
        self.details.setVisible(expanded)
        if expanded:
            self.refresh()

    def _selected(self, item, _old=None):
        if item is not None:
            fact = item.data(Qt.ItemDataRole.UserRole)
            self.key.setText(fact["key"])
            self.value.setPlainText(fact["value"])

    def refresh(self):
        memory = getattr(self.core, "memory", None)
        if memory is None or self.closing():
            self.status.setText("Память пока недоступна.")
            return
        def done(result):
            if self.closing():
                return
            status, facts = result
            self._memory_enabled = status["enabled"]
            blocked = self.enabled.blockSignals(True)
            self.enabled.setChecked(status["enabled"])
            self.enabled.blockSignals(blocked)
            self.facts.clear()
            for fact in facts:
                date = " · историческое, " + fact["observed_at"][:10] if fact.get("historical") else ""
                item = QListWidgetItem(fact["key"] + ": " + fact["value"][:120] + date)
                item.setData(Qt.ItemDataRole.UserRole, fact)
                self.facts.addItem(item)
            self.status.setText(f"Сохранено сведений: {len(facts)}. Изменения требуют доступа владельца.")
        self.run(lambda: (memory.status(), memory.list_facts()), done, on_error=self._failed)

    def _failed(self, _error):
        if not self.closing():
            blocked = self.enabled.blockSignals(True)
            self.enabled.setChecked(self._memory_enabled)
            self.enabled.blockSignals(blocked)
            self.status.setText("Не удалось изменить память. Проверь данные и подтверждение владельца.")

    def _mutate(self, method, *values):
        memory = getattr(self.core, "memory", None)
        if memory is None or self.closing():
            return
        if self.on_context_changed is not None:
            if self.on_context_changed() is False:
                return
        def done(_result):
            if self.on_mutation_finished is not None:
                self.on_mutation_finished()
            if not self.closing():
                self.refresh()
        def failed(error):
            if self.on_mutation_finished is not None:
                self.on_mutation_finished()
            self._failed(error)
        self.run(lambda: getattr(memory, method)(*values), done, on_error=failed)

    def _forget(self):
        item = self.facts.currentItem()
        if item is not None:
            self._mutate("delete", item.data(Qt.ItemDataRole.UserRole)["key"])

    def _preview_import(self):
        from desktop.personal import load_personal_snapshot, sanitize_fact
        path = self.snapshot_path
        def preview():
            snapshot = load_personal_snapshot(path)
            rows = []
            if snapshot.get("status") == "ready":
                for row in snapshot.get("memory", [])[:100]:
                    try:
                        key, value = sanitize_fact(row.get("key"), row.get("value"))
                        rows.append(key + ": " + value)
                    except ValueError:
                        continue
            return rows
        def done(rows):
            if self.closing():
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Старая память · предпросмотр")
            dialog.resize(550, 360)
            content = QVBoxLayout(dialog)
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText("Историческая копия 10.10.2026. Сведения могут устареть.\n\n" + ("\n\n".join(rows)[:30000] or "Доступных сведений для импорта нет."))
            content.addWidget(text)
            apply = QPushButton("Импортировать как исторические")
            apply.setEnabled(bool(rows))
            apply.clicked.connect(lambda: (self._mutate("import_snapshot", path), dialog.accept()))
            content.addWidget(apply)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.open()
        self.run(preview, done, on_error=self._failed)
