import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ADMIN_ID", "42")

from services.schedule import schedule_kind_for_weekday


def test_master_schedule_days():
    assert schedule_kind_for_weekday(0) == "B"  # Monday
    assert schedule_kind_for_weekday(2) == "B"  # Wednesday
    assert schedule_kind_for_weekday(1) == "A"  # Tuesday
    assert schedule_kind_for_weekday(3) == "A"  # Thursday
    assert schedule_kind_for_weekday(4) == "FLEX"
    assert schedule_kind_for_weekday(6) == "FLEX"
