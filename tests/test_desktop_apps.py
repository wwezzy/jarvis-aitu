"""Only an explicitly approved unchanged GUI executable may be launched."""
import sqlite3
import struct
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from desktop import app_catalog as module
from desktop.app_catalog import AppCatalog


def executable(path, *, body=b"approved application"):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    struct.pack_into("<4sHHIIIHH", data, 128, b"PE\x00\x00", 0x8664, 1, 0, 0, 0, 240, 2)
    struct.pack_into("<H", data, 152, 0x20B)
    data[512:512 + len(body)] = body
    path.write_bytes(data)
    return path


@pytest.fixture
def catalog(tmp_path):
    return AppCatalog(tmp_path / "state", inspector=lambda _: "UsefulApp.exe",
                      executor=lambda *_: pytest.fail("Unexpected OS action"))


def test_empty_catalog_does_not_scan_or_launch_anything(catalog):
    assert catalog.apps() == []
    assert catalog.open_app("Программа")["status"] == "missing"


@pytest.mark.parametrize("approval", [False, None, 1, "true", "I approve", []])
def test_only_exact_explicit_local_approval_can_register(catalog, tmp_path, approval):
    path = executable(tmp_path / "Useful.exe")
    with pytest.raises(PermissionError):
        catalog.register_app(path, "Моя программа", approval=approval)
    assert catalog.apps() == []


def test_explicit_approval_persists_hash_and_launches_fixed_no_argument_path(catalog, tmp_path):
    path = executable(tmp_path / "App with spaces" / "Useful.exe")
    app = catalog.register_app(path, "Моя программа", approval=True)
    assert len(app["sha256"]) == 64 and app["state"] == "registered"
    reopened = AppCatalog(catalog.state_dir, inspector=lambda _: pytest.fail("No new approval necessary"))
    calls = []
    result = reopened.open_app("моя ПРОГРАММА", executor=lambda *args: calls.append(args))
    assert result["status"] == "requested" and "Запускаю" in result["message"]
    assert calls == [(path,)]


def test_changed_binary_requires_reapproval_and_cannot_silently_refresh_hash(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    initial = catalog.register_app(path, "Программа", approval=True)
    executable(path, body=b"updated application")
    assert catalog.open_app("Программа")["status"] == "changed"
    assert catalog.apps()[0]["sha256"] == initial["sha256"]
    with pytest.raises(PermissionError):
        catalog.register_app(path, "Программа")
    approved = catalog.register_app(path, "Программа", approval=True)
    assert approved["id"] == initial["id"] and approved["sha256"] != initial["sha256"]
    assert catalog.open_app("Программа", executor=lambda _: None)["status"] == "requested"


@pytest.mark.parametrize("name", ["cmd.exe", "powershell.exe", "PwSh.exe", "python.exe", "pythonw.exe", "python3.12.exe", "pypy3.exe",
                                  "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe", "node.exe", "java.exe",
                                  "dotnet.exe", "bash.exe", "wsl.exe", "wt.exe", "msiexec.exe", "setup.exe", "unins000.exe"])
def test_known_shells_runtimes_and_installers_cannot_be_registered(catalog, tmp_path, name):
    path = executable(tmp_path / name)
    with pytest.raises(ValueError):
        catalog.register_app(path, "Безопасное имя", approval=True)
    assert catalog.apps() == []


@pytest.mark.parametrize("original", ["CMD.EXE", "python.exe", "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "node.exe"])
def test_renaming_a_known_runtime_does_not_approve_it(catalog, tmp_path, original):
    path = executable(tmp_path / "HarmlessName.exe")
    catalog.inspector = lambda _: original
    with pytest.raises(ValueError):
        catalog.register_app(path, "Программа", approval=True)
    assert catalog.apps() == []


@pytest.mark.parametrize("path", ["relative.exe", "https://example.com/file.exe", "//server/share/file.exe", "\\\\server\\share\\file.exe"])
def test_nonlocal_or_relative_executable_paths_are_rejected(catalog, path):
    with pytest.raises((OSError, ValueError)):
        catalog.register_app(path, "Программа", approval=True)


@pytest.mark.parametrize("suffix", [".lnk", ".cmd", ".bat", ".ps1", ".py", ".url", ".com", ".dll"])
def test_shortcuts_scripts_urls_and_other_file_types_are_rejected(catalog, tmp_path, suffix):
    with pytest.raises(ValueError):
        catalog.register_app(executable(tmp_path / ("Useful" + suffix)), "Программа", approval=True)


@pytest.mark.parametrize("name", ["../file", "a; shutdown", "app.exe --run", "C:\\app.exe", "https://example.com", "line\nbreak", "a" * 81])
def test_app_names_cannot_be_paths_or_arguments(catalog, tmp_path, name):
    with pytest.raises(ValueError):
        catalog.register_app(executable(tmp_path / "Useful.exe"), name, approval=True)
    with pytest.raises(ValueError):
        catalog.open_app(name)


def test_ambiguity_and_partial_names_never_launch(catalog, tmp_path):
    for name in ("First", "Second"):
        catalog.register_app(executable(tmp_path / name / "Useful.exe"), "Редактор", approval=True)
    assert catalog.open_app("Редактор")["status"] == "ambiguous"
    assert len(catalog.resolve("редак")["suggestions"]) == 2
    assert catalog.open_app("редак")["status"] == "missing"


@pytest.mark.parametrize("mutation", ["not_pe", "bad_offset", "dll", "bad_machine", "bad_magic", "oversized_header"])
def test_malformed_or_library_pe_files_are_not_executable_approvals(catalog, tmp_path, mutation):
    path = executable(tmp_path / "Useful.exe")
    data = bytearray(path.read_bytes())
    if mutation == "not_pe":
        data[128:132] = b"fake"
    elif mutation == "bad_offset":
        struct.pack_into("<I", data, 60, 0xFFFFFFFF)
    elif mutation == "dll":
        struct.pack_into("<H", data, 150, 0x2002)
    elif mutation == "bad_machine":
        struct.pack_into("<H", data, 132, 0)
    elif mutation == "bad_magic":
        struct.pack_into("<H", data, 152, 0)
    else:
        struct.pack_into("<H", data, 148, 0xFFFF)
    path.write_bytes(data)
    with pytest.raises(ValueError):
        catalog.register_app(path, "Программа", approval=True)


def test_changed_during_approval_is_rejected_atomically(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    def changed(_):
        executable(path, body=b"replaced during version metadata inspection")
        return "Useful.exe"
    catalog.inspector = changed
    with pytest.raises(ValueError):
        catalog.register_app(path, "Программа", approval=True)
    assert catalog.apps() == []


def test_owner_revoked_during_file_inspection_prevents_permission_mutation(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    allowed = [True]
    def inspector(_):
        allowed[0] = False
        return "Useful.exe"
    catalog.inspector = inspector
    with pytest.raises(PermissionError):
        catalog.register_app(path, "Программа", approval=True, approval_guard=lambda: allowed[0])
    assert catalog.apps() == []


def test_revocation_before_commit_rolls_back_reapproval_without_losing_old_permission(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    initial = catalog.register_app(path, "Программа", approval=True)
    executable(path, body=b"updated application")
    calls = []
    def guard():
        calls.append(True)
        return len(calls) < 3
    with pytest.raises(PermissionError):
        catalog.register_app(path, "Новое имя", approval=True, approval_guard=guard)
    assert catalog.apps()[0]["sha256"] == initial["sha256"]
    assert catalog.apps()[0]["aliases"] == ["Программа"]


def test_failed_and_missing_executables_report_failure_without_retry(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    catalog.register_app(path, "Программа", approval=True)
    calls = []
    def failed(value):
        calls.append(value)
        raise OSError("synthetic failure")
    assert catalog.open_app("Программа", executor=failed)["status"] == "failed"
    assert calls == [path]
    path.unlink()
    assert catalog.open_app("Программа")["status"] == "failed"


def test_remove_only_deletes_catalog_permission_not_executable(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    app = catalog.register_app(path, "Программа", approval=True)
    assert catalog.remove(app["id"]) and not catalog.remove(app["id"])
    assert path.is_file() and catalog.open_app("Программа")["status"] == "missing"


def test_catalog_count_and_alias_limits_are_atomic(catalog, tmp_path, monkeypatch):
    first = executable(tmp_path / "First.exe")
    catalog.register_app(first, "Программа", approval=True)
    for number in range(9):
        catalog.register_app(first, f"Имя {number}", approval=True)
    with pytest.raises(ValueError):
        catalog.register_app(first, "Одиннадцатое имя", approval=True)
    monkeypatch.setattr(module, "MAX_APPS", 1)
    with pytest.raises(ValueError):
        catalog.register_app(executable(tmp_path / "Second.exe"), "Второе", approval=True)
    assert len(catalog.apps()) == 1 and len(catalog.apps()[0]["aliases"]) == 10


def test_concurrent_approvals_are_isolated_by_per_operation_connections(catalog, tmp_path):
    paths = [executable(tmp_path / f"App{number}.exe") for number in range(4)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        records = list(workers.map(lambda path: catalog.register_app(path, path.stem, approval=True), paths))
    assert len({app["id"] for app in records}) == len(catalog.apps()) == 4


def test_native_gui_launch_has_no_extra_arguments_shell_or_project_cwd(catalog, tmp_path, monkeypatch):
    system = tmp_path / "Windows/System32"
    system.mkdir(parents=True)
    path = executable(tmp_path / "Useful.exe")
    calls = []
    monkeypatch.setattr(module, "_system_directory", lambda: system)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kw: calls.append((args, kw)))
    monkeypatch.setenv("SYNTHETIC_API_SECRET", "never inherited")
    module._native_launch(path)
    assert calls[0][0] == ([str(path)],)
    assert calls[0][1]["shell"] is False and calls[0][1]["cwd"] == str(system)
    assert "SYNTHETIC_API_SECRET" not in calls[0][1]["env"]


def test_newer_schema_is_not_overwritten(tmp_path):
    with sqlite3.connect(tmp_path / "app_catalog.db") as database:
        database.execute("PRAGMA user_version=9")
    with pytest.raises(RuntimeError):
        AppCatalog(tmp_path)
    with sqlite3.connect(tmp_path / "app_catalog.db") as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 9


def test_id_selection_resolves_ambiguity_and_metadata_edits_preserve_approval(catalog, tmp_path):
    first = catalog.register_app(executable(tmp_path / "One.exe"), "Editor", approval=True)
    second = catalog.register_app(executable(tmp_path / "Two.exe"), "Editor", approval=True)
    calls = []
    assert catalog.open_app("Editor", executor=calls.append)["status"] == "ambiguous"
    assert catalog.open_app_id(second["id"], executor=calls.append)["status"] == "requested"
    assert calls == [tmp_path / "Two.exe"]
    catalog.rename(second["id"], "Research and Development")
    catalog.set_aliases(second["id"], ["Second app", "Мой редактор"])
    updated = catalog.resolve("Мой редактор")["app"]
    assert updated["id"] == second["id"] and updated["sha256"] == second["sha256"]
    assert catalog.resolve("Editor")["app"]["id"] == first["id"]
    catalog.set_aliases(second["id"], [])
    assert catalog.resolve("Мой редактор")["status"] == "missing"


def test_app_id_is_not_reused_after_deletion_and_restart(catalog, tmp_path):
    old = catalog.register_app(executable(tmp_path / "Old.exe"), "Old", approval=True)
    catalog.remove(old["id"])
    reopened = AppCatalog(catalog.state_dir, inspector=lambda _: "Useful.exe")
    new = reopened.register_app(executable(tmp_path / "New.exe"), "New", approval=True)
    assert new["id"] > old["id"]
    assert reopened.open_app_id(old["id"])["status"] == "missing"


def test_reapprove_keeps_id_name_aliases_and_refreshes_changed_executable_pin(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    old = catalog.register_app(path, "Original", approval=True)
    catalog.rename(old["id"], "My editor")
    catalog.set_aliases(old["id"], ["Edit stuff"])
    executable(path, body=b"updated application")
    assert catalog.open_app_id(old["id"])["status"] == "changed"
    new = catalog.reapprove(old["id"], approval=True, approval_guard=lambda: True)
    assert new["id"] == old["id"] and new["sha256"] != old["sha256"]
    assert new["name"] == "My editor" and new["aliases"] == ["Edit stuff"]


def test_reapprove_cannot_resurrect_app_deleted_during_inspection(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    old = catalog.register_app(path, "Original", approval=True)
    def removed(_):
        catalog.remove(old["id"])
        catalog.register_app(executable(tmp_path / "Replacement.exe"), "Replacement", approval=True)
        return "Useful.exe"
    # Avoid recursive inspection in the independently approved replacement.
    def inspect(path):
        if path.name == "Useful.exe":
            return removed(path)
        return "Replacement.exe"
    catalog.inspector = inspect
    with pytest.raises(ValueError):
        catalog.reapprove(old["id"], approval=True, approval_guard=lambda: True)
    assert [app["name"] for app in catalog.apps()] == ["Replacement"]
    assert catalog.apps()[0]["id"] > old["id"]


def test_reapprove_revoked_during_inspection_keeps_original_hash_and_aliases(catalog, tmp_path):
    path = executable(tmp_path / "Useful.exe")
    old = catalog.register_app(path, "Original", approval=True)
    executable(path, body=b"updated application")
    allowed = [True]
    def revoked(_):
        allowed[0] = False
        return "Useful.exe"
    catalog.inspector = revoked
    with pytest.raises(PermissionError):
        catalog.reapprove(old["id"], approval=True, approval_guard=lambda: allowed[0])
    current = catalog.apps()[0]
    assert current["sha256"] == old["sha256"] and current["aliases"] == old["aliases"]


def test_app_owner_guard_is_rechecked_after_hashing(catalog, tmp_path, monkeypatch):
    path = executable(tmp_path / "Useful.exe")
    old = catalog.register_app(path, "Original", approval=True)
    original, allowed, calls = module._fingerprint, [True], []
    def revoked(path):
        value = original(path)
        allowed[0] = False
        return value
    monkeypatch.setattr(module, "_fingerprint", revoked)
    assert catalog.open_app_id(old["id"], executor=calls.append, authorization_guard=lambda: allowed[0])["status"] == "denied"
    assert calls == []


@pytest.mark.parametrize("original", ["python3.13t.exe", "python3.12_d.exe", "pythonw3.14t_d.exe", "pypy3.exe"])
def test_versioned_free_threaded_and_debug_python_binaries_stay_blocked(catalog, tmp_path, original):
    path = executable(tmp_path / "Renamed.exe")
    catalog.inspector = lambda _: original
    with pytest.raises(ValueError):
        catalog.register_app(path, "Useful app", approval=True)


def test_version_resource_dll_load_is_restricted_to_windows_system32(tmp_path, monkeypatch):
    calls = []
    class Function:
        def __call__(self, *_args):
            return 0
    library = SimpleNamespace(GetFileVersionInfoSizeW=Function(), GetFileVersionInfoW=Function(), VerQueryValueW=Function())
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return library
    monkeypatch.setattr(module.ctypes, "WinDLL", load, raising=False)
    assert module._original_filename(tmp_path / "Useful.exe") is None
    assert calls == [(("version.dll",), {"use_last_error": True, "winmode": 0x800})]


@pytest.mark.parametrize("operation,calls_before_revoke", [("rename", 1), ("set_aliases", 2)])
def test_metadata_permission_expiry_before_commit_rolls_back_every_field(catalog, tmp_path, operation, calls_before_revoke):
    old = catalog.register_app(executable(tmp_path / "Useful.exe"), "Original", approval=True)
    calls = []
    def guard():
        calls.append(True)
        return len(calls) <= calls_before_revoke
    with pytest.raises(PermissionError):
        if operation == "rename":
            catalog.rename(old["id"], "Updated", approval_guard=guard)
        else:
            catalog.set_aliases(old["id"], ["Updated"], approval_guard=guard)
    assert catalog.apps()[0] == old


def test_removing_app_permission_during_hashing_cancels_launch(catalog, tmp_path, monkeypatch):
    path = executable(tmp_path / "Useful.exe")
    old = catalog.register_app(path, "Original", approval=True)
    original, calls = module._fingerprint, []
    def removed(path):
        value = original(path)
        catalog.remove(old["id"])
        return value
    monkeypatch.setattr(module, "_fingerprint", removed)
    assert catalog.open_app_id(old["id"], executor=calls.append, authorization_guard=lambda: True)["status"] == "denied"
    assert calls == []


def test_schema_one_app_rows_upgrade_without_id_reuse_or_lost_approval(tmp_path):
    path = executable(tmp_path / "Useful.exe")
    digest = module._fingerprint(path)
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "app_catalog.db") as database:
        database.executescript("""
            CREATE TABLE approved_apps(id INTEGER PRIMARY KEY,path TEXT UNIQUE,name TEXT,sha256 TEXT,size INTEGER,approved_at REAL);
            CREATE TABLE approved_app_aliases(app_id INTEGER REFERENCES approved_apps(id) ON DELETE CASCADE,alias TEXT,normalized TEXT,PRIMARY KEY(app_id,normalized));
            PRAGMA user_version=1;
        """)
        database.execute("INSERT INTO approved_apps VALUES(73,?,?,?,?,?)", (str(path), "Saved", digest[0], digest[1], 100.0))
        database.execute("INSERT INTO approved_app_aliases VALUES(73,'Original alias','original alias')")
    store = AppCatalog(state, inspector=lambda _: "Useful.exe")
    old = store.resolve("Original alias")["app"]
    assert old["id"] == 73 and old["approved_at"] == 100.0 and old["sha256"] == digest[0]
    store.remove(73)
    new = AppCatalog(state, inspector=lambda _: "Useful.exe").register_app(path, "New", approval=True)
    assert new["id"] == 74 and store.open_app_id(73)["status"] == "missing"
