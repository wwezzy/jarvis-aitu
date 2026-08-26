from datetime import date, datetime
from typing import List
from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, String, Text, func, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    preferences: Mapped[str | None] = mapped_column(Text, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.current_timestamp())
    workouts: Mapped[List["Workout"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    habits: Mapped[List["Habit"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    reminders: Mapped[List["Reminder"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class Workout(Base):
    __tablename__ = "workouts"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    workout_type: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    workout_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    user: Mapped["User"] = relationship(back_populates="workouts")

class Habit(Base):
    __tablename__ = "habits"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    completed_at: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    user: Mapped["User"] = relationship(back_populates="habits")

class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    user: Mapped["User"] = relationship(back_populates="reminders")