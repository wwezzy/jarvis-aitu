"""Personal assistant state; observations never imply task or goal completion."""
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.models import Base
from database.time import UTCDateTime, utcnow


class Goal(Base):
    __tablename__ = "assistant_goals"
    __table_args__ = (UniqueConstraint("user_id", "template_key", name="uq_goal_template"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    template_key: Mapped[str | None] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(180))
    reason: Mapped[str] = mapped_column(String(500), default="")
    next_step: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(24))
    target_per_week: Mapped[int] = mapped_column(Integer, default=3)
    minimum_minutes: Mapped[int] = mapped_column(Integer, default=10)
    cue: Mapped[str] = mapped_column(String(180), default="После первого свободного окна")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class GoalCheckIn(Base):
    __tablename__ = "assistant_checkins"
    __table_args__ = (UniqueConstraint("user_id", "goal_id", "on_date", name="uq_goal_checkin"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("assistant_goals.id", ondelete="CASCADE"), index=True)
    on_date: Mapped[date] = mapped_column(Date, index=True)
    outcome: Mapped[str] = mapped_column(String(16))
    minutes: Mapped[int] = mapped_column(Integer, default=0)
    obstacle: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(32), default="explicit_user")


class AssistantState(Base):
    __tablename__ = "assistant_state"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True, autoincrement=False)
    paused_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_interaction_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    active_focus_id: Mapped[int | None] = mapped_column(Integer)
    activity_reset_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class FocusSession(Base):
    __tablename__ = "assistant_focus"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("assistant_goals.id", ondelete="SET NULL"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    reported_minutes: Mapped[int | None] = mapped_column(Integer)


class KnowledgeNote(Base):
    __tablename__ = "assistant_notes"
    __table_args__ = (UniqueConstraint("user_id", "content_hash", name="uq_note_content"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="inbox")
    source: Mapped[str] = mapped_column(String(80), default="explicit_capture")
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ActivityReceipt(Base):
    __tablename__ = "assistant_activity_receipts"
    __table_args__ = (UniqueConstraint("user_id", "bucket_id", name="uq_activity_receipt"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    bucket_id: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    seconds: Mapped[dict] = mapped_column(JSON)
