from pydantic import BaseModel, ConfigDict, Field

from database import engine
from database.v4_models import UserSettings


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sleep_hours: float = Field(default=8, ge=6, le=12)
    breakfast_minutes: int = Field(default=20, ge=0, le=120)
    shower_minutes: int = Field(default=15, ge=0, le=120)
    default_commute_minutes: int = Field(default=30, ge=0, le=240)
    preparation_minutes: int = Field(default=15, ge=0, le=120)
    day_start: str = Field(default="08:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    day_end: str = Field(default="23:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    dnd_start: str = Field(default="23:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    dnd_end: str = Field(default="07:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    max_work_minutes: int = Field(default=300, ge=30, le=600)
    free_fraction: float = Field(default=0.3, ge=0.15, le=0.75)
    course_weights: dict[str, int] = Field(default_factory=dict, max_length=50)
    quiz_open_notices: bool = False
    muted_kinds: list[str] = Field(default_factory=list, max_length=20)
    morning_brief: bool = True
    evening_brief: bool = True


async def get_preferences(user_id):
    async with engine.async_session_factory() as db:
        row = await db.get(UserSettings, user_id)
        return Preferences.model_validate(row.values if row else {})


async def save_preferences(user_id, updates):
    existing = await get_preferences(user_id)
    value = Preferences.model_validate({**existing.model_dump(), **updates})
    if value.day_start >= value.day_end:
        raise ValueError("day_end must be after day_start")
    if any(not 1 <= weight <= 10 for weight in value.course_weights.values()):
        raise ValueError("Course weights must be 1..10")
    async with engine.async_session_factory() as db:
        row = await db.get(UserSettings, user_id)
        if row is None:
            row = UserSettings(user_id=user_id)
            db.add(row)
        row.values = value.model_dump()
        await db.commit()
    return value
