"""Explicit private phone setup; construction never probes or changes services.

The parent window must call shutdown() before releasing its core. Workers own
no QWidget references; their QObject bridge is parented to the panel, weakly
referenced, and receives only queued results. URLs/secrets stay in RAM and are
never interpolated into labels, errors, logs, or diagnostic snapshots.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit
import weakref

from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QVBoxLayout, QWidget


_OFFICIAL_INSTALLER = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
_MAX_INSTALLER_BYTES = 100 * 1024 * 1024
_MESSAGES = {
    "not_installed": "Tailscale ещё не установлен. Загрузи официальный установщик, затем открой его отдельной кнопкой.",
    "untrusted_installation": "Не удалось подтвердить подпись Tailscale. Запуск не разрешён.",
    "login_required": "Войди в Tailscale через значок Windows и подключи iPhone к тому же аккаунту.",
    "not_running": "Клиент Tailscale ещё не готов. Подключи его через значок Windows.",
    "response_invalid": "Не удалось безопасно проверить состояние. Проверь Tailscale и повтори проверку.",
    "timeout": "Проверка не завершилась вовремя. Настройка не подтверждена.",
    "ready": "Частный HTTPS-маршрут готов. Подключение iPhone ещё нужно проверить отдельно.",
    "serve_required": "Tailscale подключён. После подтверждения владельца можно настроить частный HTTPS.",
    "approval_required": "Сначала включи защиту владельца и подтверди себя через Windows Hello в разделе выше.",
    "serve_conflict": "HTTPS уже используется другой настройкой. Jarvis не будет заменять её автоматически.",
    "needs_user_consent": "Tailscale требует твоего согласия на HTTPS. Открой страницу отдельной кнопкой, затем повтори настройку.",
    "permission_required": "Проверь разрешения в приложении Tailscale. Автоматического повышения прав нет.",
    "serve_unconfirmed": "Нужный частный HTTPS-маршрут пока не подтверждён.",
    "download_failed": "Не удалось подготовить проверенный официальный установщик.",
    "installer_ready": "Установщик загружен и его подпись проверена. Он ещё не запущен; используй отдельную кнопку.",
}
_STEPS = ("1. Установи Tailscale на Windows и войди через его значок.\n"
          "2. Установи Tailscale на iPhone и войди в тот же аккаунт.\n"
          "3. Подтверди владельца в Jarvis и нажми «Настроить частный HTTPS».")


def _label(text):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


def _owner_allowed(core, closing):
    try:
        return not closing.is_set() and core.owner.enabled is True and core.owner.allowed() is True
    except Exception:
        return False


def _origin(value):
    if not isinstance(value, str) or not value.isascii() or len(value) > 300:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if (parsed.scheme == "https" and hostname and value == "https://" + hostname
                and re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){2,}ts\.net", hostname)):
            return value
    except ValueError:
        pass
    return None


def _consent(value):
    if (not isinstance(value, str) or not value.isascii() or not 1 <= len(value) <= 1100
            or "\\" in value or any(char.isspace() or ord(char) < 32 for char in value)):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.netloc == "login.tailscale.com"
                and parsed.hostname == "login.tailscale.com" and parsed.username is None
                and parsed.password is None and not parsed.fragment and parsed.path.startswith("/")):
            return value
    except ValueError:
        pass
    return None


def _strong(value):
    return bool(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value) and len(set(value)) >= 8)


def _write_clipboard(text):
    QApplication.clipboard().setText(text)


def _installer_path(result, state_dir):
    if (not isinstance(result, dict) or not isinstance(result.get("path"), str)
            or not isinstance(result.get("sha256"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", result["sha256"])):
        return None
    try:
        root = Path(state_dir).resolve()
        expected = root / "installers" / "tailscale-setup-latest.exe"
        candidate = Path(result["path"])
        if (not str(root).startswith(("\\\\", "//")) and candidate.is_absolute() and candidate == expected
                and not candidate.is_symlink() and candidate.is_file() and candidate.resolve() == expected):
            return candidate
    except (OSError, ValueError):
        pass
    return None


def _file_state(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _verified_candidate(setup, state_dir):
    """Signature check and bounded hashing happen off the GUI thread.

    The GUI rechecks the file identity/metadata immediately before handing the
    path to Windows, so a replaced installer cannot inherit a queued result.
    """
    result = setup.verified_installer()
    path = _installer_path(result, state_dir)
    if path is None:
        return None
    before = _file_state(path)
    if not 128 <= before[2] <= _MAX_INSTALLER_BYTES:
        return None
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as source:
        while chunk := source.read(65536):
            read += len(chunk)
            if read > _MAX_INSTALLER_BYTES:
                return None
            digest.update(chunk)
    if read != before[2] or _file_state(path) != before or digest.hexdigest() != result["sha256"].lower():
        return None
    return {"path": str(path), "sha256": result["sha256"], "file_state": before}


class _Signals(QObject):
    completed = Signal(int, bool, object)


class RemoteSetupPanel(QWidget):
    def __init__(self, core, *, setup=None, on_configured=None):
        super().__init__()
        self.core, self.on_configured = core, on_configured
        if setup is None:
            from desktop.remote_setup import RemoteSetup
            setup = RemoteSetup(core.store.state_dir)
        self.setup = setup
        self._closing = threading.Event()
        closing = self._closing
        self.destroyed.connect(lambda *_args: closing.set())
        self._signals = _Signals(self)
        self._signals.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
        self._jobs, self._number = {}, 0
        self._worker_lock = threading.Lock()
        self._worker_number, self._worker_threads = 0, {}
        self._origin = self._consent_url = None
        self._build()
        self.access_timer = QTimer(self)
        self.access_timer.setInterval(1000)
        self.access_timer.timeout.connect(self._sync)
        self.access_timer.start()
        self._sync()

    @property
    def busy(self):
        return bool(self._jobs)

    @property
    def shutdown_complete(self):
        """The owner may release Core only after the real worker has exited."""
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

    def _start_worker(self, function):
        done = threading.Event()
        def execute():
            try:
                function()
            finally:
                done.set()
        thread = threading.Thread(target=execute, name="JarvisRemoteSetup", daemon=True)
        with self._worker_lock:
            self._prune_worker_threads()
            self._worker_number += 1
            number = self._worker_number
            self._worker_threads[number] = (thread, done)
        try:
            thread.start()
        except RuntimeError:
            with self._worker_lock:
                self._worker_threads.pop(number, None)
            raise

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("controlCard")
        content = QVBoxLayout(card)
        content.addWidget(_label("Подключить iPhone из любой сети"))
        content.addWidget(_label("Частное подключение через Tailscale. Установка, вход и согласие выполняются тобой; Jarvis не входит в аккаунт и не публикует доступ в интернете."))
        self.status_label = _label("Связь ещё не проверена. Начни с проверки состояния или загрузки установщика.")
        self.status_label.setObjectName("remoteSetupStatus")
        content.addWidget(self.status_label)
        self.steps_label = _label(_STEPS)
        content.addWidget(self.steps_label)
        row = QVBoxLayout()
        self.download_button = self._button("Скачать официальный установщик", self._download)
        self.installer_button = self._button("Проверить и открыть установщик", self._open_installer)
        row.addWidget(self.download_button)
        row.addWidget(self.installer_button)
        content.addLayout(row)
        self.refresh_button = self._button("Проверить состояние", self.refresh)
        self.configure_button = self._button("Настроить частный HTTPS", self._configure)
        content.addWidget(self.refresh_button)
        content.addWidget(self.configure_button)
        self.consent_button = self._button("Открыть страницу согласия Tailscale", self._open_consent)
        self.consent_button.setVisible(False)
        content.addWidget(self.consent_button)
        content.addWidget(_label("После настройки скопируй ссылку плана в свою вкладку iPhone. Отдельный ключ управления вводится вручную; в ссылку он не добавляется."))
        self.copy_link_button = self._button("Скопировать ссылку плана", self._copy_link)
        self.copy_control_button = self._button("Скопировать отдельный ключ управления", self._copy_control)
        self.copy_link_button.setEnabled(False)
        self.copy_control_button.setEnabled(False)
        content.addWidget(self.copy_link_button)
        content.addWidget(self.copy_control_button)
        content.addWidget(_label("Ключи здесь не показываются. Копирование требует действующего подтверждения владельца. Готовность HTTPS не означает, что iPhone уже подключён."))
        layout.addWidget(card)
        self._buttons = (self.download_button, self.installer_button, self.refresh_button,
                         self.configure_button, self.consent_button, self.copy_link_button, self.copy_control_button)

    @staticmethod
    def _button(text, callback):
        widget = QPushButton(text)
        widget.clicked.connect(callback)
        return widget

    def _sync(self):
        enabled = not self.busy and not self._closing.is_set()
        allowed = enabled and _owner_allowed(self.core, self._closing)
        for widget in self._buttons:
            widget.setEnabled(enabled)
        self.configure_button.setEnabled(allowed)
        self.consent_button.setEnabled(allowed and self._consent_url is not None)
        self.copy_link_button.setEnabled(allowed and self._origin is not None)
        self.copy_control_button.setEnabled(allowed and self._origin is not None)

    def _run(self, kind, function):
        if self._closing.is_set() or self.busy:
            return False
        self._number += 1
        number, closing, signal_ref = self._number, self._closing, weakref.ref(self._signals)
        self._jobs[number] = kind
        if kind in {"status", "configure"}:
            self._origin = self._consent_url = None
            self.consent_button.setVisible(False)
        self.status_label.setText({"status": "Проверяю частное подключение…", "configure": "Настраиваю частный HTTPS…",
                                   "download": "Загружаю и проверяю официальный установщик…",
                                   "installer": "Проверяю подпись установщика перед открытием…"}.get(kind, "Проверяю настройку…"))
        self._sync()
        def execute():
            if closing.is_set():
                return
            try:
                value, success = function(), True
            except Exception:
                value, success = None, False
            if closing.is_set():
                return
            signals = signal_ref()
            if signals is not None:
                try:
                    signals.completed.emit(number, success, value)
                except RuntimeError:
                    pass
        try:
            self._start_worker(execute)
        except RuntimeError:
            self._jobs.pop(number, None)
            self.status_label.setText("Не удалось начать проверку. Попробуй ещё раз.")
            self._sync()
            return False
        return True

    def refresh(self):
        setup = self.setup
        self._run("status", setup.status)

    def _download(self):
        setup, closing = self.setup, self._closing
        def download():
            info = setup.installer_info()
            if (not isinstance(info, dict) or info.get("url") != _OFFICIAL_INSTALLER
                    or info.get("runs_automatically") is not False
                    or info.get("requires_user_installation") is not True
                    or info.get("requires_windows_and_iphone_login") is not True
                    or type(info.get("max_bytes")) is not int or info["max_bytes"] != _MAX_INSTALLER_BYTES):
                return None
            return setup.download_installer(cancel_event=closing)
        self._run("download", download)

    def _open_installer(self):
        if self._closing.is_set() or self.busy:
            return
        setup, state_dir = self.setup, self.core.store.state_dir
        self._run("installer", lambda: _verified_candidate(setup, state_dir))

    def _configure(self):
        if self._closing.is_set() or self.busy:
            return
        core, setup, closing = self.core, self.setup, self._closing
        if not _owner_allowed(core, closing):
            self.status_label.setText(_MESSAGES["approval_required"])
            return
        def configure():
            if not _owner_allowed(core, closing):
                return {"reason": "approval_required"}
            value = setup.configure_serve(approved=True, approval_guard=lambda: _owner_allowed(core, closing))
            return value if _owner_allowed(core, closing) else {"reason": "approval_required"}
        self._run("configure", configure)

    def _valid_installer(self, result):
        path = _installer_path(result, self.core.store.state_dir)
        try:
            if path is not None and result.get("file_state") == _file_state(path):
                return path
        except (OSError, ValueError):
            pass
        return None

    @Slot(int, bool, object)
    def _completed(self, number, success, value):
        kind = self._jobs.pop(number, None)
        if self._closing.is_set() or kind is None:
            return
        try:
            if not success:
                self.status_label.setText("Не удалось безопасно завершить настройку. Проверь состояние и попробуй снова.")
            elif kind == "installer":
                path = self._valid_installer(value)
                accepted = path is not None and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                self.status_label.setText("Установщик передан Windows. Заверши установку в его окне." if accepted else
                                          "Проверенный установщик не готов или Windows не подтвердил запуск. Сначала загрузи его.")
            elif kind == "download":
                prepared = isinstance(value, dict) and value.get("ok") is True and value.get("reason") == "installer_ready" and value.get("launched") is False
                self.status_label.setText(_MESSAGES["installer_ready" if prepared else "download_failed"])
            else:
                self._apply_status(kind, value)
        except Exception:
            self._origin = None
            self.status_label.setText("Не удалось подтвердить настройку Jarvis. Сохранённые данные не сброшены.")
        self._sync()

    def _apply_status(self, kind, value):
        self._origin = None
        self._consent_url = None
        self.consent_button.setVisible(False)
        if not isinstance(value, dict):
            self.status_label.setText(_MESSAGES["response_invalid"])
            return
        reason = value.get("reason")
        reason = reason if isinstance(reason, str) else "response_invalid"
        if kind == "configure" and not _owner_allowed(self.core, self._closing):
            reason = "approval_required"
        self.status_label.setText(_MESSAGES.get(reason, _MESSAGES["response_invalid"]))
        if reason == "needs_user_consent":
            self._consent_url = _consent(value.get("consent_url"))
            self.consent_button.setVisible(self._consent_url is not None)
        origin = _origin(value.get("origin"))
        if (reason == "ready" and value.get("serve_configured") is True and value.get("installed") is True
                and value.get("logged_in") is True and origin is not None):
            self._origin = origin
            if kind == "configure" and callable(self.on_configured):
                if not _owner_allowed(self.core, self._closing):
                    self._origin = None
                    self.status_label.setText(_MESSAGES["approval_required"])
                    return
                applied = self.on_configured(origin)
                if not _owner_allowed(self.core, self._closing):
                    self._origin = None
                    self.status_label.setText(_MESSAGES["approval_required"])
                elif applied is False:
                    self._origin = None
                    self.status_label.setText("HTTPS-маршрут готов, но настройки Jarvis ещё не подтверждены.")
        else:
            self._origin = None
            if reason == "ready":
                self.status_label.setText(_MESSAGES["serve_unconfirmed"])

    def _open_consent(self):
        if self._closing.is_set() or self.busy:
            return
        url = _consent(self._consent_url)
        if not _owner_allowed(self.core, self._closing):
            self.status_label.setText(_MESSAGES["approval_required"])
        else:
            try:
                opened = url is not None and QDesktopServices.openUrl(QUrl(url)) is True
            except Exception:
                opened = False
            self.status_label.setText("Страница согласия открыта. Заверши её самостоятельно, затем повтори настройку HTTPS."
                                      if opened else "Не удалось открыть проверенную страницу согласия Tailscale.")

    def _pairing_values(self):
        if self._closing.is_set() or self.busy:
            return None
        if not _owner_allowed(self.core, self._closing):
            self.status_label.setText(_MESSAGES["approval_required"])
            return None
        origin = _origin(self._origin)
        if (origin is None or os.environ.get("JARVIS_PHONE_ENABLED") != "1"
                or _origin(os.environ.get("JARVIS_PHONE_ORIGIN")) != origin):
            self.status_label.setText("Сначала примени частный HTTPS к настройке Jarvis и проверь состояние.")
            return None
        return origin

    def _copy_link(self):
        origin = self._pairing_values()
        key = os.environ.get("JARVIS_PHONE_TOKEN", "")
        if origin is None:
            return
        if not _strong(key):
            self.status_label.setText("Ключ плана ещё не настроен. Проверь частную настройку Jarvis.")
            return
        if _owner_allowed(self.core, self._closing):
            try:
                _write_clipboard(origin + "/#token=" + key)
                self.status_label.setText("Ссылка плана скопирована. Передай её только своей вкладке iPhone.")
            except Exception:
                self.status_label.setText("Не удалось скопировать ссылку. Попробуй ещё раз.")
        else:
            self.status_label.setText(_MESSAGES["approval_required"])

    def _copy_control(self):
        if self._pairing_values() is None:
            return
        key, planner = os.environ.get("JARVIS_REMOTE_TOKEN", ""), os.environ.get("JARVIS_PHONE_TOKEN", "")
        if os.environ.get("JARVIS_REMOTE_ENABLED") != "1" or not _strong(key) or not _strong(planner) or key == planner:
            self.status_label.setText("Отдельный ключ управления ещё не настроен. Он должен отличаться от ключа плана.")
            return
        if _owner_allowed(self.core, self._closing):
            try:
                _write_clipboard(key)
                self.status_label.setText("Отдельный ключ скопирован. Введи его вручную в панели управления на iPhone.")
            except Exception:
                self.status_label.setText("Не удалось скопировать ключ. Попробуй ещё раз.")
        else:
            self.status_label.setText(_MESSAGES["approval_required"])

    def shutdown(self):
        self._closing.set()
        self.access_timer.stop()
        self._jobs.clear()
        self._origin = self._consent_url = None
        self._sync()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
