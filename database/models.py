from __future__ import annotations

from datetime import date, datetime
from typing import List

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    preferences: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    workouts: Mapped[List["Workout"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    workout_sessions: Mapped[List["WorkoutSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    habits: Mapped[List["Habit"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    reminders: Mapped[List["Reminder"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    memories: Mapped[List["MemoryFact"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    gtg_sets: Mapped[List["GTGSet"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    reflections: Mapped[List["DailyReflection"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


# Legacy table kept so existing databases continue to open without destructive migration.
class Workout(Base):
    __tablename__ = "workouts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workout_type: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    workout_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)

    user: Mapped["User"] = relationship(back_populates="workouts")


class WorkoutSession(Base):
    __tablename__ = "workout_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="Workout")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="telegram")

    user: Mapped["User"] = relationship(back_populates="workout_sessions")
    sets: Mapped[List["ExerciseSet"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ExerciseSet.id",
    )


class ExerciseSet(Base):
    __tablename__ = "exercise_sets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workout_session_id: Mapped[int] = mapped_column(
        ForeignKey("workout_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exercise_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    set_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    reps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rir: Mapped[float | None] = mapped_column(Float, nullable=True)
    technique_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped["WorkoutSession"] = relationship(back_populates="sets")


class Habit(Base):
    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    completed_at: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)

    user: Mapped["User"] = relationship(back_populates="habits")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped["User"] = relationship(back_populates="reminders")


class MemoryFact(Base):
    __tablename__ = "memory_facts"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_memory_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="other")
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    user: Mapped["User"] = relationship(back_populates="memories")


class GTGSet(Base):
    __tablename__ = "gtg_sets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reps: Mapped[int] = mapped_column(Integer, nullable=False)
    logged_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="button")

    user: Mapped["User"] = relationship(back_populates="gtg_sets")


class DailyReflection(Base):
    __tablename__ = "daily_reflections"
    __table_args__ = (UniqueConstraint("user_id", "reflection_date", name="uq_reflection_user_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reflection_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today, index=True)
    deep_work_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    calories_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    protein_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rir_respected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mood: Mapped[int | None] = mapped_column(Integer, nullable=True)
    energy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    user: Mapped["User"] = relationship(back_populates="reflections")
