"""Selected local IDs preserve owner policy without a generated text command."""
import struct
from types import SimpleNamespace

import pytest

from desktop import app_catalog
from desktop.core import DesktopCore


@pytest.fixture
def selected(tmp_path):
    calls = []
    core = DesktopCore(tmp_path / "state", executor=lambda _: pytest.fail("Unexpected built-in action"))
    root = tmp_path / "projects"
    root.mkdir()
    core.projects.register_root(root)
    records = []
    for name in ("first", "second"):
        folder = root / name
        folder.mkdir()
        records.append(core.projects.add_project(folder, alias="Same name"))
    core.projects.executor = lambda path, editor: calls.append((path, editor))
    yield core, calls, records
    core.close()


def test_selected_id_opens_correct_duplicate_without_alias_guessing(selected):
    core, calls, records = selected
    assert core.projects.resolve("Same name")["status"] == "ambiguous"
    reply = core.open_catalog_item("project", records[1]["id"])
    assert "Same name" in reply.text and reply.confirmation_id is None
    assert calls == [(core.projects.state_dir.parent / "projects" / "second", "explorer")]


@pytest.mark.parametrize("source", ["model", "voice", "typed", "remote", "import", None, []])
def test_only_local_button_adapter_can_open_an_id(selected, source):
    core, calls, records = selected
    before = core.snapshot()["recent"]
    core.open_catalog_item("project", records[0]["id"], source=source)
    assert not calls and core.snapshot()["recent"] == before


@pytest.mark.parametrize("kind,identifier", [("other", 1), ([], 1), ("project", True),
                                            ("project", 0), ("app", "1"), ("project", -1)])
def test_invalid_selection_is_not_an_action(selected, kind, identifier):
    core, calls, _ = selected
    core.open_catalog_item(kind, identifier)
    assert not calls and not core.snapshot()["recent"]


def test_unverified_owner_denied_before_selection_lookup_or_history(selected, monkeypatch):
    core, calls, records = selected
    core.owner = SimpleNamespace(enabled=True, allowed=lambda: False, revoke=lambda: None)
    monkeypatch.setattr(core.projects, "open_project_id", lambda *_args, **_kwargs: pytest.fail("Unverified lookup"))
    assert "Windows Hello" in core.open_catalog_item("project", records[0]["id"]).text
    assert not calls and not core.snapshot()["recent"]


def test_removed_selected_id_never_becomes_a_new_project(selected):
    core, calls, records = selected
    old = records[-1]["id"]
    path = records[-1]["path"]
    core.projects.remove(old)
    new = core.projects.add_project(path, alias="Replacement")
    assert new["id"] > old
    core.open_catalog_item("project", old)
    assert not calls
    core.open_catalog_item("project", new["id"])
    assert len(calls) == 1


def test_owner_rechecked_after_executable_hashing(selected, tmp_path, monkeypatch):
    core, calls, _ = selected
    path = tmp_path / "Useful.exe"
    content = bytearray(1024)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 60, 128)
    struct.pack_into("<4sHHIIIHH", content, 128, b"PE\0\0", 0x8664, 1, 0, 0, 0, 240, 2)
    struct.pack_into("<H", content, 152, 0x20B)
    path.write_bytes(content)
    core.apps.inspector = lambda _: "Useful.exe"
    core.apps.executor = lambda executable: calls.append(executable)
    record = core.apps.register_app(path, "Useful", approval=True)
    authorized = [True]
    core.owner = SimpleNamespace(enabled=True, allowed=lambda: authorized[0], revoke=lambda: None)
    fingerprint = app_catalog._fingerprint
    def revoke_after_read(executable):
        result = fingerprint(executable)
        authorized[0] = False
        return result
    monkeypatch.setattr(app_catalog, "_fingerprint", revoke_after_read)
    reply = core.open_catalog_item("app", record["id"])
    assert reply.text and not calls


def test_core_close_revokes_selection_and_ignores_late_history(selected):
    core, calls, records = selected
    before = core.snapshot()["recent"]
    core.close()
    core.open_catalog_item("project", records[0]["id"])
    core.handle("открой браузер")
    assert not calls and core.snapshot()["recent"] == before
