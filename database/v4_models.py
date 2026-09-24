"""Durable v4 state. Raw inbox attachments and provider prompts never live here."""
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.models import Base
from database.time import UTCDateTime, utcnow


class LmsEvent(Base):
    __tablename__ = "lms_events"
    __table_args__ = (UniqueConstraint("user_id", "source", "external_uid", name="uq_lms_uid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="lms_ical")
    external_uid: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    course: Mapped[str | None] = mapped_column(String(180))
    teacher: Mapped[str | None] = mapped_column(String(180))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    last_modified: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    missing_sync_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_hash: Mapped[str] = mapped_column(String(64))


class ScheduleOverride(Base):
    __tablename__ = "schedule_overrides"
    __table_args__ = (UniqueConstraint("user_id", "entry_id", "on_date", name="uq_dated_override"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("schedule_entries.id", ondelete="CASCADE"))
    on_date: Mapped[date] = mapped_column(Date, index=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)
    provenance: Mapped[str] = mapped_column(String(120), default="user")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class UserSettings(Base):
    __tablename__ = "user_settings"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True, autoincrement=False)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class NotificationDelivery(Base):
    __tablename__ = "notification_delivery"
    __table_args__ = (UniqueConstraint("user_id", "event_key", name="uq_delivery_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    event_key: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    critical: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(120))
    capability: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int] = mapped_column(Integer)
    error_class: Mapped[str | None] = mapped_column(String(80))


class StudyTopic(Base):
    __tablename__ = "study_topics"
    __table_args__ = (UniqueConstraint("user_id", "course", "topic", name="uq_study_topic"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    course: Mapped[str] = mapped_column(String(180), default="General")
    topic: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    last_score: Mapped[int | None] = mapped_column(Integer)
    reviews: Mapped[int] = mapped_column(Integer, default=0)
    next_review_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class StudySession(Base):
    __tablename__ = "study_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    topic: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    planned_minutes: Mapped[int] = mapped_column(Integer)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reported_minutes: Mapped[int | None] = mapped_column(Integer)


class PersonalCalendarEvent(Base):
    __tablename__ = "personal_calendar_events"
    __table_args__ = (UniqueConstraint("user_id", "external_id", name="uq_google_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    jarvis_owned: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
