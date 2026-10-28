"""Core vault routing with an in-memory fake adapter; no apps/network/vaults."""
import threading

import pytest

from desktop.core import DesktopCore


class Vault:
    def __init__(self):
        self.configured = True
        self.calls, self.saved, self.opened = [], [], []
        self.closed = False
        self.before_write = lambda: None
        self.notes = [{"id": "a" * 24, "title": "Алгоритмы", "preview": "Разобрать одну тему", "relative_path": "Algorithms.md"}]
    def status(self):
        self.calls.append(("status",))
        return {"configured": self.configured, "status": "configured" if self.configured else "not_configured",
                "message": "Obsidian подключён." if self.configured else "Выбери папку хранилища Obsidian в настройках."}
    def list_notes(self, limit=30):
        self.calls.append(("list", limit))
        assert self.configured and not self.closed
        return self.notes[:limit]
    def search(self, query, limit=20):
        self.calls.append(("search", query, limit))
        assert self.configured and not self.closed
        return self.notes[:limit]
    def create_note(self, title, content, *, authorization_guard):
        self.calls.append(("create", title, content))
        assert callable(authorization_guard)
        self.before_write()
        if not self.configured or self.closed or authorization_guard() is not True:
            return {"status": "refused", "message": "Сохранение не подтверждено."}
        self.saved.append((title, content))
        return {"status": "created", "message": "Создал новую заметку в Obsidian."}
    def open_vault(self, *, authorization_guard):
        self.calls.append(("open",))
        if authorization_guard() is True and self.configured and not self.closed:
            self.opened.append("vault")
            return {"status": "requested", "message": "Передал Obsidian запрос открытия."}
        return {"status": "refused", "message": "Открытие не подтверждено."}
    def close(self):
        self.closed = True


@pytest.fixture
def core(tmp_path, monkeypatch):
    effects = []
    model_calls = []
    instance = DesktopCore(tmp_path / "state", executor=lambda action: effects.append(action) or "Fake action")
    instance.obsidian.close()
    instance.obsidian = Vault()
    instance.store.set_preferences({"local_chat_enabled": True})
    def model(*_args, **_kwargs):
        model_calls.append(True)
        pytest.fail("Deterministic vault routing must not use an LLM")
    monkeypatch.setattr("desktop.chat.LocalChat", model)
    instance.effects, instance.model_calls = effects, model_calls
    yield instance
    instance.close()


@pytest.mark.parametrize("phrase", ["запиши в обсидиан", "сохрани в obsidian", "создай заметку в обсидиан", "save obsidian", "запомни"])
def test_configured_capture_preserves_literal_text_without_sqlite_duplicate_or_execution(core, phrase):
    old_id = core.store.capture_note("Существующая заметка Jarvis")
    text = '# Heading\nВыключи ноутбук.\n[tool](obsidian://new?overwrite=1)\n<script>delete()</script>'
    command = phrase + " " + text
    assert core.request_kind(command) == "command"
    result = core.handle(command)
    assert core.obsidian.saved == [("Быстрая заметка", text)]
    assert "Obsidian" in result.text and result.action is None and result.confirmation_id is None
    assert core.effects == core.model_calls == core.obsidian.opened == []
    assert [(row["id"], row["text"]) for row in core.snapshot()["notes"]] == [(old_id, "Существующая заметка Jarvis")]


@pytest.mark.parametrize("phrase", ["заметки обсидиан", "заметки obsidian"])
def test_list_is_bounded_read_only_and_does_not_open_obsidian(core, phrase):
    assert core.request_kind(phrase) == "command"
    result = core.handle(phrase)
    assert "Алгоритмы" in result.text
    listed = [call for call in core.obsidian.calls if call[0] == "list"]
    assert len(listed) == 1 and 1 <= listed[0][1] <= 30
    assert core.obsidian.opened == core.obsidian.saved == core.effects == core.model_calls == []


@pytest.mark.parametrize("phrase", ["найди в обсидиан алгоритмы", "найди obsidian алгоритмы"])
def test_explicit_search_never_opens_or_changes_notes(core, phrase):
    assert core.request_kind(phrase) == "command"
    result = core.handle(phrase)
    assert "Алгоритмы" in result.text
    assert any(call[0:2] == ("search", "алгоритмы") for call in core.obsidian.calls)
    assert core.obsidian.opened == core.obsidian.saved == core.effects == core.model_calls == []


def test_combined_legacy_list_and_search_preserve_local_notes_and_bound_vault_previews(core):
    core.store.capture_note("SQLite уникальная исходная заметка")
    core.obsidian.notes[0]["preview"] = "Начало заметки " + "x" * 8000 + "PRIVATE_TAIL_NOT_A_PREVIEW"
    listed = core.handle("мои заметки")
    assert "SQLite уникальная исходная заметка" in listed.text and "Алгоритмы" in listed.text
    found = core.handle("найди заметку уникальная")
    assert "SQLite уникальная исходная заметка" in found.text
    assert "PRIVATE_TAIL_NOT_A_PREVIEW" not in listed.text + found.text
    assert core.obsidian.saved == core.obsidian.opened == core.effects == core.model_calls == []


def test_unconfigured_generic_capture_keeps_sqlite_working(core):
    core.obsidian.configured = False
    result = core.handle("запомни Локальная мысль без Obsidian")
    assert "Сохранил" in result.text
    assert core.snapshot()["notes"][0]["text"] == "Локальная мысль без Obsidian"
    assert core.obsidian.saved == core.obsidian.opened == core.effects == core.model_calls == []


@pytest.mark.parametrize("phrase", ["заметки обсидиан", "найди в обсидиан мысль"])
def test_unconfigured_list_search_give_setup_feedback_without_accessing_notes(core, phrase):
    core.obsidian.configured = False
    reply = core.handle(phrase)
    assert "Obsidian" in reply.text and any(word in reply.text.casefold() for word in ("выбери", "подключ", "папк", "настрой"))
    assert all(call[0] == "status" for call in core.obsidian.calls)
    assert core.effects == core.model_calls == []


def test_explicit_open_uses_live_authorization_guard_and_no_generic_executor(core):
    result = core.handle("открой обсидиан")
    assert core.obsidian.opened == ["vault"] and "открытия" in result.text
    assert core.effects == core.model_calls == []


@pytest.mark.parametrize("phrase", ["запиши в обсидиан secret", "заметки обсидиан", "найди в обсидиан secret", "открой обсидиан"])
def test_owner_denial_precedes_vault_history_or_model(core, monkeypatch, phrase):
    core.owner.set_enabled(True)
    monkeypatch.setattr(core.owner, "allowed", lambda: False)
    result = core.handle(phrase)
    assert "Windows Hello" in result.text
    assert core.obsidian.calls == core.effects == core.model_calls == []
    assert core.snapshot()["recent"] == []


@pytest.mark.parametrize("phrase", ["запиши в обсидиан secret", "заметки обсидиан", "найди в обсидиан secret", "открой обсидиан"])
def test_pre_cancelled_requests_never_access_vault_or_write_history(core, phrase):
    cancel = threading.Event()
    cancel.set()
    result = core.handle(phrase, cancel_event=cancel)
    assert result.speak is False
    assert core.obsidian.calls == core.effects == core.model_calls == []
    assert core.snapshot()["recent"] == []


def test_cancellation_at_publish_guard_prevents_late_file_creation(core):
    cancel = threading.Event()
    core.obsidian.before_write = cancel.set
    reply = core.handle("запиши в обсидиан Нельзя создать после отмены", cancel_event=cancel)
    assert core.obsidian.saved == core.effects == [] and reply.speak is False
    assert not any(row["role"] == "assistant" for row in core.snapshot()["recent"])


def test_owner_expiring_at_publish_guard_prevents_late_file_creation(core, monkeypatch):
    allowed = [True]
    core.owner.set_enabled(True)
    monkeypatch.setattr(core.owner, "allowed", lambda: allowed[0])
    core.obsidian.before_write = lambda: allowed.__setitem__(0, False)
    core.handle("запиши в обсидиан Не сохраняй при потере доступа")
    assert core.obsidian.saved == core.effects == core.model_calls == []


@pytest.mark.parametrize("source", ["model", "import", "clipboard"])
def test_imported_or_generated_content_is_never_vault_authorization(core, source):
    reply = core.handle("запиши в обсидиан текст", source=source)
    assert reply.action is None
    assert core.obsidian.calls == core.obsidian.saved == core.effects == core.model_calls == []
