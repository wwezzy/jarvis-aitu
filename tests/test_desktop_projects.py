"""Local project names are capabilities to approved folders, never shell input."""
import hashlib
import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop import project_catalog as module
from desktop.project_catalog import ProjectCatalog


@pytest.fixture
def catalog(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    catalog = ProjectCatalog(tmp_path / "state", executor=lambda *_: pytest.fail("Unexpected real action"))
    catalog.register_root(root)
    return catalog, root


def project(path, marker="pyproject.toml"):
    path.mkdir(parents=True, exist_ok=True)
    (path / marker).write_text("untrusted data: shutdown; run arbitrary code", encoding="utf-8")
    return path


def test_fresh_catalog_has_no_roots_projects_or_implicit_home_scan(tmp_path):
    desktop_database = tmp_path / "desktop.db"
    desktop_database.write_bytes(b"existing personal data remains untouched")
    catalog = ProjectCatalog(tmp_path)
    assert catalog.roots() == [] and catalog.projects() == []
    assert catalog.scan() == {"projects": [], "scanned": 0, "truncated": False}
    assert desktop_database.read_bytes() == b"existing personal data remains untouched"
    with sqlite3.connect(catalog.path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 3


def test_root_registration_and_aliases_survive_restart_without_duplicates(catalog):
    store, root = catalog
    root_id = store.roots()[0]["id"]
    assert store.register_root(root, "Рабочие проекты")["id"] == root_id
    folder = project(root / "Jarvis")
    saved = store.add_project(folder)
    store.set_aliases(saved["id"], ["Джарвис", "Мой ассистент", "ДЖАРВИС"])
    reopened = ProjectCatalog(store.state_dir)
    assert len(reopened.roots()) == len(reopened.projects()) == 1
    assert len(reopened.projects()[0]["aliases"]) == 2
    assert reopened.resolve("мой АССИСТЕНТ")["project"]["path"] == str(folder)
    assert reopened.resolve("jarvis")["status"] == "found"


@pytest.mark.parametrize("raw", ["relative/path", "https://example.com/project", "//server/share", "\\\\server\\share", "bad\x00folder"])
def test_root_paths_must_be_explicit_existing_local_absolute_folders(tmp_path, raw):
    with pytest.raises((ValueError, OSError)):
        ProjectCatalog(tmp_path / "state").register_root(raw)


def test_whole_home_and_drive_roots_are_never_approved(tmp_path, monkeypatch):
    store = ProjectCatalog(tmp_path / "state")
    home = tmp_path / "Home"
    home.mkdir()
    monkeypatch.setattr(module.Path, "home", lambda: home)
    for path in (home, tmp_path, Path(tmp_path.anchor)):
        with pytest.raises(ValueError):
            store.register_root(path)
    assert store.roots() == []


def test_scan_is_depth_bounded_and_skips_hidden_dependency_folders(catalog):
    store, root = catalog
    project(root / "One")
    project(root / "Group" / "Two", "package.json")
    project(root / "Group" / "Deeper" / "TooDeep")
    for name in (".hidden", ".git", ".venv", "node_modules", "venv", "__pycache__"):
        project(root / name / "Ignored")
    (root / "notes.txt").write_text("not read as a command", encoding="utf-8")
    result = store.scan()
    assert {item["name"] for item in result["projects"]} == {"Projects", "One", "Two"}
    assert not result["truncated"]


def test_windows_hidden_attribute_is_excluded_without_reading_content():
    hidden = SimpleNamespace(name="ordinary name", stat=lambda **_: SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_HIDDEN))
    assert module._hidden(hidden)


def test_scan_includes_approved_root_itself_and_deduplicates_overlaps(catalog):
    store, root = catalog
    project(root)
    child = project(root / "Nested")
    store.register_root(child)
    assert {item["path"] for item in store.scan()["projects"]} == {str(root), str(child)}


@pytest.mark.parametrize("options", [{"max_depth": -1}, {"max_depth": 3}, {"max_depth": True}, {"limit": 0}, {"limit": 201}, {"limit": False}])
def test_scan_limits_cannot_be_expanded_by_callers(catalog, options):
    with pytest.raises(ValueError):
        catalog[0].scan(**options)


def test_scan_caps_project_results_and_total_directory_entries(catalog, monkeypatch):
    store, root = catalog
    for number in range(8):
        project(root / f"Project {number}")
    assert len(store.scan(limit=3)["projects"]) == 3
    assert store.scan(limit=3)["truncated"]
    monkeypatch.setattr(module, "MAX_ENTRIES", 2)
    assert store.scan()["truncated"]


def test_existing_catalog_capacity_stops_scan_truthfully(catalog, monkeypatch):
    store, root = catalog
    store.add_project(project(root / "Zulu"))
    project(root / "Alpha")
    monkeypatch.setattr(module, "MAX_PROJECTS", 1)
    result = store.scan(limit=1)
    assert result["projects"] == [] and result["truncated"]
    assert len(store.projects()) == 1


def test_folder_without_project_marker_can_be_explicitly_registered(catalog):
    store, root = catalog
    plain = root / "Design notes"
    plain.mkdir()
    assert store.scan()["projects"] == []
    store.add_project(plain, alias="Мой дизайн")
    assert store.resolve("мой дизайн")["status"] == "found"


def test_missing_and_ambiguous_names_only_suggest_never_guess(catalog):
    store, root = catalog
    first = store.add_project(project(root / "Alpha" / "Shared"))
    second = store.add_project(project(root / "Beta" / "Shared"))
    store.set_aliases(first["id"], ["Первый"])
    store.set_aliases(second["id"], ["Второй"])
    calls = []
    result = store.open_project("shared", executor=lambda *args: calls.append(args))
    assert result["status"] == "ambiguous" and len(result["suggestions"]) == 2
    assert store.open_project("Sha", executor=lambda *args: calls.append(args))["status"] == "missing"
    assert store.resolve("неизвестный")["suggestions"] == []
    assert calls == []


def test_unique_approved_alias_dispatches_only_folder_and_finite_editor(catalog):
    store, root = catalog
    folder = project(root / "Folder with spaces")
    saved = store.add_project(folder)
    store.set_aliases(saved["id"], ["Серьёзный проект"])
    calls = []
    result = store.open_project("СЕРЬЕЗНЫЙ ПРОЕКТ", "vscode", executor=lambda *args: calls.append(args))
    assert result["status"] == "requested" and "Открываю" in result["message"]
    assert calls == [(folder, "vscode")]
    assert "открыл" not in result["message"].lower()
    with pytest.raises(ValueError):
        store.open_project("Серьёзный проект", "powershell", executor=lambda *args: calls.append(args))
    assert len(calls) == 1


@pytest.mark.parametrize("alias", ["../outside", "C:\\secret", "https://example.com", "run;shutdown", "x|cmd", "x&exit", "x$(cmd)", "\"quoted\"", "a\ncommand", ".", "..", "-arg", "x" * 81, ""])
def test_paths_commands_and_control_characters_are_not_project_names(catalog, alias):
    store, root = catalog
    saved = store.add_project(project(root / "Valid"))
    with pytest.raises(ValueError):
        store.set_aliases(saved["id"], [alias])
    with pytest.raises(ValueError):
        store.open_project(alias)
    assert store.projects()[0]["aliases"] == []


def test_outside_unapproved_or_deleted_folders_cannot_open(catalog, tmp_path):
    store, root = catalog
    outside = project(tmp_path / "Outside")
    with pytest.raises(ValueError):
        store.add_project(outside)
    saved = store.add_project(project(root / "Valid"))
    with sqlite3.connect(store.path) as database:
        database.execute("UPDATE projects SET path=? WHERE id=?", (str(outside), saved["id"]))
    assert store.projects()[0]["available"] is False
    assert store.open_project("Valid")["status"] == "missing"


def test_symlink_escape_is_neither_discovered_nor_explicitly_registered(catalog, tmp_path):
    store, root = catalog
    outside = project(tmp_path / "Outside")
    link = root / "Escaped"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable to this user")
    assert store.scan()["projects"] == []
    with pytest.raises(ValueError):
        store.add_project(link)


def test_delete_and_root_removal_only_remove_catalog_rows(catalog):
    store, root = catalog
    folder = project(root / "Important")
    saved = store.add_project(folder, alias="Важное")
    assert store.remove(saved["id"]) and not store.remove(saved["id"])
    assert (folder / "pyproject.toml").is_file()
    store.add_project(folder)
    assert store.remove_root(store.roots()[0]["id"])
    assert store.projects() == [] and (folder / "pyproject.toml").is_file()


def test_executor_failure_is_truthful_and_never_retried(catalog):
    store, root = catalog
    store.add_project(project(root / "Valid"))
    calls = []
    def failure(*args):
        calls.append(args)
        raise OSError("synthetic failure")
    result = store.open_project("Valid", executor=failure)
    assert result["status"] == "failed" and len(calls) == 1
    assert "Не удалось" in result["message"]


def test_concurrent_registration_uses_separate_sqlite_connections(catalog):
    store, root = catalog
    folders = [project(root / f"Project {number}") for number in range(8)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        records = list(workers.map(store.add_project, folders))
    assert len({record["id"] for record in records}) == len(store.projects()) == 8


def test_native_launch_uses_fixed_executable_argv_and_system_working_folder(tmp_path, monkeypatch):
    system = tmp_path / "Windows" / "System32"
    system.mkdir(parents=True)
    explorer = system.parent / "explorer.exe"
    explorer.write_bytes(b"fixture, never executed")
    code = tmp_path / "Code.exe"
    code.write_bytes(b"fixture, never executed")
    folder = tmp_path / "Approved project"
    folder.mkdir()
    calls = []
    monkeypatch.setattr(module, "_system_directory", lambda: system)
    monkeypatch.setattr(module, "_verified_vscode", lambda _: code)
    monkeypatch.setattr(module, "_verified_jetbrains", lambda _system, editor: code if editor in {"pycharm", "idea"} else None)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kw: calls.append((args, kw)))
    monkeypatch.setenv("SYNTHETIC_PROVIDER_SECRET", "never inherited")
    module._native_open(folder, "explorer")
    module._native_open(folder, "vscode")
    module._native_open(folder, "pycharm")
    module._native_open(folder, "idea")
    assert calls[0][0] == ([str(explorer), str(folder)],)
    assert calls[1][0] == ([str(code), "--new-window", str(folder)],)
    assert calls[2][0] == calls[3][0] == ([str(code), str(folder)],)
    for _args, options in calls:
        assert options["shell"] is False and options["cwd"] == str(system)
        assert "SYNTHETIC_PROVIDER_SECRET" not in options["env"]


@pytest.fixture
def code(tmp_path, monkeypatch):
    local = tmp_path / "LocalAppData"
    executable = local / "Programs/Microsoft VS Code/Code.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic signed binary response, never executed")
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return executable


def code_response(path, **changes):
    data = {"protocol": 1, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    data.update(changes)
    return SimpleNamespace(returncode=0, stdout=json.dumps(data).encode())


def test_vscode_requires_fixed_official_location_microsoft_signature_and_hash(code, tmp_path):
    def probe(arguments, **options):
        assert "-EncodedCommand" in arguments and options["shell"] is False and options["timeout"] == 8
        assert "Microsoft Corporation" in module._CODE_PROBE and "'Valid'" in module._CODE_PROBE
        return code_response(code)
    assert module._verified_vscode(tmp_path, runner=probe) == code


@pytest.mark.parametrize("changes", [{"protocol": True}, {"protocol": 2}, {"path": "//server/Code.exe"},
                                    {"path": "https://example.com/Code.exe"}, {"path": "Code.exe"},
                                    {"sha256": "0" * 64}, {"extra": "--execute command"}])
def test_invalid_vscode_verification_fails_closed(code, tmp_path, changes):
    assert module._verified_vscode(tmp_path, runner=lambda *_args, **_kw: code_response(code, **changes)) is None


def test_changed_or_unofficial_editor_binary_is_rejected(code, tmp_path):
    response = code_response(code)
    code.write_bytes(b"changed after verification")
    assert module._verified_vscode(tmp_path, runner=lambda *_a, **_kw: response) is None
    other = tmp_path / "unapproved/Code.exe"
    other.parent.mkdir()
    other.write_bytes(code.read_bytes())
    assert module._verified_vscode(tmp_path, runner=lambda *_a, **_kw: code_response(other)) is None


def test_newer_catalog_schema_is_not_rewritten(tmp_path):
    with sqlite3.connect(tmp_path / "catalog.db") as database:
        database.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError):
        ProjectCatalog(tmp_path)
    with sqlite3.connect(tmp_path / "catalog.db") as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 99


@pytest.mark.parametrize("editor,filename,product", [("pycharm", "pycharm64.exe", "PyCharm"), ("idea", "idea64.exe", "IntelliJ IDEA Ultimate")])
def test_jetbrains_only_discovers_finite_editor_launchers_in_install_trees(tmp_path, monkeypatch, editor, filename, product):
    local = tmp_path / "LocalAppData"
    approved = local / "Programs" / product / "bin" / filename
    approved.parent.mkdir(parents=True)
    approved.write_bytes(b"fixture never executed")
    unrelated = local / "Downloads" / "bin" / filename
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(approved.read_bytes())
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert module._jetbrains_candidates(editor) == {approved}
    def probe(arguments, **options):
        import base64
        source = base64.b64decode(arguments[-1]).decode("utf-16-le")
        assert "JetBrains s.r.o." in source and "Get-AuthenticodeSignature -LiteralPath" in source
        assert options["shell"] is False and options["timeout"] == 8
        return code_response(approved)
    assert module._verified_jetbrains(tmp_path, editor, runner=probe) == approved
    assert module._verified_jetbrains(tmp_path, editor, runner=lambda *_a, **_kw: code_response(unrelated)) is None
    assert module._jetbrains_candidates("arbitrary editor") == set()


def test_python_and_java_editors_are_explicit_not_inferred_from_project(catalog):
    store, root = catalog
    saved = store.add_project(project(root / "Java named Python"))
    calls = []
    for editor in ("pycharm", "idea"):
        assert store.open_project(saved["name"], editor, executor=lambda *args: calls.append(args))["status"] == "requested"
    assert [call[1] for call in calls] == ["pycharm", "idea"]


def test_project_editor_choice_is_explicit_persistent_and_overrideable(catalog):
    store, root = catalog
    saved = store.add_project(project(root / "Project"))
    assert saved["editor"] == "explorer"
    store.set_editor(saved["id"], "pycharm")
    reopened = ProjectCatalog(store.state_dir)
    calls = []
    assert reopened.open_project("Project", executor=lambda *args: calls.append(args))["status"] == "requested"
    assert calls[0][1] == "pycharm"
    assert reopened.open_project("Project", "idea", executor=lambda *args: calls.append(args))["status"] == "requested"
    assert calls[1][1] == "idea" and reopened.projects()[0]["editor"] == "pycharm"
    with pytest.raises(ValueError):
        store.set_editor(saved["id"], "arbitrary program")


def test_catalog_schema_one_upgrades_without_losing_roots_projects_or_aliases(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    folder = project(root / "Saved")
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "catalog.db") as database:
        database.executescript("""
            CREATE TABLE project_roots(id INTEGER PRIMARY KEY,path TEXT UNIQUE NOT NULL,label TEXT NOT NULL);
            CREATE TABLE projects(id INTEGER PRIMARY KEY,root_id INTEGER NOT NULL REFERENCES project_roots(id),path TEXT NOT NULL UNIQUE,name TEXT NOT NULL);
            CREATE TABLE project_aliases(project_id INTEGER NOT NULL REFERENCES projects(id),alias TEXT NOT NULL,normalized TEXT NOT NULL,PRIMARY KEY(project_id,normalized));
            PRAGMA user_version=1;
        """)
        database.execute("INSERT INTO project_roots VALUES(1,?,?)", (str(root), "Saved root"))
        database.execute("INSERT INTO projects VALUES(1,1,?,?)", (str(folder), "Saved"))
        database.execute("INSERT INTO project_aliases VALUES(1,?,?)", ("Ассистент", "ассистент"))
    store = ProjectCatalog(state)
    assert store.resolve("Ассистент")["project"]["editor"] == "explorer"
    store.set_editor(1, "idea")
    assert ProjectCatalog(state).resolve("Ассистент")["project"]["editor"] == "idea"
    assert (folder / "pyproject.toml").is_file()


@pytest.mark.parametrize("marker", [".idea", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle.kts"])
def test_common_pycharm_and_intellij_projects_are_discovered(catalog, marker):
    store, root = catalog
    folder = root / "Design and Analysis"
    folder.mkdir()
    if marker == ".idea":
        (folder / marker).mkdir()
    else:
        (folder / marker).write_text("untrusted project metadata", encoding="utf-8")
    assert store.scan()["projects"][0]["path"] == str(folder)


def test_id_selection_opens_exact_ambiguous_project_and_rename_preserves_aliases(catalog):
    store, root = catalog
    first = store.add_project(project(root / "One" / "Shared"))
    second = store.add_project(project(root / "Two" / "Shared"))
    store.set_aliases(second["id"], ["Second project"])
    store.set_editor(second["id"], "idea")
    calls = []
    assert store.open_project("Shared", executor=lambda *args: calls.append(args))["status"] == "ambiguous"
    assert store.open_project_id(second["id"], executor=lambda *args: calls.append(args))["status"] == "requested"
    assert calls == [(Path(second["path"]), "idea")]
    renamed = store.rename(second["id"], "Research and Development")
    assert renamed["id"] == second["id"] and renamed["aliases"] == ["Second project"]
    assert renamed["editor"] == "idea" and store.resolve("Research and Development")["project"]["id"] == second["id"]
    assert store.resolve("Shared")["project"]["id"] == first["id"]


def test_removed_project_and_root_ids_never_target_new_records_even_after_restart(catalog):
    store, root = catalog
    old_root = store.roots()[0]
    old_project = store.add_project(project(root / "Old"))
    store.remove_root(old_root["id"])
    reopened = ProjectCatalog(store.state_dir)
    new_root = reopened.register_root(root)
    new_project = reopened.add_project(project(root / "New"))
    assert new_root["id"] > old_root["id"] and new_project["id"] > old_project["id"]
    assert reopened.open_project_id(old_project["id"])["status"] == "missing"
    with pytest.raises(ValueError):
        reopened.scan(old_root["id"])


@pytest.mark.parametrize("bad", [True, 0, -1, "1", None])
def test_project_id_and_rename_only_accept_existing_integer_records(catalog, bad):
    with pytest.raises(ValueError):
        catalog[0].open_project_id(bad)
    with pytest.raises(ValueError):
        catalog[0].rename(bad, "Project")


def test_project_owner_guard_is_rechecked_after_resolution(catalog, monkeypatch):
    store, root = catalog
    saved = store.add_project(project(root / "Saved"))
    allowed, calls = [True], []
    original = store.resolve
    def revoked(name):
        result = original(name)
        allowed[0] = False
        return result
    monkeypatch.setattr(store, "resolve", revoked)
    result = store.open_project(saved["name"], executor=lambda *args: calls.append(args), authorization_guard=lambda: allowed[0])
    assert result["status"] == "denied" and calls == []


def test_project_native_guard_is_rechecked_after_expensive_editor_verification(tmp_path, monkeypatch):
    system = tmp_path / "Windows/System32"
    system.mkdir(parents=True)
    app = tmp_path / "idea64.exe"
    app.write_bytes(b"fixture never executed")
    allowed, calls = [True], []
    monkeypatch.setattr(module, "_system_directory", lambda: system)
    def verify(*_a):
        allowed[0] = False
        return app
    monkeypatch.setattr(module, "_verified_jetbrains", verify)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(PermissionError):
        module._native_open(tmp_path, "idea", authorization_guard=lambda: allowed[0])
    assert calls == []


def test_linked_root_is_rejected_before_registration_without_symlink_privileges(catalog, tmp_path, monkeypatch):
    store, root = catalog
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *a, **kw: tmp_path / "Outside" if path == root else original(path, *a, **kw))
    with pytest.raises(ValueError):
        store.register_root(root)
    assert store.open_project_id(999)["status"] == "missing"


def test_removal_during_editor_verification_cancels_native_project_launch(catalog, tmp_path, monkeypatch):
    store, root = catalog
    store.executor = None  # Native bridge below is fully replaced with fakes.
    saved = store.add_project(project(root / "Saved"))
    store.set_editor(saved["id"], "idea")
    system = tmp_path / "Windows/System32"
    system.mkdir(parents=True)
    app = tmp_path / "idea64.exe"
    app.write_bytes(b"fixture never executed")
    calls = []
    monkeypatch.setattr(module, "_system_directory", lambda: system)
    def removed(*_a):
        store.remove(saved["id"])
        return app
    monkeypatch.setattr(module, "_verified_jetbrains", removed)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    assert store.open_project_id(saved["id"], authorization_guard=lambda: True)["status"] == "denied"
    assert calls == []


def test_jetbrains_discovery_avoids_recursing_runtime_and_plugin_trees(tmp_path, monkeypatch):
    programs = tmp_path / "ProgramFiles"
    install = programs / "JetBrains/PyCharm 2026.2"
    binary = install / "bin/pycharm64.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fixture never executed")
    runtime = install / "jbr"
    runtime.mkdir()
    plugins = install / "plugins"
    plugins.mkdir()
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ProgramFiles", str(programs))
    scandir = module.os.scandir
    def inspect(folder):
        assert Path(folder) not in {runtime, plugins, binary.parent}
        return scandir(folder)
    monkeypatch.setattr(module.os, "scandir", inspect)
    assert module._jetbrains_candidates("pycharm") == {binary}


@pytest.mark.parametrize("editor,product,filename", [
    ("pycharm", "PyCharm 2026.2.1", "pycharm64.exe"),
    ("pycharm", "PyCharm Community Edition 2024.3.2", "pycharm64.exe"),
    ("pycharm", "PyCharm 2026.3 EAP", "pycharm64.exe"),
    ("idea", "IntelliJ IDEA 2026.2.2", "idea64.exe"),
    ("idea", "IntelliJ IDEA Ultimate 2026.3 RC2", "idea64.exe"),
])
def test_versioned_per_user_jetbrains_installations_are_verified_candidates(tmp_path, monkeypatch, editor, product, filename):
    local = tmp_path / "LocalAppData"
    binary = local / "Programs" / product / "bin" / filename
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fixture never executed")
    unrelated = local / "Programs/Unrelated Application"
    unrelated.mkdir()
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    scan = module.os.scandir
    def inspect(folder):
        assert Path(folder) != unrelated
        return scan(folder)
    monkeypatch.setattr(module.os, "scandir", inspect)
    assert module._jetbrains_candidates(editor) == {binary}
    assert module._verified_jetbrains(tmp_path, editor, runner=lambda *_a, **_kw: code_response(binary)) == binary
    assert module._verified_jetbrains(tmp_path, editor, runner=lambda *_a, **_kw: code_response(binary, sha256="0" * 64)) is None


@pytest.mark.parametrize("name", ["PyCharm unrelated", "PyCharm Plugin", "PyCharm;command", "PyCharm 2026.2.1 copied", "Some PyCharm 2026.2"])
def test_versioned_product_discovery_does_not_accept_arbitrary_prefixes(name):
    assert not module._jetbrains_product_name(name, "pycharm")


def test_unrelated_program_subtrees_do_not_spend_jetbrains_discovery_budget(tmp_path, monkeypatch):
    local = tmp_path / "LocalAppData"
    programs = local / "Programs"
    unrelated = programs / "Unrelated Application"
    unrelated.mkdir(parents=True)
    binary = programs / "PyCharm 2026.2.1/bin/pycharm64.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fixture never executed")
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    original = module.os.scandir
    def only_matching_subtrees(folder):
        assert Path(folder) != unrelated  # Its potentially huge tree stays untouched.
        return original(folder)
    monkeypatch.setattr(module.os, "scandir", only_matching_subtrees)
    assert module._jetbrains_candidates("pycharm") == {binary}


def test_local_programs_enumeration_reads_at_most_two_hundred_names(tmp_path, monkeypatch):
    local = tmp_path / "LocalAppData"
    programs = local / "Programs"
    programs.mkdir(parents=True)
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    count = []
    class Entries:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __iter__(self):
            for number in range(1000):
                count.append(number)
                yield SimpleNamespace(name=f"Unrelated{number}")
    original = module.os.scandir
    monkeypatch.setattr(module.os, "scandir", lambda folder: Entries() if Path(folder) == programs else original(folder))
    assert module._jetbrains_candidates("pycharm") == set()
    assert len(count) == 200
