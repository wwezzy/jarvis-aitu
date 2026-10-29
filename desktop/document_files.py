"""Explicit immutable local documents; extracted text is data, never commands."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
import uuid
import zipfile

from desktop.personal import safe_context_text

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 50000
_KINDS = {".txt": "txt", ".md": "md", ".pdf": "pdf", ".docx": "docx"}
_ID = re.compile(r"[a-f0-9]{32}")
_APPLICATION = 1246905412


class DocumentError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _regular(path, *, require_file=True):
    raw = str(path)
    if not path.is_absolute() or raw.startswith(("\\\\", "//")) or "://" in raw or ".." in path.parts or "\0" in raw:
        raise DocumentError("local_path_required")
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            if entry == path and require_file:
                raise DocumentError("file_missing") from None
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DocumentError("linked_path_refused")
    if require_file:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DocumentError("regular_file_required")
        return info


def _docx(data):
    try:
        from defusedxml import ElementTree
    except ImportError:
        raise DocumentError("dependencies_missing") from None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 512 or sum(item.file_size for item in entries) > 30 * 1024 * 1024:
                raise DocumentError("archive_limit")
            names = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (entry.filename in names or path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                        or ":" in entry.filename or entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.file_size > 8 * 1024 * 1024
                        or entry.file_size > max(1, entry.compress_size) * 100):
                    raise DocumentError("archive_limit")
                names.add(entry.filename)
            if "word/document.xml" not in names:
                raise DocumentError("invalid_document")
            xml = archive.read("word/document.xml")
            if len(xml) > 5 * 1024 * 1024:
                raise DocumentError("archive_limit")
        root = ElementTree.fromstring(xml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        paragraphs = []
        size = 0
        for element in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
            text = "".join(item.text or "" for item in element.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
            paragraphs.append(text)
            size += len(text) + 1
            if size > MAX_CHARS:
                break
        return "\n".join(paragraphs)
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("invalid_document") from None


def _pdf(data):
    try:
        from pypdf import Configuration, PdfReader, apply_configuration
    except ImportError:
        raise DocumentError("dependencies_missing") from None
    try:
        config = Configuration(maximum_declared_stream_length=8 * 1024 * 1024,
            array_based_stream_maximum_output_length=8 * 1024 * 1024, zlib_maximum_output_length=8 * 1024 * 1024,
            zlib_maximum_recovery_input_length=1024 * 1024, lzw_maximum_output_length=8 * 1024 * 1024,
            run_length_maximum_output_length=8 * 1024 * 1024, image_maximum_buffer_size=8 * 1024 * 1024,
            page_tree_maximum_entries=31, page_tree_maximum_depth=20, xform_maximum_invocations_per_extraction=100,
            jbig2dec_binary=None)
        with apply_configuration(config):
            reader = PdfReader(io.BytesIO(data), strict=True, root_object_recovery_limit=100)
            if reader.is_encrypted:
                raise DocumentError("encrypted_document")
            if len(reader.pages) > 30:
                raise DocumentError("page_limit")
            parts, size = [], 0
            for page in reader.pages:
                text = page.extract_text() or ""
                parts.append(text)
                size += len(text) + 1
                if size > MAX_CHARS:
                    break
            return "\n".join(parts)
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("invalid_document") from None


def _extract(data, kind):
    if kind in {"txt", "md"}:
        try:
            text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeError:
            raise DocumentError("encoding_unsupported") from None
    else:
        text = _pdf(data) if kind == "pdf" else _docx(data)
    if "\0" in text:
        raise DocumentError("binary_text_refused")
    text = "".join(char for char in text if ord(char) >= 32 or char in "\n\r\t").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise DocumentError("no_text")
    return text[:MAX_CHARS], len(text) > MAX_CHARS


class DocumentFiles:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.directory = self.state_dir / "documents"
        self.path = self.state_dir / "documents.sqlite3"

    @contextmanager
    def _connection(self):
        _regular(self.path, require_file=False)
        _regular(self.directory, require_file=False)
        if self.path.exists():
            _regular(self.path)
            if self.path.stat().st_size > 32 * 1024 * 1024:
                raise DocumentError("storage_limit")
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables and (version != 1 or application != _APPLICATION):
                raise DocumentError("invalid_storage")
            if version not in {0, 1} or application not in {0, _APPLICATION}:
                raise DocumentError("invalid_storage")
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,
                text TEXT NOT NULL, truncated INTEGER NOT NULL, sha256 TEXT NOT NULL, imported_at REAL NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0)""")
            db.execute(f"PRAGMA application_id={_APPLICATION}")
            db.execute("PRAGMA user_version=1")
            db.commit()
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _record(row):
        value = {key: row[key] for key in ("id", "name", "kind", "size", "text")}
        value.update(preview=row["text"][:500], truncated=bool(row["truncated"]), status="ready")
        return value

    def import_file(self, path):
        source = Path(path)
        kind = _KINDS.get(source.suffix.casefold())
        if kind is None:
            raise DocumentError("format_unsupported")
        before = _regular(source)
        if not 0 < before.st_size <= MAX_BYTES:
            raise DocumentError("file_limit")
        with source.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_nlink != 1 or (before.st_dev, before.st_ino, before.st_size) != (opened.st_dev, opened.st_ino, opened.st_size):
                raise DocumentError("file_changed")
            data = handle.read(MAX_BYTES + 1)
        after = _regular(source)
        if (len(data) != before.st_size or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
            raise DocumentError("file_changed")
        text, truncated = _extract(data, kind)
        identity = uuid.uuid4().hex
        name = "".join(char for char in source.name if ord(char) >= 32)[:128]
        blob = self.directory / (identity + ".blob")
        created = False
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                count, total = db.execute("SELECT count(*),coalesce(sum(size),0) FROM documents WHERE deleted=0").fetchone()
                if count >= 100 or total + len(data) > 200 * 1024 * 1024:
                    raise DocumentError("storage_limit")
                _regular(blob, require_file=False)
                with blob.open("xb") as output:
                    created = True
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,0)",
                    (identity, name, kind, len(data), text, int(truncated), hashlib.sha256(data).hexdigest(), time.time()))
        except BaseException:
            if created:
                blob.unlink(missing_ok=True)
            raise
        return {"id": identity, "name": name, "kind": kind, "size": len(data), "text": text,
                "preview": text[:500], "truncated": truncated, "status": "ready"}

    def get(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            return None
        with self._connection() as db:
            row = db.execute("SELECT * FROM documents WHERE id=? AND deleted=0", (identity,)).fetchone()
        return self._record(row) if row else None

    def list(self):
        with self._connection() as db:
            rows = db.execute("SELECT * FROM documents WHERE deleted=0 ORDER BY imported_at DESC,id LIMIT 100").fetchall()
        return [self._record(row) for row in rows]

    def delete(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            return False
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM documents WHERE id=?", (identity,)).fetchone() is None:
                return False
            db.execute("UPDATE documents SET deleted=1,text='' WHERE id=?", (identity,))
        blob = self.directory / (identity + ".blob")
        _regular(blob, require_file=False)
        try:
            blob.unlink(missing_ok=True)
        except OSError:
            return False  # Context is already revoked; physical cleanup can be retried.
        return True

    def context(self, identities):
        if (not isinstance(identities, (list, tuple)) or len(identities) > 5
                or any(not isinstance(identity, str) or _ID.fullmatch(identity) is None for identity in identities)):
            raise DocumentError("selection_limit")
        result, remaining = [], 12000
        for identity in dict.fromkeys(identities):
            row = self.get(identity)
            if row is None or remaining <= 0:
                continue
            text = safe_context_text(row["text"], min(8000, remaining))
            result.append({"id": identity, "name": safe_context_text(row["name"], 128), "kind": row["kind"], "text": text,
                           "truncated": row["truncated"] or len(row["text"]) > len(text), "authority": "data_only"})
            remaining -= len(text)
        return result
