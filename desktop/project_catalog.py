"""Owner-approved local project folders; never interprets code or chat paths.

Register roots/add aliases from trusted local settings only. A command router
may pass an original user's project *name* to open_project(), never a model's
output. Discovery is explicit and bounded, reads names/markers only, and does
not run project scripts. Removing records never removes a folder or file.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import unicodedata
from contextlib import closing, contextmanager

from desktop.native_runtime import powershell_environment


MAX_PROJECTS = 200
MAX_ROOTS = 20
MAX_ENTRIES = 4000
EDITORS = frozenset({"explorer", "vscode", "pycharm", "idea"})
_EXCLUDED = {"node_modules", "venv", "env", "__pycache__"}
_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "CMakeLists.txt", "requirements.txt")


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise ValueError("Project name must contain 1–80 characters")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("Project names cannot contain control characters")
    value = unicodedata.normalize("NFC", value).strip()
    if (not value or value.startswith((".", "-")) or value.endswith(".") or re.search(r"(?:^|\s)-", value)
            or any(not (unicodedata.category(character)[0] in {"L", "M", "N"} or character in " ._-+") for character in value)):
        raise ValueError("Use a project name, not a path, URL or command")
    return re.sub(r"\s+", " ", value)


def _key(value):
    return _name(value).casefold().replace("ё", "е")


def _folder(value):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or len(raw) > 2048 or "\x00" in raw
            or "://" in raw or raw.startswith(("\\\\", "//"))):
        raise ValueError("Choose an existing local folder")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Choose an absolute local folder")
    resolved = path.resolve(strict=True)
    if (not resolved.is_dir() or path != resolved or path.is_symlink()
            or getattr(path, "is_junction", lambda: False)()):
        raise ValueError("Linked or missing folders are not approved project locations")
    return resolved


def _inside(path, root):
    return path == root or root in path.parents


def _identifier(value):
    if type(value) is not int or value < 1:
        raise ValueError("Invalid catalog record")
    return value


def _hidden(entry):
    return entry.name.startswith(".") or bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)


class _CatalogFull(ValueError):
    pass


class ProjectCatalog:
    """Separate local SQLite catalog, thread-safe through per-call connections.

    ``resolve`` returns status found/ambiguous/missing, project (or None), and
    suggestions (project dictionaries). Exact case-insensitive names/aliases
    alone can open a folder; suggestions never select a project automatically.
    ``executor(folder: Path, editor: str)`` is an optional test/native adapter.
    """

    def __init__(self, state_dir: Path, *, executor=None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / "catalog.db"
        self.executor = executor
        with self._connection() as database:
            version = database.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("Project catalog is newer than this application")
            database.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS project_roots (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, label TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES project_roots(id) ON DELETE CASCADE,
                    path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    editor TEXT NOT NULL DEFAULT 'explorer' CHECK(editor IN ('explorer','vscode','pycharm','idea'))
                );
                CREATE TABLE IF NOT EXISTS project_aliases (
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL, normalized TEXT NOT NULL,
                    PRIMARY KEY(project_id, normalized)
                );
                CREATE INDEX IF NOT EXISTS project_alias_lookup ON project_aliases(normalized);
            """)
            columns = {row[1] for row in database.execute("PRAGMA table_info(projects)")}
            if "editor" not in columns:
                database.execute("ALTER TABLE projects ADD COLUMN editor TEXT NOT NULL DEFAULT 'explorer' CHECK(editor IN ('explorer','vscode','pycharm','idea'))")
            database.execute("PRAGMA user_version=2")

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as database:
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys=ON")
            with database:
                yield database

    def roots(self):
        with self._connection() as database:
            return [dict(row) for row in database.execute("SELECT * FROM project_roots ORDER BY id")]

    def register_root(self, path, label=None):
        folder = _folder(path)
        home = Path.home().resolve()
        if folder == Path(folder.anchor) or _inside(home, folder):
            raise ValueError("Choose a project folder, not the whole home or drive")
        label = _name(label if label is not None else folder.name)
        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT id FROM project_roots WHERE path=?", (str(folder),)).fetchone()
            if row is None:
                if database.execute("SELECT count(*) FROM project_roots").fetchone()[0] >= MAX_ROOTS:
                    raise ValueError("At most 20 project roots can be approved")
                cursor = database.execute("INSERT INTO project_roots(path,label) VALUES(?,?)", (str(folder), label))
                root_id = cursor.lastrowid
            else:
                root_id = row["id"]
                database.execute("UPDATE project_roots SET label=? WHERE id=?", (label, root_id))
            return dict(database.execute("SELECT * FROM project_roots WHERE id=?", (root_id,)).fetchone())

    def add_project(self, path, alias=None, root_id=None):
        folder = _folder(path)
        title = _name(alias if alias is not None else folder.name)
        roots = self.roots()
        if root_id is not None:
            root_id = _identifier(root_id)
            roots = [root for root in roots if root["id"] == root_id]
        approved = []
        for root in roots:
            try:
                location = _folder(root["path"])
                if _inside(folder, location):
                    approved.append(root)
            except (OSError, ValueError):
                continue
        if not approved:
            raise ValueError("The project must be inside an approved root")
        root = max(approved, key=lambda item: len(Path(item["path"]).parts))
        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT id FROM projects WHERE path=?", (str(folder),)).fetchone()
            if row is None:
                if database.execute("SELECT count(*) FROM projects").fetchone()[0] >= MAX_PROJECTS:
                    raise _CatalogFull("The catalog contains 200 projects; remove an old record first")
                cursor = database.execute("INSERT INTO projects(root_id,path,name) VALUES(?,?,?)", (root["id"], str(folder), title))
                project_id = cursor.lastrowid
            else:
                project_id = row["id"]
            if alias is not None:
                count = database.execute("SELECT count(*) FROM project_aliases WHERE project_id=?", (project_id,)).fetchone()[0]
                if count >= 10 and not database.execute("SELECT 1 FROM project_aliases WHERE project_id=? AND normalized=?", (project_id, _key(alias))).fetchone():
                    raise ValueError("At most 10 aliases per project")
                database.execute("INSERT OR IGNORE INTO project_aliases VALUES(?,?,?)", (project_id, title, _key(title)))
        return next(project for project in self.projects() if project["id"] == project_id)

    def set_aliases(self, project_id, aliases):
        project_id = _identifier(project_id)
        if not isinstance(aliases, (list, tuple)) or len(aliases) > 10:
            raise ValueError("Provide at most 10 project aliases")
        values = {_key(alias): _name(alias) for alias in aliases}
        with self._connection() as database:
            database.execute("BEGIN IMMEDIATE")
            if not database.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ValueError("Project not found")
            database.execute("DELETE FROM project_aliases WHERE project_id=?", (project_id,))
            database.executemany("INSERT INTO project_aliases VALUES(?,?,?)", ((project_id, alias, key) for key, alias in values.items()))

    def set_editor(self, project_id, editor):
        if editor not in EDITORS:
            raise ValueError("Choose Explorer, VS Code, PyCharm or IntelliJ IDEA")
        with self._connection() as database:
            if not database.execute("UPDATE projects SET editor=? WHERE id=?", (editor, _identifier(project_id))).rowcount:
                raise ValueError("Project not found")
        return next(project for project in self.projects() if project["id"] == project_id)

    def projects(self):
        with self._connection() as database:
            rows = database.execute("SELECT projects.*,project_roots.path AS root_path FROM projects JOIN project_roots ON project_roots.id=projects.root_id ORDER BY projects.id LIMIT 200").fetchall()
            aliases = database.execute("SELECT * FROM project_aliases ORDER BY alias").fetchall()
        result = []
        for row in rows:
            project = dict(row)
            try:
                project["available"] = _inside(_folder(project["path"]), _folder(project["root_path"]))
            except (OSError, ValueError):
                project["available"] = False
            project.pop("root_path")
            project["aliases"] = [alias["alias"] for alias in aliases if alias["project_id"] == project["id"]]
            result.append(project)
        return result

    def resolve(self, name):
        key = _key(name)
        projects = [project for project in self.projects() if project["available"]]
        matches = [project for project in projects if key in {_key(project["name"]), *(_key(alias) for alias in project["aliases"])}]
        if len(matches) == 1:
            return {"status": "found", "project": matches[0], "suggestions": []}
        suggestions = matches or [project for project in projects if any(key in _key(candidate) for candidate in (project["name"], *project["aliases"]))][:5]
        return {"status": "ambiguous" if matches else "missing", "project": None, "suggestions": suggestions[:5]}

    def scan(self, root_id=None, *, max_depth=2, limit=MAX_PROJECTS):
        if type(max_depth) is not int or not 0 <= max_depth <= 2 or type(limit) is not int or not 1 <= limit <= MAX_PROJECTS:
            raise ValueError("Scan depth is 0–2 and project limit is 1–200")
        roots = self.roots()
        if root_id is not None:
            root_id = _identifier(root_id)
            roots = [root for root in roots if root["id"] == root_id]
            if not roots:
                raise ValueError("Approved project root not found")
        found, identities, scanned, entries, truncated = [], set(), 0, 0, False
        for root in roots:
            queue = [(Path(root["path"]), 0)]
            while queue:
                folder, depth = queue.pop(0)
                try:
                    folder = _folder(folder)
                    if not _inside(folder, _folder(root["path"])):
                        continue
                    scanned += 1
                    if any((folder / marker).exists() for marker in _MARKERS):
                        project = self.add_project(folder, root_id=root["id"])
                        if project["id"] not in identities:
                            identities.add(project["id"])
                            found.append(project)
                        if len(found) >= limit:
                            truncated = True
                            break
                    if depth == max_depth:
                        continue
                    children = []
                    with os.scandir(folder) as iterator:
                        for entry in iterator:
                            entries += 1
                            if entries > MAX_ENTRIES:
                                truncated = True
                                break
                            try:
                                if (not _hidden(entry) and entry.name.casefold() not in _EXCLUDED
                                        and entry.is_dir(follow_symlinks=False) and not entry.is_symlink()):
                                    children.append(Path(entry.path))
                            except OSError:
                                continue
                    queue.extend((child, depth + 1) for child in sorted(children))
                except _CatalogFull:
                    truncated = True
                    break
                except (OSError, ValueError):
                    continue
                if truncated:
                    break
            if truncated:
                break
        return {"projects": found, "scanned": scanned, "truncated": truncated}

    def remove(self, project_id):
        with self._connection() as database:
            return bool(database.execute("DELETE FROM projects WHERE id=?", (_identifier(project_id),)).rowcount)

    def remove_root(self, root_id):
        with self._connection() as database:
            return bool(database.execute("DELETE FROM project_roots WHERE id=?", (_identifier(root_id),)).rowcount)

    def open_project(self, name, editor=None, *, executor=None):
        if editor is not None and editor not in EDITORS:
            raise ValueError("Choose Explorer, VS Code, PyCharm or IntelliJ IDEA")
        result = self.resolve(name)
        if result["status"] != "found":
            result["message"] = ("Нашёл несколько проектов с таким именем. Уточни уникальное название или псевдоним."
                                 if result["status"] == "ambiguous" else "Не нашёл проект в разрешённых папках. Добавь его через настройки проектов.")
            return result
        project = result["project"]
        editor = project["editor"] if editor is None else editor
        try:
            if editor not in EDITORS:
                raise ValueError("Invalid project editor")
            # Recheck immediately before dispatch; a deleted/replaced root must
            # not let stale catalog records escape their original approval.
            root = next(root for root in self.roots() if root["id"] == project["root_id"])
            folder = _folder(project["path"])
            if not _inside(folder, _folder(root["path"])):
                raise ValueError("Project folder changed")
            (executor or self.executor or _native_open)(folder, editor)
        except (OSError, ValueError, RuntimeError, StopIteration, subprocess.SubprocessError):
            return {**result, "status": "failed", "message": "Не удалось открыть проект. Проверь, доступна ли папка и установлен ли выбранный редактор."}
        application = {"explorer": "Проводнике", "vscode": "Visual Studio Code", "pycharm": "PyCharm", "idea": "IntelliJ IDEA"}[editor]
        return {**result, "status": "requested", "message": f"Открываю проект «{project['name']}» в {application}."}


_CODE_PROBE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$candidates = @()
if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\Code.exe' }
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($root) { $candidates += Join-Path $root 'Microsoft VS Code\Code.exe' }
}
foreach ($path in $candidates) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $signature = Get-AuthenticodeSignature -LiteralPath $path
    if ($signature.Status -ne 'Valid') { continue }
    $publisher = $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    if ($publisher -ne 'Microsoft Corporation') { continue }
    @{protocol=1;path=$path;sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash} | ConvertTo-Json -Compress
    exit 0
}
@{protocol=1;path=$null;sha256=$null} | ConvertTo-Json -Compress
"""


def _code_candidates():
    candidates = set()
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(key):
            suffix = "Programs/Microsoft VS Code/Code.exe" if key == "LOCALAPPDATA" else "Microsoft VS Code/Code.exe"
            candidates.add(Path(os.environ[key]).resolve() / suffix)
    return candidates


def _verified_vscode(system, *, runner=None):
    """Only official install locations plus valid Microsoft signature/hash.

    https://code.visualstudio.com/docs/setup/windows
    https://learn.microsoft.com/powershell/module/microsoft.powershell.security/get-authenticodesignature
    """
    try:
        encoded = base64.b64encode(_CODE_PROBE.encode("utf-16-le")).decode("ascii")
        response = (runner or subprocess.run)(
            [str(system / "WindowsPowerShell/v1.0/powershell.exe"), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
            env=powershell_environment(system), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if response.returncode or not isinstance(response.stdout, bytes) or len(response.stdout) > 8192:
            return None
        data = json.loads(response.stdout.decode("utf-8-sig"))
        if not isinstance(data, dict) or set(data) != {"protocol", "path", "sha256"} or type(data["protocol"]) is not int or data["protocol"] != 1:
            return None
        raw, digest = data["path"], data["sha256"]
        if (not isinstance(raw, str) or "\x00" in raw or "://" in raw or raw.startswith(("\\\\", "//"))
                or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)):
            return None
        path = Path(raw)
        if not path.is_absolute() or path not in _code_candidates() or path.resolve(strict=True) != path or not path.is_file():
            return None
        if not 0 < path.stat().st_size <= 512 * 1024 * 1024:
            return None
        with path.open("rb") as stream:
            return path if hashlib.file_digest(stream, "sha256").hexdigest() == digest.lower() else None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def _system_directory():
    if os.name != "nt":
        raise RuntimeError("Native project opening requires Windows")
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise OSError("Windows system directory is unavailable")
    return Path(buffer.value)


def _jetbrains_candidates(editor):
    """Bounded metadata walk in official installation trees, not the home."""
    if editor not in {"pycharm", "idea"}:
        return set()
    filename = {"pycharm": "pycharm64.exe", "idea": "idea64.exe"}[editor]
    roots = [Path(os.environ[key]) / "JetBrains" for key in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(key)]
    if os.environ.get("LOCALAPPDATA"):
        local = Path(os.environ["LOCALAPPDATA"])
        roots.append(local / "JetBrains/Toolbox/apps")
        products = (("PyCharm", "PyCharm Professional", "PyCharm Community") if editor == "pycharm" else
                    ("IntelliJ IDEA", "IntelliJ IDEA Ultimate", "IntelliJ IDEA Community"))
        roots.extend(local / "Programs" / product for product in products)
    found, entries = set(), 0
    for root in roots:
        queue = [(root, 0)]
        while queue and entries < 1000 and len(found) < 10:
            folder, depth = queue.pop(0)
            try:
                folder = _folder(folder)
                if not _inside(folder, root):
                    continue
                candidate = folder / filename
                if folder.name.casefold() == "bin" and candidate.is_file() and candidate.resolve(strict=True) == candidate:
                    found.add(candidate)
                if depth >= 5:
                    continue
                with os.scandir(folder) as iterator:
                    for entry in iterator:
                        entries += 1
                        if entries >= 1000:
                            break
                        if not _hidden(entry) and entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                            queue.append((Path(entry.path), depth + 1))
            except (OSError, ValueError):
                continue
    return found


def _verified_jetbrains(system, editor, *, runner=None):
    """Exact launcher names, approved install trees and JetBrains signature."""
    try:
        candidates = _jetbrains_candidates(editor)
        if not candidates:
            return None
        encoded_paths = base64.b64encode(json.dumps(sorted(map(str, candidates))).encode()).decode("ascii")
        # The encoded JSON is data, never PowerShell source or Invoke-Expression.
        probe = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                 "$paths=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded_paths + "'))|ConvertFrom-Json); "
                 "foreach($path in $paths){$signature=Get-AuthenticodeSignature -LiteralPath $path; "
                 "if($signature.Status -ne 'Valid'){continue}; "
                 "$publisher=$signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false); "
                 "if($publisher -ne 'JetBrains s.r.o.'){continue}; "
                 "@{protocol=1;path=$path;sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash}|ConvertTo-Json -Compress; exit 0}; "
                 "@{protocol=1;path=$null;sha256=$null}|ConvertTo-Json -Compress")
        response = (runner or subprocess.run)(
            [str(system / "WindowsPowerShell/v1.0/powershell.exe"), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(probe.encode("utf-16-le")).decode("ascii")],
            shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
            env=powershell_environment(system), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if response.returncode or not isinstance(response.stdout, bytes) or len(response.stdout) > 8192:
            return None
        data = json.loads(response.stdout.decode("utf-8-sig"))
        if not isinstance(data, dict) or set(data) != {"protocol", "path", "sha256"} or type(data["protocol"]) is not int or data["protocol"] != 1:
            return None
        raw, digest = data["path"], data["sha256"]
        if not isinstance(raw, str) or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            return None
        path = Path(raw)
        if path not in candidates or path.resolve(strict=True) != path or not path.is_file() or not 0 < path.stat().st_size <= 512 * 1024 * 1024:
            return None
        with path.open("rb") as stream:
            return path if hashlib.file_digest(stream, "sha256").hexdigest() == digest.lower() else None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def _native_open(folder, editor):
    system = _system_directory()
    if editor == "explorer":
        executable = system.parent / "explorer.exe"
    elif editor == "vscode":
        executable = _verified_vscode(system)
    elif editor in {"pycharm", "idea"}:
        executable = _verified_jetbrains(system, editor)
    else:
        raise ValueError("Unapproved project editor")
    if executable is None or not executable.is_file():
        raise OSError("Verified project application is unavailable")
    arguments = [str(executable), "--new-window", str(folder)] if editor == "vscode" else [str(executable), str(folder)]
    subprocess.Popen(arguments, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, cwd=str(system), env=powershell_environment(system))
