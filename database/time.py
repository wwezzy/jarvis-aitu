"""UTC storage with aware local datetimes at the application boundary.

Naive legacy API inputs mean the configured user timezone, never the host timezone.
The physical TIMESTAMP columns contain UTC on both SQLite and PostgreSQL.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

from config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    zone = get_settings().timezone
    return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return aware(value).astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc).astimezone(get_settings().timezone)
