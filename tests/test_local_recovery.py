"""Recovery tooling must never target a cloud or existing personal database."""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.recover_sqlite import digest, export_copy


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://example.invalid/jarvis_recovery",
    "postgresql+asyncpg://127.0.0.1/production",
    "sqlite+aiosqlite:///:memory:",
    "postgresql://127.0.0.1/jarvis_recovery",
])
async def test_refuses_cloud_wrong_database_and_driver(url, tmp_path):
    with pytest.raises(ValueError, match="isolated loopback"):
        await export_copy(url, tmp_path / "recovered.sqlite3")
    assert list(tmp_path.iterdir()) == []


async def test_refuses_to_overwrite_local_data_without_connecting(tmp_path):
    destination = tmp_path / "personal.sqlite3"
    destination.write_bytes(b"existing private content")
    with pytest.raises(FileExistsError):
        await export_copy("postgresql+asyncpg://127.0.0.1/jarvis_recovery", destination)
    assert destination.read_bytes() == b"existing private content"


def test_verification_compares_content_and_normalizes_equivalent_utc_values():
    utc = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    local = utc.astimezone(timezone(timedelta(hours=5)))
    assert digest([{"id": 1, "at": utc}]) == digest([{"at": local, "id": 1}])
    assert digest([{"id": 1, "text": "a"}]) != digest([{"id": 1, "text": "b"}])
