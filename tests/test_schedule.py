import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ADMIN_ID", "42")

from services.schedule import DAY_FOCUS, DAY_NAMES_RU, default_blocks_for_weekday, default_schedule_count


def test_week_has_all_days():
    assert set(DAY_NAMES_RU) == set(range(7))
    assert set(DAY_FOCUS) == set(range(7))
    assert default_schedule_count() > 70


def test_real_aitu_blocks_are_present():
    monday = default_blocks_for_weekday(0)
    thursday = default_blocks_for_weekday(3)
    sunday = default_blocks_for_weekday(6)

    assert any(b.start == "14:00" and "WEB" in b.title and b.block_type == "fixed" for b in monday)
    assert any(b.start == "12:00" and "алгоритмов" in b.title.lower() and b.block_type == "fixed" for b in thursday)
    assert any("уборка" in b.title.lower() for b in sunday)
    assert any("Weekly Review" in b.title for b in sunday)


def test_online_classes_are_flexible():
    tuesday = default_blocks_for_weekday(1)
    thursday = default_blocks_for_weekday(3)
    saturday = default_blocks_for_weekday(5)

    async_blocks = [
        b for blocks in (tuesday, thursday, saturday) for b in blocks if b.title.startswith("Async:")
    ]
    assert async_blocks
    assert all(block.block_type == "flex" for block in async_blocks)
