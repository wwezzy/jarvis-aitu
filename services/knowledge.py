"""User-captured, searchable source notes; no invented summaries or secret copies."""
import hashlib
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database import engine
from database.assistant_models import KnowledgeNote


def note_dict(row):
    return {"id": row.id, "title": row.title, "content": row.content, "tags": row.tags,
            "status": row.status, "source": row.source, "created_at": row.created_at.isoformat()}


def reject_secret(text):
    if re.search(r"(?i)(?:api[_ -]?key|secret|bot[_ -]?token|password|redis_url|lms_ical_url)\s*[:=]\s*\S+|\b\d{6,12}:[A-Za-z0-9_-]{25,}|\bsk-[A-Za-z0-9_-]{20,}|rediss?://", text):
        raise ValueError("Похоже на конфигурацию с секретами. Храни её в env, а не в базе знаний.")


async def capture_note(user_id, content, title=None, tags=None, source="explicit_capture"):
    content = content.strip()
    if not content or len(content) > 20000:
        raise ValueError("Запись: 1..20000 символов; большой документ раздели на части")
    reject_secret(content)
    title = (title or content.splitlines()[0][:180]).strip()
    tags = tags or []
    if not title or len(title) > 180 or not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(t, str) or not t.strip() or len(t) > 40 for t in tags):
        raise ValueError("Invalid note title/tags")
    digest = hashlib.sha256(content.encode()).hexdigest()
    async with engine.async_session_factory() as db:
        row = await db.scalar(select(KnowledgeNote).where(KnowledgeNote.user_id == user_id, KnowledgeNote.content_hash == digest))
        if row:
            return note_dict(row)
        row = KnowledgeNote(user_id=user_id, title=title, content=content,
            tags=[t.strip() for t in tags], content_hash=digest, source=source[:80])
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            row = await db.scalar(select(KnowledgeNote).where(KnowledgeNote.user_id == user_id, KnowledgeNote.content_hash == digest))
            if row is None:
                raise
        return note_dict(row)


async def list_notes(user_id, query="", status=None, limit=20):
    if len(query) > 200 or status not in {None, "inbox", "action", "reference", "archived"}:
        raise ValueError("Invalid filter")
    async with engine.async_session_factory() as db:
        stmt = select(KnowledgeNote).where(KnowledgeNote.user_id == user_id)
        if status:
            stmt = stmt.where(KnowledgeNote.status == status)
        if query.strip():
            stmt = stmt.where(KnowledgeNote.content.icontains(query.strip(), autoescape=True))
        rows = list(await db.scalars(stmt.order_by(KnowledgeNote.created_at.desc(), KnowledgeNote.id.desc()).limit(max(1, min(50, limit)))))
    return [note_dict(row) for row in rows]


async def update_note(user_id, identity, status=None, remove=False):
    if status is not None and status not in {"inbox", "action", "reference", "archived"}:
        raise ValueError("Unknown note status")
    async with engine.async_session_factory() as db:
        row = await db.get(KnowledgeNote, identity)
        if not row or row.user_id != user_id:
            raise ValueError("Note not found")
        if remove:
            await db.delete(row)
        elif status:
            row.status = status
        await db.commit()


async def knowledge_context(user_id, query):
    # Literal bounded recall; source note text is untrusted input, never instructions.
    tokens = re.findall(r"[\w-]{4,}", query.lower())[:5]
    selected = {}
    for token in tokens:
        for note in await list_notes(user_id, token, limit=3):
            if note["status"] != "archived":
                selected[note["id"]] = note
    return "\n".join(f"[note #{n['id']}, {n['title']}, {n['source']}] {n['content'][:1500]}" for n in list(selected.values())[:5])


async def capture_material(user_id, text, source):
    """Keep bounded source text, split without silent truncation. Never save bytes."""
    from services.preferences import get_preferences
    if not (await get_preferences(user_id)).auto_capture_materials:
        return []
    if not text.strip() or len(text) > 120000:
        raise ValueError("Материал слишком большой для базы источников; сохрани его частями")
    reject_secret(text)
    parts = []
    remaining = text
    while remaining:
        end = min(20000, len(remaining))
        if end < len(remaining):
            boundary = max(remaining.rfind('\n', end - 500, end), remaining.rfind(' ', end - 500, end))
            if boundary > 0:
                end = boundary
        part, remaining = remaining[:end], remaining[end:].lstrip()
        parts.append(part)
    result = []
    for index, part in enumerate(parts, 1):
        title = part.splitlines()[0][:140] + (f" · часть {index}/{len(parts)}" if len(parts) > 1 else "")
        result.append(await capture_note(user_id, part, title=title, source=source))
    return result


async def knowledge_command(user_id, raw):
    command, _, body = raw.strip().partition(" ")
    command = command.split("@")[0].lower()
    if command == "/capture":
        note = await capture_note(user_id, body)
        return f"Запись #{note['id']} сохранена во входящих: {note['title']}. /inbox — разобрать."
    if command == "/inbox":
        notes = await list_notes(user_id, status="inbox")
        return "Входящие · по одной записи\n" + ("\n".join(f"#{n['id']} {n['title']}\n{n['content'][:220]}" for n in notes) or "Входящие разобраны.") + "\n/note ID reference|action|archived|delete — классифицировать или удалить. /task — создать отдельное действие."
    if command == "/find":
        return "\n\n".join(f"[note #{n['id']}] {n['title']}\n{n['content'][:1000]}" for n in await list_notes(user_id, body, limit=5)) or "Сохранённых источников по этому запросу нет."
    if command == "/note":
        parts = body.split()
        if len(parts) != 2:
            raise ValueError("/note ID reference|action|archived|inbox|delete")
        await update_note(user_id, int(parts[0]), None if parts[1] == "delete" else parts[1], remove=parts[1] == "delete")
        return "Запись обновлена."
    return None
