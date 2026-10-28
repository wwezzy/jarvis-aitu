"""Explicit local controls for catalogs, owner consent and optional gestures.

No camera, Windows Hello prompt or program is started by construction/refresh.
Worker callbacks enter Qt through signals; late results after shutdown have no
UI effect. The main window must call shutdown() before closing its core.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QPushButton, QVBoxLayout, QWidget,
)

from desktop.gesture_worker import IsolatedGestureService as GestureService


_EDITORS = (("PyCharm", "pycharm"), ("IntelliJ IDEA", "idea"), ("Visual Studio Code", "vscode"), ("Проводник", "explorer"))
_GESTURES = frozenset({"volume_up", "volume_down", "minimize"})


def _label(text):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


class _Signals(QObject):
    completed = Signal(int, bool, object)
    gesture = Signal(int, str)
    camera_state = Signal(int, str)
    camera_stopped = Signal(int, bool)


class ControlsPanel(QWidget):
    def __init__(self, core, *, main_window=None, on_reply=None, remote_status=None):
        super().__init__()
        self.core, self.main_window = core, main_window
        self.on_reply, self.remote_status = on_reply, remote_status
        self._closing, self._camera_requested = threading.Event(), threading.Event()
        self._signals = _Signals(self)
        self._signals.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._signals.gesture.connect(self._gesture_intent, Qt.ConnectionType.QueuedConnection)
        self._signals.camera_state.connect(self._camera_state, Qt.ConnectionType.QueuedConnection)
        self._signals.camera_stopped.connect(self._camera_stopped, Qt.ConnectionType.QueuedConnection)
        self._jobs, self._job_number, self._refresh_pending = {}, 0, False
        self._worker_lock = threading.Lock()
        self._worker_number, self._worker_threads = 0, {}
        self._gesture_generation, self._gesture_service, self._stopping = 0, None, {}
        self._build()
        # A saved preference is a record of the last explicit session, not a
        # permission to open the camera at the next launch.
        self._save_camera_preference(False)
        self.owner_timer = QTimer(self)
        self.owner_timer.setInterval(1000)
        self.owner_timer.timeout.connect(self._poll_owner)
        self.owner_timer.start()
        self.refresh()

    @property
    def busy(self):
        return bool(self._jobs)

    @property
    def shutdown_complete(self):
        """Core may close only after actual workers exited, not GUI job removal.

        No joins or Qt access: the main window can poll this without blocking.
        A completed function can still be unwinding COM/thread-local state, so
        both its done event and Thread.is_alive() are checked.
        """
        if not self._closing.is_set():
            return False
        with self._worker_lock:
            self._prune_worker_threads()
            return not self._worker_threads

    def _prune_worker_threads(self):
        finished = [key for key, (thread, done) in self._worker_threads.items()
                    if done.is_set() and not thread.is_alive()]
        for key in finished:
            self._worker_threads.pop(key)

    def _start_worker(self, function, name):
        done = threading.Event()
        def execute():
            try:
                function()
            finally:
                done.set()
        thread = threading.Thread(target=execute, name=name, daemon=True)
        with self._worker_lock:
            self._prune_worker_threads()
            self._worker_number += 1
            self._worker_threads[self._worker_number] = (thread, done)
        try:
            thread.start()
        except RuntimeError:
            done.set()
            raise

    def _card(self, title):
        frame = QFrame()
        frame.setObjectName("controlCard")
        layout = QVBoxLayout(frame)
        layout.addWidget(_label(title))
        self._layout.addWidget(frame)
        return layout

    @staticmethod
    def _button(text, callback):
        widget = QPushButton(text)
        widget.clicked.connect(callback)
        return widget

    def _build(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        projects = self._card("Проекты")
        projects.addWidget(_label("Выбери папки с проектами. Поиск ограничен двумя уровнями и 200 проектами; файлы проектов не запускаются."))
        self.root_combo = QComboBox()
        projects.addWidget(self.root_combo)
        row = QHBoxLayout()
        self.root_picker = self._button("Добавить папку", self._pick_root)
        self.root_scan = self._button("Найти проекты", self._scan)
        self.root_remove = self._button("Убрать папку из доступа", self._remove_root)
        for widget in (self.root_picker, self.root_scan):
            row.addWidget(widget)
        projects.addLayout(row)
        projects.addWidget(self.root_remove)
        self.project_combo, self.editor_combo = QComboBox(), QComboBox()
        for title, key in _EDITORS:
            self.editor_combo.addItem(title, key)
        self.project_combo.currentIndexChanged.connect(self._project_selected)
        self.editor_combo.currentIndexChanged.connect(self._editor_changed)
        projects.addWidget(self.project_combo)
        projects.addWidget(self.editor_combo)
        row = QGridLayout()
        self.project_add = self._button("Добавить проект вручную", self._pick_project)
        self.project_alias = self._button("Задать имя для команды", self._alias_project)
        self.project_open = self._button("Открыть проект", self._open_project)
        self.project_remove = self._button("Убрать из списка", self._remove_project)
        for index, widget in enumerate((self.project_add, self.project_alias, self.project_open, self.project_remove)):
            row.addWidget(widget, index // 2, index % 2)
        projects.addLayout(row)
        projects.addWidget(_label("«Убрать» меняет только список Jarvis. Папки и файлы остаются на месте."))

        apps = self._card("Мои приложения")
        apps.addWidget(_label("Выбери файл приложения и дай ему короткое имя. Добавление требует подтверждённого владельца; после обновления файла доступ нужно подтвердить заново. Аргументы и команды оболочки не поддерживаются."))
        self.app_combo = QComboBox()
        for combo in (self.root_combo, self.project_combo, self.app_combo):
            combo.setMinimumContentsLength(12)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        apps.addWidget(self.app_combo)
        row = QHBoxLayout()
        self.app_add = self._button("Добавить приложение…", self._pick_app)
        self.app_open = self._button("Открыть приложение", self._open_app)
        self.app_remove = self._button("Убрать доступ", self._remove_app)
        for widget in (self.app_add, self.app_open, self.app_remove):
            row.addWidget(widget)
        apps.addLayout(row)

        owner = self._card("Подтверждение владельца")
        self.owner_toggle = QCheckBox("Подтверждать владельца перед командами")
        self.owner_toggle.toggled.connect(self._owner_changed)
        owner.addWidget(self.owner_toggle)
        owner.addWidget(_label("Windows Hello подтверждает текущего пользователя Windows лицом, отпечатком или PIN. Это не распознавание владельца по голосу. Доступ временный и сбрасывается при блокировке Windows."))
        self.owner_verify = self._button("Подтвердить через Windows Hello", self._verify_owner)
        self.owner_status = _label("Требуется проверка владельца.")
        owner.addWidget(self.owner_verify)
        owner.addWidget(self.owner_status)

        gestures = self._card("Жесты")
        self.gesture_toggle = QCheckBox("Включить камеру и жесты в этом сеансе")
        self.gesture_toggle.toggled.connect(self._gestures_changed)
        gestures.addWidget(self.gesture_toggle)
        gestures.addWidget(_label("После подтверждения владельца: большой палец вверх — громче, вниз — тише, открытая ладонь — свернуть окно. Сначала убери руки, затем удержи жест. Камера при запуске всегда выключена."))
        gestures.addWidget(_label("Jarvis не записывает кадры и не хранит видео. Компонент MediaPipe может отправлять диагностические данные об использовании SDK. Жесты не подтверждают личность."))
        self.gesture_status = _label("Камера выключена.")
        gestures.addWidget(self.gesture_status)

        remote = self._card("Управление с телефона")
        remote.addWidget(_label("Удалённый доступ включается отдельно после подтверждения владельца. Секреты и ссылки входа здесь не отображаются; готовность подключения проверяется отдельно."))
        self.remote_status_label = _label("Проверяю готовность удалённого доступа…")
        remote.addWidget(self.remote_status_label)
        row = QHBoxLayout()
        self.remote_approve = self._button("Разрешить удалённое управление", self._approve_remote)
        self.remote_revoke = self._button("Отозвать разрешение", self._revoke_remote)
        row.addWidget(self.remote_approve)
        row.addWidget(self.remote_revoke)
        remote.addLayout(row)
        self.status = _label("")
        self._layout.addWidget(self.status)

    def _owner_allowed(self):
        try:
            return not self._closing.is_set() and self.core.owner.enabled is True and self.core.owner.allowed() is True
        except Exception:
            return False

    def _owner_report(self):
        owner = self.core.owner
        status = getattr(owner, "status", None)
        if callable(status):
            return status()
        return {"enabled": owner.enabled is True, "verified": self._owner_allowed(), "remaining_seconds": 0}

    def _sync_enabled(self):
        active, allowed = not self.busy and not self._closing.is_set(), self._owner_allowed()
        for widget in (self.root_picker, self.project_add, self.owner_toggle):
            widget.setEnabled(active)
        for widget in (self.root_scan, self.root_remove):
            widget.setEnabled(active and self.root_combo.count() > 0)
        for widget in (self.project_alias, self.project_open, self.project_remove, self.editor_combo):
            widget.setEnabled(active and self.project_combo.count() > 0)
        self.app_add.setEnabled(active and allowed)
        self.app_open.setEnabled(active and self.app_combo.count() > 0)
        self.app_remove.setEnabled(active and self.app_combo.count() > 0)
        self.owner_verify.setEnabled(active and getattr(self.core.owner, "enabled", False) is True)
        self.remote_approve.setEnabled(active and allowed)
        self.remote_revoke.setEnabled(active)
        self.gesture_toggle.setEnabled(not self._closing.is_set() and (self._camera_requested.is_set() or active and allowed and not self._stopping))

    def _run(self, function, callback=None, *, refresh=True):
        if self._closing.is_set() or self.busy:
            return False
        self._job_number += 1
        number, signals, closing = self._job_number, self._signals, self._closing
        self._jobs[number] = (callback, refresh)
        self._sync_enabled()
        def execute():
            try:
                if closing.is_set():
                    return
                value, success = function(), True
            except Exception:
                value, success = None, False
            if closing.is_set():
                return
            try:
                signals.completed.emit(number, success, value)
            except RuntimeError:
                pass  # Widget destruction disconnected its QObject receiver.
        self._start_worker(execute, "JarvisControls")
        return True

    @Slot(int, bool, object)
    def _completed(self, number, success, value):
        callback, refresh = self._jobs.pop(number, (None, False))
        if self._closing.is_set():
            return
        if success:
            if callable(callback):
                try:
                    callback(value)
                except Exception:
                    self.status.setText("Не удалось обновить состояние управления. Попробуй обновить список.")
        else:
            self.status.setText("Не удалось выполнить действие. Проверь доступ и выбранный файл; сохранённые данные остаются на месте.")
        self._sync_enabled()
        if refresh or self._refresh_pending:
            self._refresh_pending = False
            self.refresh()

    def refresh(self):
        if self._closing.is_set():
            return
        if self.busy:
            self._refresh_pending = True
            return
        core, remote = self.core, self.remote_status
        def read():
            try:
                report = remote() if callable(remote) else remote
            except Exception:
                report = {"status": "unavailable"}
            return core.projects.roots(), core.projects.projects(), core.apps.apps(), report
        self._run(read, self._apply_snapshot, refresh=False)

    @staticmethod
    def _fill(combo, records, *, field="name"):
        current = combo.currentData()
        current_id = current.get("id") if isinstance(current, dict) else None
        blocked = combo.blockSignals(True)
        combo.clear()
        for record in records:
            combo.addItem(str(record[field]), dict(record))
        for index in range(combo.count()):
            if combo.itemData(index).get("id") == current_id:
                combo.setCurrentIndex(index)
                break
        combo.blockSignals(blocked)

    def _apply_snapshot(self, snapshot):
        roots, projects, apps, remote = snapshot
        self._fill(self.root_combo, roots, field="label")
        self._fill(self.project_combo, projects)
        self._fill(self.app_combo, apps)
        self._project_selected()
        self._remote_report(remote)
        self._poll_owner()

    def _remote_report(self, report):
        states = {"ready": "Подключение готово.", "running": "Служба подключения работает.", "active": "Удалённый доступ активен.",
                  "disabled": "Удалённый доступ выключен.", "stopped": "Служба подключения остановлена.",
                  "not_configured": "Подключение с телефона ещё не настроено.", "owner_required": "Сначала подтверди владельца.",
                  "pending": "Подключение ещё готовится.", "unavailable": "Подключение сейчас недоступно."}
        state = report.get("status", report.get("state")) if isinstance(report, dict) else None
        state = state if isinstance(state, str) else None
        text = states.get(state, "Готовность удалённого подключения пока не подтверждена.")
        if isinstance(report, dict) and type(report.get("approved")) is bool:
            text += " Разрешение выдано." if report["approved"] else " Разрешение не выдано."
        self.remote_status_label.setText(text)

    def _poll_owner(self):
        if self._closing.is_set():
            return
        try:
            report = self._owner_report()
            enabled = report.get("enabled") is True
            verified = enabled and report.get("verified") is True and self._owner_allowed()
            remaining = report.get("remaining_seconds", 0)
            seconds = remaining if type(remaining) is int and 0 <= remaining <= 900 else 0
            blocked = self.owner_toggle.blockSignals(True)
            self.owner_toggle.setChecked(enabled)
            self.owner_toggle.blockSignals(blocked)
            self.owner_status.setText((f"Владелец подтверждён. Осталось {seconds} сек." if verified else
                                       "Подтверди владельца кнопкой Windows Hello.") if enabled else
                                      "Защита голосовых команд выключена. Для приложений, жестов и удалённого доступа нужна проверка владельца.")
        except Exception:
            self.owner_status.setText("Не удалось проверить доступ владельца; новые разрешения не выдаются.")
        if self._camera_requested.is_set() and not self._owner_allowed():
            self._stop_gestures("Доступ владельца истёк. Останавливаю камеру…")
        self._sync_enabled()

    def _pick_root(self):
        if self.busy or self._closing.is_set():
            return
        path = QFileDialog.getExistingDirectory(self, "Выбери папку с проектами")
        if path:
            catalog = self.core.projects
            self._run(lambda: catalog.register_root(path), lambda _: self.status.setText("Папка добавлена. Нажми «Найти проекты»."))

    def _scan(self):
        root = self.root_combo.currentData()
        if not isinstance(root, dict):
            return
        catalog, root_id = self.core.projects, root["id"]
        def done(result):
            self.status.setText(f"Найдено проектов: {len(result['projects'])}." + (" Достигнут предел поиска; можно добавить нужный проект вручную." if result["truncated"] else ""))
        self._run(lambda: catalog.scan(root_id, max_depth=2, limit=200), done)

    def _remove_root(self):
        root = self.root_combo.currentData()
        if isinstance(root, dict):
            catalog, root_id = self.core.projects, root["id"]
            self._run(lambda: catalog.remove_root(root_id), lambda _: self.status.setText("Папка убрана из доступа Jarvis. Файлы не удалены."))

    def _pick_project(self):
        if self.busy or self._closing.is_set():
            return
        path = QFileDialog.getExistingDirectory(self, "Выбери проект внутри разрешённой папки")
        if path:
            catalog = self.core.projects
            self._run(lambda: catalog.add_project(path), lambda _: self.status.setText("Проект добавлен."))

    def _alias_project(self):
        project = self.project_combo.currentData()
        if not isinstance(project, dict) or self.busy:
            return
        alias, accepted = QInputDialog.getText(self, "Имя проекта", "Короткое уникальное имя для команды:")
        if accepted and alias.strip():
            catalog, project_id = self.core.projects, project["id"]
            aliases = [*project.get("aliases", [])[-9:], alias]
            self._run(lambda: catalog.set_aliases(project_id, aliases), lambda _: self.status.setText("Имя проекта сохранено."))

    def _project_selected(self, *_):
        project = self.project_combo.currentData()
        if isinstance(project, dict):
            index = self.editor_combo.findData(project.get("editor", "explorer"))
            blocked = self.editor_combo.blockSignals(True)
            self.editor_combo.setCurrentIndex(index)
            self.editor_combo.blockSignals(blocked)

    def _editor_changed(self, *_):
        project, editor = self.project_combo.currentData(), self.editor_combo.currentData()
        if isinstance(project, dict) and editor:
            catalog, project_id = self.core.projects, project["id"]
            self._run(lambda: catalog.set_editor(project_id, editor), lambda _: self.status.setText("Редактор проекта сохранён."))

    @staticmethod
    def _unique_name(catalog, record, field):
        for name in (*record.get("aliases", []), record["name"]):
            resolved = catalog.resolve(name)
            if resolved["status"] == "found" and resolved[field]["id"] == record["id"]:
                return name
        raise ValueError("An unambiguous alias is required")

    def _open_project(self):
        project = self.project_combo.currentData()
        if isinstance(project, dict):
            core = self.core
            def execute():
                name = self._unique_name(core.projects, project, "project")
                if self._closing.is_set():
                    return None
                return core.handle("открой проект " + name, source="typed")
            self._run(execute, self._reply)

    def _remove_project(self):
        project = self.project_combo.currentData()
        if isinstance(project, dict):
            catalog, project_id = self.core.projects, project["id"]
            self._run(lambda: catalog.remove(project_id), lambda _: self.status.setText("Проект убран из списка. Файлы не удалены."))

    def _pick_app(self):
        if self.busy or not self._owner_allowed():
            self.status.setText("Для добавления приложения подтверди владельца через Windows Hello.")
            return
        path, _filter = QFileDialog.getOpenFileName(self, "Выбери приложение", "", "Приложения Windows (*.exe)")
        if not path:
            return
        alias, accepted = QInputDialog.getText(self, "Имя приложения", "Короткое имя для команды:")
        if not accepted or not alias.strip():
            return
        catalog = self.core.apps
        def register():
            if not self._owner_allowed():
                raise PermissionError("Owner access expired")
            return catalog.register_app(path, alias, approval=True, approval_guard=self._owner_allowed)
        self._run(register, lambda _: self.status.setText("Приложение добавлено. После изменения файла потребуется новое подтверждение."))

    def _open_app(self):
        app = self.app_combo.currentData()
        if isinstance(app, dict):
            core = self.core
            def execute():
                name = self._unique_name(core.apps, app, "app")
                if self._closing.is_set():
                    return None
                return core.handle("открой приложение " + name, source="typed")
            self._run(execute, self._reply)

    def _remove_app(self):
        app = self.app_combo.currentData()
        if isinstance(app, dict):
            catalog, app_id = self.core.apps, app["id"]
            self._run(lambda: catalog.remove(app_id), lambda _: self.status.setText("Разрешение удалено из Jarvis. Приложение остаётся на компьютере."))

    def _reply(self, reply):
        if reply is None:
            return
        text = getattr(reply, "text", None)
        self.status.setText(text[:2000] if isinstance(text, str) else "Действие обработано.")
        if callable(self.on_reply):
            self.on_reply(reply)

    def _owner_changed(self, enabled):
        if self._closing.is_set():
            return
        core = self.core
        def change():
            return core.set_owner_required(bool(enabled))
        def done(accepted):
            if accepted is not True:
                self.status.setText("Чтобы выключить защиту, сначала подтверди владельца.")
            elif not enabled:
                self._stop_gestures()
            self._poll_owner()
        self._run(change, done)

    def _verify_owner(self):
        if self.busy or self._closing.is_set() or getattr(self.core.owner, "enabled", False) is not True:
            return
        if getattr(self.main_window, "_session_notifications_registered", True) is not True:
            self.core.owner.revoke()
            self.status.setText("Windows не подключила проверку блокировки сеанса. Перезапусти Jarvis; доступ владельца не выдан.")
            return
        # QWidget.winId belongs to the GUI thread. The worker gets only int.
        window = self.main_window if self.main_window is not None else self.window()
        hwnd = int(window.winId())
        owner, closing = self.core.owner, self._closing
        self.status.setText("Подтверди владельца в окне Windows Hello…")
        def done(result):
            message = getattr(result, "message", "Проверка завершена; состояние доступа показано выше.")
            self.status.setText(message[:300] if isinstance(message, str) else "Проверка завершена.")
            self._poll_owner()
        self._run(lambda: owner.verify(hwnd, cancel=closing), done)

    def _save_camera_preference(self, enabled):
        try:
            self.core.store.set_preferences({"gestures_enabled": bool(enabled)})
        except Exception:
            pass  # A camera is never enabled merely because storage failed.

    def _gestures_changed(self, enabled):
        if not enabled:
            self._stop_gestures()
            return
        if not self._owner_allowed() or self._stopping or self._closing.is_set() or self.busy:
            self._set_camera_checked(False)
            self.gesture_status.setText("Камера выключена. Сначала подтверди владельца.")
            return
        self._gesture_generation += 1
        generation, signals = self._gesture_generation, self._signals
        self._camera_requested.set()
        self._save_camera_preference(True)
        try:
            service = GestureService(
                lambda intent: self._emit(signals.gesture, generation, intent),
                lambda state: self._emit(signals.camera_state, generation, state),
                owner_gate=lambda _intent=None: self._camera_requested.is_set() and self._owner_allowed())
        except Exception:
            self._camera_requested.clear()
            self._set_camera_checked(False)
            self._save_camera_preference(False)
            self.gesture_status.setText("Камера выключена: компоненты жестов недоступны.")
            return
        self._gesture_service = service
        self.gesture_status.setText("Подготавливаю жесты. Камера включается только в этом сеансе…")
        def started(success):
            if generation != self._gesture_generation or not self._camera_requested.is_set():
                return
            if success is not True:
                self.status.setText("Не удалось включить жесты. Проверь компоненты жестов и доступ владельца.")
                self._stop_gestures("Камера не включена. Проверь компоненты жестов и доступ владельца.")
        self._run(service.start, started, refresh=False)

    def _emit(self, signal, *values):
        if self._closing.is_set():
            return
        try:
            signal.emit(*values)
        except RuntimeError:
            pass

    def _set_camera_checked(self, enabled):
        blocked = self.gesture_toggle.blockSignals(True)
        self.gesture_toggle.setChecked(enabled)
        self.gesture_toggle.blockSignals(blocked)

    def _stop_gestures(self, message="Останавливаю камеру…"):
        self._camera_requested.clear()
        previous_generation = self._gesture_generation
        self._gesture_generation += 1
        self._set_camera_checked(False)
        self._save_camera_preference(False)
        service, self._gesture_service = self._gesture_service, None
        if service is None:
            self.gesture_status.setText(message if self._stopping else "Камера выключена.")
            self._sync_enabled()
            return
        generation, signals = previous_generation, self._signals
        self._stopping[generation] = service
        self.gesture_status.setText(message)
        self._sync_enabled()
        def stop():
            try:
                stopped = service.stop() is True
            except Exception:
                stopped = False
            self._emit(signals.camera_stopped, generation, stopped)
        self._start_worker(stop, "JarvisGestureStop")

    @Slot(int, bool)
    def _camera_stopped(self, generation, stopped):
        if self._closing.is_set():
            return
        if stopped:
            self._stopping.pop(generation, None)
        self.gesture_status.setText("Камера выключена." if not self._stopping else "Камера ещё останавливается. Дождись завершения.")
        self._sync_enabled()

    @Slot(int, str)
    def _camera_state(self, generation, message):
        if self._closing.is_set():
            return
        if generation in self._stopping and message == "Камера выключена.":
            self._stopping.pop(generation, None)
            self.gesture_status.setText("Камера выключена." if not self._stopping else "Камера ещё останавливается. Дождись завершения.")
            self._sync_enabled()
            return
        if not self._closing.is_set() and generation == self._gesture_generation and self._camera_requested.is_set():
            self.gesture_status.setText(message[:300])
            if message == "Камера выключена.":
                self._stop_gestures()

    @Slot(int, str)
    def _gesture_intent(self, generation, intent):
        if (self._closing.is_set() or generation != self._gesture_generation or intent not in _GESTURES
                or not self._camera_requested.is_set() or not self._owner_allowed() or self.busy):
            return
        core, closing, requested = self.core, self._closing, self._camera_requested
        def execute():
            if (closing.is_set() or not requested.is_set() or generation != self._gesture_generation
                    or not self._owner_allowed()):
                return None
            return core.request_control(intent, source="gesture")
        self._run(execute, lambda reply: self._reply(reply) if reply is not None else None, refresh=False)

    def _approve_remote(self):
        if not self._owner_allowed():
            self.status.setText("Сначала подтверди владельца через Windows Hello.")
            return
        core = self.core
        def approve():
            return core.approve_remote(True) if self._owner_allowed() else False
        self._run(approve, lambda accepted: self.status.setText("Разрешение выдано. Для подключения нужна настроенная служба." if accepted is True else "Разрешение не выдано; подтверди владельца заново."))

    def _revoke_remote(self):
        core = self.core
        self._run(lambda: core.approve_remote(False), lambda _: self.status.setText("Удалённое разрешение отозвано."))

    def shutdown(self):
        if self._closing.is_set():
            return
        self._closing.set()
        self._jobs.clear()
        self.owner_timer.stop()
        self._stop_gestures()
        try:
            self.core.owner.revoke()
        except Exception:
            pass
        try:
            self.core.approve_remote(False)
        except Exception:
            pass

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
