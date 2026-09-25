"""One task repository for manual, Telegram and LMS tasks (legacy table retained)."""
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from database import engine
from database.models import Assignment
from database.time import aware
from database.v4_models import NotificationDelivery
from services.assignments import _serialize
from services.lms import safe_url


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=255)
    course: str | None = Field(default=None, max_length=180)
    details: str | None = Field(default=None, max_length=8000)
    due_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=5, le=100000)
    progress: int = Field(default=0, ge=0, le=100)
    importance: int = Field(default=5, ge=1, le=10)
    consequence: int = Field(default=5, ge=1, le=10)
    preparation_minutes: int = Field(default=0, ge=0, le=1440)
    testing_buffer_minutes: int = Field(default=60, ge=0, le=10080)
    status: Literal["pending", "done", "cancelled"] = "pending"
    url: str | None = None
    teacher_override: bool = False


async def save_task(user_id, payload, task_id=None, *, source="manual", provenance="user"):
    values = TaskInput.model_validate(payload)
    values.title = values.title.strip()
    if not values.title:
        raise ValueError("Task title is required")
    async with engine.async_session_factory() as db:
        row = await db.get(Assignment, task_id) if task_id else None
        if task_id and (row is None or row.user_id != user_id):
            raise ValueError("Task not found")
        if row is None:
            row = Assignment(user_id=user_id, source=source, provenance=provenance)
            db.add(row)
        new_due = aware(values.due_at) if values.due_at else None
        if row.source == "lms_ical" and row.due_at != new_due and not values.teacher_override:
            raise ValueError("LMS deadline needs an explicit teacher override")
        for key, value in values.model_dump(exclude={"teacher_override", "details", "url", "due_at"}).items():
            setattr(row, key, value)
        row.notes = values.details
        row.url = safe_url(values.url)
        row.due_at = new_due
        row.deadline_overridden = row.deadline_overridden or values.teacher_override
        if row.status == "done":
            row.progress = 100
        await db.flush()
        # Pending notices must be rebuilt on any deadline/status edit.
        await db.execute(update(NotificationDelivery).where(
            NotificationDelivery.task_id == row.id,
            NotificationDelivery.status == "queued").values(status="cancelled"))
        await db.commit()
        await db.refresh(row)
        return _serialize(row)


async def list_tasks(user_id, *, status=None, source=None, course=None, limit=200):
    query = select(Assignment).where(Assignment.user_id == user_id)
    for column, value in ((Assignment.status, status), (Assignment.source, source), (Assignment.course, course)):
        if value:
            query = query.where(column == value)
    async with engine.async_session_factory() as db:
        rows = await db.scalars(query.order_by(Assignment.due_at.asc().nulls_last(), Assignment.id).limit(min(500, max(1, limit))))
        return [_serialize(row) for row in rows]


def parse_task_command(text):
    """Exact user-specified values only. A date without a time stays uncertain."""
    body = re.sub(r"^/task(?:@\w+)?\s*", "", text.strip(), flags=re.I)
    parts = [part.strip() for part in body.split("|")]
    if not parts[0]:
        raise ValueError("/task Title | due 2030-01-08 15:00 | estimate 90 | course WEB")
    result = {"title": parts[0]}
    for item in parts[1:]:
        key, _, value = item.partition(" ")
        if key == "due":
            if not re.fullmatch(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)?", value):
                raise ValueError("An exact due date and time are required; omit due when uncertain")
            result["due_at"] = aware(datetime.fromisoformat(value))
        elif key == "estimate":
            result["estimated_minutes"] = int(value)
        elif key == "course":
            result["course"] = value
        elif key == "importance":
            result["importance"] = int(value)
        else:
            raise ValueError("Unknown task field")
    return TaskInput.model_validate(result).model_dump()
