import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_ID", "42")
os.environ.setdefault("GEMINI_API_KEY", "primary")
os.environ.setdefault("GEMINI_API_KEY_2", "backup")
os.environ.setdefault("GEMINI_API_KEY_3", "backup")

from config import _collect_gemini_api_keys
from services.direct_intents import _simple_query


def test_gemini_keys_are_deduplicated():
    keys = _collect_gemini_api_keys()
    assert keys[0] == "primary"
    assert keys.count("backup") == 1


def test_planning_queries_are_not_short_circuited():
    assert _simple_query("что сейчас по расписанию") is True
    assert _simple_query("распредели мне дз до пятницы") is False
