from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HabitLog(BaseModel):
    name: str


class WorkoutSetPayload(BaseModel):
    exercise_name: str
    set_number: int | None = Field(default=None, ge=1, le=100)
    weight_kg: float | None = Field(default=None, ge=0, le=2000)
    reps: int | None = Field(default=None, ge=0, le=1000)
    rir: float | None = Field(default=None, ge=0, le=10)
    technique_ok: bool | None = None
    notes: str | None = None


class WorkoutLogPayload(BaseModel):
    title: str = "Workout"
    sets: list[WorkoutSetPayload] = Field(default_factory=list)
    notes: str | None = None


class ReflectionPayload(BaseModel):
    deep_work_hours: float | None = Field(default=None, ge=0, le=24)
    calories_hit: bool | None = None
    protein_hit: bool | None = None
    rir_respected: bool | None = None
    mood: int | None = Field(default=None, ge=1, le=5)
    energy: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = None


class ReminderPayload(BaseModel):
    text: str
    remind_at: str = Field(description="Local datetime in YYYY-MM-DD HH:MM:SS")


class AssignmentPayload(BaseModel):
    title: str = Field(description="Concrete assignment/task title")
    course: str | None = Field(default=None, description="Course name if known")
    due_at: str | None = Field(
        default=None,
        description="Local deadline in YYYY-MM-DD HH:MM:SS. Null when the user did not give enough information.",
    )
    url: str | None = None
    notes: str | None = None


class MemoryUpdate(BaseModel):
    category: Literal["profile", "preference", "project", "training", "schedule", "other"] = "other"
    key: str = Field(description="Stable concise identifier")
    value: str
    importance: int = Field(default=5, ge=1, le=10)


class JarvisResponse(BaseModel):
    reply: str
    workout_log: WorkoutLogPayload | None = None
    habit_logs: list[HabitLog] = Field(default_factory=list)
    reflection: ReflectionPayload | None = None
    memory_updates: list[MemoryUpdate] = Field(default_factory=list)
    reminders: list[ReminderPayload] = Field(default_factory=list)
    assignments: list[AssignmentPayload] = Field(default_factory=list)
    system_command: Literal["lock", "sleep", "shutdown", "restart"] | None = None
