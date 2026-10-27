import sqlite3

from desktop.personal import load_personal_snapshot


def test_explicit_recovery_handoff_is_used_only_if_primary_missing(monkeypatch, tmp_path):
    from pathlib import Path
    from desktop.personal import default_snapshot_path
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    primary = tmp_path / "local" / "Jarvis" / "recovered-20261010.sqlite3"
    handoff = tmp_path / "home" / "Downloads" / "Jarvis-Recovery" / "recovered-20261010.sqlite3"
    assert default_snapshot_path() == primary
    handoff.parent.mkdir(parents=True)
    handoff.touch()
    assert default_snapshot_path() == handoff
    primary.parent.mkdir(parents=True)
    primary.touch()
    assert default_snapshot_path() == primary


def sample(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE users(telegram_id INTEGER PRIMARY KEY,name TEXT);
            INSERT INTO users VALUES(42,'User');
            CREATE TABLE schedule_entries(id INTEGER,user_id INTEGER,weekday INTEGER,start TEXT,end TEXT,title TEXT,enabled INTEGER);
            INSERT INTO schedule_entries VALUES(1,42,0,'09:00','10:00','Math',1);
            INSERT INTO schedule_entries VALUES(2,43,0,'10:00','11:00','Foreign',1);
            INSERT INTO schedule_entries VALUES(3,42,0,'11:00','12:00','Disabled',0);
            CREATE TABLE assignments(id INTEGER,user_id INTEGER,title TEXT,course TEXT,due_at TEXT,status TEXT,url TEXT);
            INSERT INTO assignments VALUES(1,42,'Project','CS','2026-10-20 10:00:00','pending','https://secret.invalid/lms-token');
            INSERT INTO assignments VALUES(2,43,'Foreign','CS',NULL,'pending',NULL);
            INSERT INTO assignments VALUES(3,42,'Completed','CS',NULL,'done',NULL);
            CREATE TABLE memory_facts(id INTEGER,user_id INTEGER,key TEXT,value TEXT);
            INSERT INTO memory_facts VALUES(1,42,'interest','<b>coding</b>');
            INSERT INTO memory_facts VALUES(2,43,'foreign','private');
            INSERT INTO memory_facts VALUES(3,42,'API_TOKEN','do-not-show');
            INSERT INTO memory_facts VALUES(4,42,'resource','https://private.invalid/a?token=secret');
            CREATE TABLE provider_usage(secret TEXT);
            INSERT INTO provider_usage VALUES('NEVER_READ');
        """)


def test_missing_source_never_created(tmp_path):
    path = tmp_path / "missing.db"
    result = load_personal_snapshot(path)
    assert result["status"] == "missing"
    assert not path.exists()


def test_owner_filter_selected_columns_and_read_only(tmp_path):
    path = tmp_path / "recovered.db"
    sample(path)
    before = path.read_bytes()
    result = load_personal_snapshot(path)
    assert result["status"] == "ready" and result["snapshot_date"] == "2026-10-10"
    assert [row["title"] for row in result["schedule"]] == ["Math"]
    assert [row["title"] for row in result["assignments"]] == ["Project"]
    assert "url" not in result["assignments"][0]
    assert result["memory"][0]["value"] == "<b>coding</b>"
    assert all(row["key"] != "API_TOKEN" for row in result["memory"])
    assert result["memory"][1]["value"] == "[ссылка скрыта]"
    assert "NEVER_READ" not in str(result) and "Foreign" not in str(result)
    assert path.read_bytes() == before


def test_multiple_users_are_not_guessed(tmp_path):
    path = tmp_path / "recovered.db"
    sample(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users VALUES(43,'Other')")
    result = load_personal_snapshot(path)
    assert result["status"] == "selection_required"
    assert not result["memory"] and not result["assignments"]


def test_old_partial_database_is_still_usable(tmp_path):
    path = tmp_path / "partial.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE users(telegram_id INTEGER,name TEXT); INSERT INTO users VALUES(42,'User');")
    result = load_personal_snapshot(path)
    assert result["status"] == "ready"
    assert set(result["unavailable"]) == {"schedule", "assignments", "memory"}


def test_corrupt_source_returns_fixed_error(tmp_path):
    path = tmp_path / "bad.db"
    path.write_text("not-a-database", encoding="utf-8")
    assert load_personal_snapshot(path)["status"] == "unavailable"


def test_section_views_are_not_followed(tmp_path):
    path = tmp_path / "view.db"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE users(telegram_id INTEGER,name TEXT); INSERT INTO users VALUES(42,'User');
            CREATE TABLE private_keys(secret TEXT); INSERT INTO private_keys VALUES('NEVER_READ');
            CREATE VIEW memory_facts AS SELECT 1 id,42 user_id,'key' key,secret value FROM private_keys;""")
    result = load_personal_snapshot(path)
    assert result["status"] == "ready" and result["memory"] == []
    assert "NEVER_READ" not in str(result)


def test_zero_users_or_missing_identity_does_not_expose_rows(tmp_path):
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users(telegram_id INTEGER,name TEXT)")
    assert load_personal_snapshot(path)["status"] == "identity_missing"


def test_snapshot_limits_are_reported(tmp_path):
    path = tmp_path / "many.db"
    sample(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO memory_facts VALUES(?,42,'fact','text')", [(index,) for index in range(10, 230)])
    result = load_personal_snapshot(path)
    assert len(result["memory"]) <= 200
    assert "memory" in result["truncated"]
