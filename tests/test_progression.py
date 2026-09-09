import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ADMIN_ID", "42")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from services.progression import double_progression_recommendation


def test_increase_when_rep_ceiling_reached():
    result = double_progression_recommendation([
        {"reps": 12, "rir": 2},
        {"reps": 12, "rir": 2},
        {"reps": 12, "rir": 2},
    ])
    assert result["action"] == "increase_load"


def test_hold_and_add_reps_inside_range():
    result = double_progression_recommendation([
        {"reps": 10, "rir": 2},
        {"reps": 9, "rir": 2},
        {"reps": 8, "rir": 2},
    ])
    assert result["action"] == "hold_load_add_reps"


def test_hold_or_reduce_below_range():
    result = double_progression_recommendation([
        {"reps": 7, "rir": 1},
        {"reps": 6, "rir": 0},
    ])
    assert result["action"] == "hold_or_reduce"
