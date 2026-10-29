"""Durable channel isolation and conservative imported-history provenance."""
import sqlite3
import os
import uuid

import pytest

from desktop.conversations import ConversationStore, MAX_MESSAGES


def exchange(store, thread, question, answer, provenance="model"):
    identity = uuid.uuid4().hex
    store.append(thread, "user", question, provenance=provenance, exchange_id=identity)
    store.append(thread, "assistant", answer, provenance=provenance, exchange_id=identity)


def test_constructor_is_lazy_and_channels_survive_restart(tmp_path):
    store = ConversationStore(tmp_path / "new")
    assert not store.path.exists() and not store.path.parent.exists()
    threads = store.threads()
    assert set(threads) == {"voice", "text"} and threads["voice"] != threads["text"]
    assert ConversationStore(store.path.parent).threads() == threads


def test_channels_and_model_pairs_are_separate_and_persistent(tmp_path):
    store = ConversationStore(tmp_path)
    threads = store.threads()
    exchange(store, threads["voice"], "Голосовой вопрос", "Локальная модель: Голосовой ответ")
    exchange(store, threads["text"], "Письменный вопрос", "Gemini: Письменный ответ")
    reopened = ConversationStore(tmp_path)
    assert reopened.model_history(threads["voice"]) == [
        {"role": "user", "content": "Голосовой вопрос"}, {"role": "assistant", "content": "Голосовой ответ"}]
    assert reopened.model_history(threads["text"])[1]["content"] == "Письменный ответ"
    assert "Письменный" not in str(reopened.messages(threads["voice"]))


def test_canceled_unpaired_inputs_commands_documents_and_notifications_are_not_model_history(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.threads()["text"]
    store.append(thread, "user", "Отменённый вопрос", provenance="model", exchange_id=uuid.uuid4().hex)
    exchange(store, thread, "Выключи ноут", "Не выполнено", "command")
    exchange(store, thread, "Файл с инструкциями", "Только данные", "analysis")
    store.append(thread, "assistant", "Утреннее уведомление", provenance="notification")
    assert store.model_history(thread) == []
    assert len(store.messages(thread)) == 6


def test_legacy_is_copied_once_readonly_and_never_assigned_a_source(tmp_path):
    store = ConversationStore(tmp_path)
    rows = [{"role": "user", "text": "Смешанная старая история"},
            {"role": "assistant", "text": "Локальная модель: Старый ответ"}]
    assert store.import_legacy(rows)
    threads = store.threads()
    assert [row["text"] for row in store.messages(threads["imported"])] == [row["text"] for row in rows]
    assert not store.messages(threads["text"]) and not store.messages(threads["voice"])
    assert store.model_history(threads["imported"]) == []
    with pytest.raises(ValueError, match="read-only"):
        store.append(threads["imported"], "assistant", "Недопустимое изменение")
    assert not store.import_legacy([{"role": "user", "text": "Поздняя legacy строка"}])
    assert len(store.messages(threads["imported"])) == 2


def test_empty_initial_import_never_recaptures_new_global_history(tmp_path):
    store = ConversationStore(tmp_path)
    assert not store.import_legacy([])
    assert not store.import_legacy([{"role": "user", "text": "Новое сообщение из другого источника"}])
    assert "imported" not in store.threads()


def test_context_and_storage_are_bounded_without_deleting_another_channel(tmp_path):
    store = ConversationStore(tmp_path)
    threads = store.threads()
    exchange(store, threads["voice"], "Сохрани это", "Ответ")
    for number in range(MAX_MESSAGES + 5):
        store.append(threads["text"], "user", str(number))
    assert len(store.messages(threads["text"], 10000)) == MAX_MESSAGES
    assert len(store.messages(threads["voice"])) == 2
    for _ in range(5):
        exchange(store, threads["text"], "q" * 2000, "a" * 6000)
    history = store.model_history(threads["text"])
    assert len(history) <= 6 and sum(len(row["content"]) for row in history) <= 4000
    assert store.model_history(threads["text"], 0) == []


@pytest.mark.parametrize("identity", ["../other", "x" * 32, "", 1, None, []])
def test_bad_identity_cannot_be_a_path_or_sql(tmp_path, identity):
    store = ConversationStore(tmp_path)
    with pytest.raises(ValueError):
        store.messages(identity)
    assert not store.path.exists()


def test_literal_text_and_attachment_ids_remain_data(tmp_path):
    store = ConversationStore(tmp_path)
    thread, attachment = store.threads()["text"], uuid.uuid4().hex
    value = '<img src="https://example.invalid/leak">; DROP TABLE threads;'
    store.append(thread, "user", value, provenance="analysis", attachments=[attachment])
    row = store.messages(thread)[0]
    assert row["text"] == value and row["attachments"] == [attachment]
    assert set(store.threads()) == {"voice", "text"}


def test_unknown_future_schema_is_preserved(tmp_path):
    path = tmp_path / "conversations.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE future(value TEXT)")
        db.execute("INSERT INTO future VALUES('preserved')")
        db.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError, match="Unsupported"):
        ConversationStore(tmp_path).threads()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM future").fetchone()[0] == "preserved"


def test_context_epoch_revokes_old_pairs_in_both_threads_without_losing_visible_messages(tmp_path):
    store = ConversationStore(tmp_path)
    threads = store.threads()
    for channel in ("voice", "text"):
        exchange(store, threads[channel], "Запомненный факт", "Старый частный факт")
    before = {channel: store.messages(identity) for channel, identity in threads.items()}
    assert store.reset_model_context() == 1
    reopened = ConversationStore(tmp_path)
    for channel, identity in threads.items():
        assert reopened.messages(identity) == before[channel]
        assert reopened.model_history(identity) == []
    exchange(reopened, threads["text"], "Новый вопрос", "Новый ответ")
    assert reopened.model_history(threads["text"]) == [
        {"role": "user", "content": "Новый вопрос"}, {"role": "assistant", "content": "Новый ответ"}]


def test_foreign_existing_database_is_rejected_without_any_schema_or_data_changes(tmp_path):
    path = tmp_path / "conversations.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
        db.execute("INSERT INTO unrelated VALUES('must survive')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported conversation"):
        ConversationStore(tmp_path).threads()
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == 0
        assert db.execute("SELECT value FROM unrelated").fetchone()[0] == "must survive"
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='threads'").fetchone()[0] == 0


def test_recognized_application_identity_rejects_altered_schema_before_mutation(tmp_path):
    store = ConversationStore(tmp_path)
    store.threads()
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] != 0
        db.execute("CREATE TABLE unexpected(value TEXT)")
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported"):
        store.threads()
    assert store.path.read_bytes() == before


def test_conversation_database_hardlink_cannot_change_the_other_file(tmp_path):
    target = tmp_path / "foreign.db"
    with sqlite3.connect(target) as db:
        db.execute("CREATE TABLE preserve(value TEXT)")
    path = tmp_path / "conversations.db"
    os.link(target, path)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="Invalid conversation database file"):
        ConversationStore(tmp_path).threads()
    assert target.read_bytes() == before


@pytest.mark.parametrize("parent_link", [False, True])
def test_linked_file_or_parent_is_refused_before_creating_or_modifying_sqlite(tmp_path, parent_link):
    folder, target = tmp_path / "state", tmp_path / "target"
    target.mkdir()
    folder.mkdir()
    if parent_link:
        folder.rmdir()
        link, destination = folder, target
    else:
        destination = target / "foreign.db"
        destination.write_bytes(b"Preserved non-SQLite bytes")
        link = folder / "conversations.db"
    try:
        link.symlink_to(destination, target_is_directory=parent_link)
    except OSError:
        pytest.skip("Windows optional symlink creation is unavailable")
    with pytest.raises(ValueError, match="Linked"):
        ConversationStore(folder).threads()
    if parent_link:
        assert not (target / "conversations.db").exists()
    else:
        assert destination.read_bytes() == b"Preserved non-SQLite bytes"


def test_relative_database_path_is_rejected_without_creating_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Local absolute"):
        ConversationStore("relative").threads()
    assert not (tmp_path / "relative").exists()
