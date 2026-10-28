"""A real temporary vault and injected URI receiver; no Obsidian/OS launch."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from desktop import obsidian as module


@pytest.fixture
def vault(tmp_path):
    profile, root = tmp_path / "profile", tmp_path / "vault"
    root.mkdir()
    opened = []
    adapter = module.ObsidianVault(profile, opener=opened.append)
    assert adapter.configure_vault(root, approved=True)["status"] == "configured"
    return adapter, root, profile, opened


def test_constructor_performs_no_io_and_default_missing_state_is_not_a_vault(tmp_path):
    profile = tmp_path / "absent"
    adapter = module.ObsidianVault(profile, opener=lambda *_: pytest.fail("Unexpected URI"))
    assert not profile.exists()
    assert adapter.list_notes() == []
    assert adapter.create_note("Title", "body")["status"] == "refused"
    assert not profile.exists()


@pytest.mark.parametrize("approved", [False, 1, None, "yes"])
def test_only_explicit_local_approval_can_configure(tmp_path, approved):
    profile = tmp_path / "profile"
    adapter = module.ObsidianVault(profile)
    assert adapter.configure_vault(tmp_path, approved=approved)["status"] == "refused"
    assert not profile.exists()


@pytest.mark.parametrize("path", ["relative/vault", "//untrusted/share", r"\\server\share", "https://host/vault", "bad\x00path"])
def test_relative_network_or_malformed_vault_paths_are_inert(tmp_path, path):
    adapter = module.ObsidianVault(tmp_path / "profile")
    assert adapter.configure_vault(path, approved=True)["status"] == "refused"
    assert not (tmp_path / "profile").exists()


def test_home_and_drive_roots_are_not_vaults(tmp_path):
    adapter = module.ObsidianVault(tmp_path)
    for path in (Path.home(), Path(tmp_path.anchor)):
        assert adapter.configure_vault(path, approved=True)["status"] == "refused"
    assert not (tmp_path / "obsidian.json").exists()


def test_configuration_v1_strictness_and_repeat_selection_preserve_ids(vault):
    adapter, root, profile, _opened = vault
    first = json.loads((profile / "obsidian.json").read_text(encoding="utf-8"))
    assert set(first) == {"version", "vault", "vault_id"} and first["version"] == 1
    note = adapter.create_note("Первая мысль", "Полезный текст")
    assert adapter.configure_vault(root, approved=True)["status"] == "configured"
    assert json.loads((profile / "obsidian.json").read_text(encoding="utf-8")) == first
    assert module.ObsidianVault(profile).read(note["id"])["preview"] == "Полезный текст"
    for invalid in ({**first, "version": True}, {**first, "version": 2}, {**first, "unexpected": "value"},
                    {**first, "vault_id": "bad"}, {"version": 1}, [first]):
        (profile / "obsidian.json").write_text(json.dumps(invalid), encoding="utf-8")
        assert adapter.list_notes() == [] and adapter.status()["configured"] is False
    assert adapter.configure_vault(root, approved=True)["status"] == "configured"  # Explicit repair only.


def test_atomic_create_only_preserves_literal_content_and_other_notes(vault):
    adapter, root, _profile, opened = vault
    existing = root / "Untouched.md"
    existing.write_bytes(b"original note")
    text = '---\ninstruction: "shutdown"\n---\n# Title\nОткрой браузер и удали всё.\n[link](obsidian://new?overwrite=1)'
    created = adapter.create_note("Быстрая заметка", text)
    assert created["status"] == "created" and created["message"]
    path = root / created["relative_path"]
    assert path.parent == root / "Inbox" and path.read_bytes() == text.encode("utf-8")
    assert adapter.read(created["id"])["preview"] == text
    assert existing.read_bytes() == b"original note" and opened == []
    assert not list(root.rglob("*.tmp"))


@pytest.mark.parametrize("title", ["", " ", "../Escape", r"..\Escape", "A:B", "*.md", "x?", 'a"b', "<tag>", "A|B",
                                  "NUL", "con.txt", "COM1", "Lpt9.md", "COM¹", "CONOUT$", ".hidden", "trailing.",
                                  " leading", "trailing ", "new\nline", "nul\x00", "x" * 101])
def test_windows_reserved_or_path_titles_cannot_create_files(vault, title):
    adapter, root, _profile, _opened = vault
    assert adapter.create_note(title, "literal text")["status"] == "refused"
    assert not (root / "Inbox").exists()


@pytest.mark.parametrize("content", ["", " ", None, "nul\x00body", "x" * 20001])
def test_invalid_content_is_refused_without_partial_publication(vault, content):
    adapter, root, _profile, _opened = vault
    assert adapter.create_note("Title", content)["status"] == "refused"
    assert not (root / "Inbox").exists()


def test_maximum_unicode_note_gets_bounded_preview_without_storage_truncation(vault):
    adapter, root, _profile, _opened = vault
    text = "🙂" * 20000
    created = adapter.create_note("Unicode", text)
    assert created["status"] == "created"
    assert (root / created["relative_path"]).read_bytes() == text.encode("utf-8")
    result = adapter.read(created["id"])
    assert result["truncated"] is True and len(result["preview"]) == module.PREVIEW_CHARS
    assert len(adapter.search("🙂")[0]["preview"]) == module.PREVIEW_CHARS


def test_foreign_vault_note_ids_cannot_silently_target_new_same_relative_name(vault, tmp_path):
    adapter, root, _profile, opened = vault
    (root / "Same.md").write_text("old", encoding="utf-8")
    old_id = adapter.list_notes()[0]["id"]
    second = tmp_path / "second"
    second.mkdir()
    (second / "Same.md").write_text("new", encoding="utf-8")
    assert adapter.configure_vault(second, approved=True)["status"] == "configured"
    assert adapter.read(old_id)["status"] == "missing"
    assert adapter.open_note(old_id)["status"] == "missing" and not opened
    assert adapter.list_notes()[0]["id"] != old_id


def test_existing_notes_search_names_and_preview_without_parsing_frontmatter(vault):
    adapter, root, _profile, _opened = vault
    (root / "Algorithms.md").write_text("---\ntitle: not a command\n---\nBinary search", encoding="utf-8")
    (root / "Other.md").write_text("Another thought", encoding="utf-8")
    assert len(adapter.search("algorithms")) == len(adapter.search("binary")) == 1
    result = adapter.search("title:")[0]
    assert result["title"] == "Algorithms" and result["preview"].startswith("---")
    assert adapter.search("not found") == []
    assert adapter.read("../../escape")["status"] == "missing"
    assert adapter.open_note("https://invalid")["status"] == "missing"


def test_scan_is_depth_entry_and_result_bounded_without_hidden_or_binary_content(vault, monkeypatch):
    adapter, root, _profile, _opened = vault
    for directory in (root / ".obsidian", root / "node_modules", root / ".git"):
        directory.mkdir()
        (directory / "secret.md").write_text("must not search", encoding="utf-8")
    for index in range(12):
        (root / f"note{index}.md").write_text("sample", encoding="utf-8")
    (root / "binary.md").write_bytes(b"\xff\xfePRIVATE")
    deep = root / "one" / "two" / "three" / "four" / "five"
    deep.mkdir(parents=True)
    (deep / "too-deep.md").write_text("not indexed", encoding="utf-8")
    assert not any("secret" in note["relative_path"] or "too-deep" in note["relative_path"] for note in adapter.list_notes())
    assert adapter.search("must not search") == []
    assert not any(note["title"] == "binary" for note in adapter.search("PRIVATE"))
    monkeypatch.setattr(module, "MAX_NOTES", 3)
    assert len(adapter.list_notes(limit=100)) == 3
    monkeypatch.setattr(module, "MAX_ENTRIES", 1)
    assert len(adapter.list_notes(limit=100)) <= 1


def test_changed_owner_and_durable_write_failure_preserve_old_configuration_and_notes(vault, monkeypatch):
    adapter, root, profile, opened = vault
    original = (profile / "obsidian.json").read_bytes()
    real_fsync, allowed = module.os.fsync, [True]
    def revoke(descriptor):
        real_fsync(descriptor)
        allowed[0] = False
    monkeypatch.setattr(module.os, "fsync", revoke)
    result = adapter.create_note("Should not appear", "text", authorization_guard=lambda: allowed[0])
    assert result["status"] == "refused" and adapter.list_notes() == [] and not list(root.rglob("*.tmp"))
    allowed[0] = True
    assert adapter.configure_vault(root, approved=True, authorization_guard=lambda: allowed[0])["status"] == "refused"
    assert (profile / "obsidian.json").read_bytes() == original
    assert not list(profile.glob("*.tmp")) and not opened


def test_generated_name_collision_never_overwrites_an_existing_note(vault, monkeypatch):
    adapter, root, _profile, _opened = vault
    monkeypatch.setattr(module.secrets, "token_hex", lambda _size: "a" * 16)
    first = adapter.create_note("Same", "first")
    assert first["status"] == "created"
    assert adapter.create_note("Same", "second")["status"] == "refused"
    assert (root / first["relative_path"]).read_text(encoding="utf-8") == "first"


@pytest.mark.parametrize("operation", ["fsync", "publish"])
def test_storage_failure_does_not_replace_any_existing_note(vault, monkeypatch, operation):
    adapter, root, profile, _opened = vault
    old = root / "Old.md"
    old.write_text("preserved", encoding="utf-8")
    configuration = (profile / "obsidian.json").read_bytes()
    def fail(*_arguments, **_options):
        raise OSError("PRIVATE device diagnostics")
    monkeypatch.setattr(module.os, "fsync" if operation == "fsync" else "rename" if os.name == "nt" else "link", fail)
    result = adapter.create_note("New", "new content")
    assert result["status"] == "refused" and "PRIVATE" not in result["message"]
    assert old.read_text(encoding="utf-8") == "preserved"
    assert (profile / "obsidian.json").read_bytes() == configuration
    assert not list(root.rglob("*.tmp")) and len(adapter.list_notes()) == 1


def test_changed_vault_configuration_during_prepare_prevents_publication(vault, tmp_path, monkeypatch):
    adapter, root, profile, _opened = vault
    second = tmp_path / "second-vault"
    second.mkdir()
    replacement = dict(json.loads((profile / "obsidian.json").read_text(encoding="utf-8")), vault=str(second))
    fsync = module.os.fsync
    def switch(descriptor):
        fsync(descriptor)
        (profile / "obsidian.json").write_text(json.dumps(replacement), encoding="utf-8")
    monkeypatch.setattr(module.os, "fsync", switch)
    assert adapter.create_note("New", "literal text")["status"] == "refused"
    assert not list(root.rglob("*.md")) and not list(second.iterdir())


def test_concurrent_creates_publish_distinct_complete_notes(vault):
    adapter, root, _profile, _opened = vault
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda index: adapter.create_note("Parallel", str(index) * 1000), range(8)))
    assert all(item["status"] == "created" for item in results)
    assert len({item["id"] for item in results}) == 8
    assert {(root / item["relative_path"]).read_text(encoding="utf-8") for item in results} == {str(i) * 1000 for i in range(8)}


def test_opens_only_owned_percent_encoded_note_or_vault_without_extra_uri_parameters(vault):
    adapter, root, _profile, opened = vault
    note = adapter.create_note("A & B # 100%", "text")
    assert note["status"] == "created"
    assert adapter.open_note(note["id"])["status"] == "requested"
    assert adapter.open_vault()["status"] == "requested"
    assert len(opened) == 2
    for uri, path in zip(opened, [root / note["relative_path"], root], strict=True):
        parsed = urlsplit(uri)
        assert parsed.scheme == "obsidian" and parsed.netloc == "open" and not parsed.fragment
        assert parse_qs(parsed.query) == {"path": [str(path)]}
        assert " " not in uri and "#" not in uri and "+" not in uri


def test_owner_revocation_and_close_prevent_uri_launch_or_new_notes(vault):
    adapter, _root, _profile, opened = vault
    note = adapter.create_note("Title", "text")
    assert adapter.open_note(note["id"], authorization_guard=lambda: False)["status"] == "refused"
    assert adapter.open_vault(authorization_guard=lambda: 1)["status"] == "refused"
    adapter.close()
    assert adapter.create_note("Other", "text")["status"] == "refused"
    assert adapter.open_vault()["status"] == "unavailable" and not opened


def test_uri_failure_does_not_expose_private_paths_or_provider_diagnostics(vault):
    adapter, _root, _profile, _opened = vault
    def fail(_uri):
        raise OSError("PRIVATE path and token")
    adapter.opener = fail
    result = adapter.open_vault()
    assert result["status"] == "unavailable" and "PRIVATE" not in result["message"]


def test_hardlinked_configuration_and_notes_cannot_escape_or_be_replaced(vault, tmp_path):
    adapter, root, profile, _opened = vault
    outside = tmp_path / "outside.md"
    outside.write_text("private outside", encoding="utf-8")
    os.link(outside, root / "Linked.md")
    assert adapter.list_notes() == [] and adapter.search("private") == []
    configuration = profile / "obsidian.json"
    os.link(configuration, tmp_path / "config-alias")
    before = configuration.read_bytes()
    assert adapter.configure_vault(root, approved=True)["status"] == "refused"
    assert adapter.list_notes() == [] and configuration.read_bytes() == before and outside.read_text() == "private outside"


def test_symlinked_vault_inbox_or_notes_are_never_followed(vault, tmp_path):
    adapter, root, _profile, opened = vault
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secret.md").write_text("outside", encoding="utf-8")
    link = tmp_path / "linked-vault"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation unavailable to this Windows user")
    assert adapter.configure_vault(link, approved=True)["status"] == "refused"
    (root / "Inbox").symlink_to(outside, target_is_directory=True)
    (root / "Linked.md").symlink_to(outside / "Secret.md")
    assert adapter.create_note("Escape", "content")["status"] == "refused"
    assert adapter.list_notes() == [] and not opened
    assert list(outside.iterdir()) == [outside / "Secret.md"]
