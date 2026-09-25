import asyncio
import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from services.attachments import Attachment, AttachmentError
from services.inbox import CollectionStore, normalize, safe_filename
from services.redis_backend import NativeAsyncRedis, create_redis, health
from services.telegram_text import chunks, deliver
from services import llm


@pytest.fixture
def redis():
    return NativeAsyncRedis(fakeredis.aioredis.FakeRedis(decode_responses=True))


async def test_collection_concurrent_append_limits_isolation_and_ack(redis):
    store = CollectionStore(redis)
    await store.start(42, 1)
    await store.append(42, 1, "first", [Attachment(b"visual", "a.png", "image/png", "image")])
    await store.start(42, 1)  # idempotent; never erase
    results = await asyncio.gather(*(store.append(42, 1, str(i), []) for i in range(20)), return_exceptions=True)
    assert sum(isinstance(x, AttachmentError) for x in results) == 9
    assert not await store.active(43, 1)
    text, files = await store.finish(42, 1)
    assert "first" in text and files[0].data == b"visual"
    assert await store.active(42, 1)  # failure/retry retains input
    assert 0 < await redis.ttl(store.key(42, 1)) <= 900
    assert await store.acknowledge(42, 1) == 1
    assert not await store.active(42, 1)


async def test_arrivals_during_analysis_never_erased_and_cancel(redis):
    store = CollectionStore(redis)
    await store.start(42, 1)
    await store.append(42, 1, "first", [])
    await store.finish(42, 1)
    await store.append(42, 1, "late", [])
    assert await store.acknowledge(42, 1) == 0
    assert "late" in (await store.finish(42, 1))[0]
    await store.cancel(42, 1)
    assert not await store.active(42, 1)


async def test_collection_expiry_and_size_reject_without_losing_previous(redis):
    store = CollectionStore(redis)
    await store.start(42, 1)
    await store.append(42, 1, "kept", [])
    with pytest.raises(AttachmentError):
        await store.append(42, 1, "x" * 95000, [])
    assert "kept" in (await store.finish(42, 1))[0]
    await redis.expire(store.key(42, 1), -1)
    assert await store.finish(42, 1) is None
    with pytest.raises(AttachmentError):
        await store.append(42, 1, "expired", [])


async def test_docx_and_utf8_text_are_normalized():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Defense evidence</w:t></w:r></w:p></w:document>')
    text, native = await normalize(stream.getvalue(), "../report.docx", "application/octet-stream")
    assert "Defense evidence" in text and native is None
    assert (await normalize("Қазақ Русский English".encode(), "code.py", "text/plain"))[0] == "Қазақ Русский English"
    assert safe_filename(r"C:\secrets\report.docx") == "report.docx"
    for data, name, mime in [(b"invalid", "bad.pdf", "application/pdf"), (b"\x00", "bad.txt", "text/plain"), (b"x", "bad.zip", "application/zip")]:
        with pytest.raises(AttachmentError):
            await normalize(data, name, mime)


async def test_multifile_payload_and_long_unicode_delivery():
    files = [Attachment(b"image", "a.png", "image/png", "image"), Attachment(b"pdf", "b.pdf", "application/pdf", "document")]
    content = llm._openai_input([], "both", None, None, None, files)[-1]["content"]
    assert [x["type"] for x in content] == ["input_text", "input_image", "input_file"]
    text = "🔥<unsafe>&" * 2000
    parts = list(chunks(text))
    assert "".join(parts) == text
    assert all(len(x.encode("utf-16-le")) // 2 <= 3900 for x in parts)
    message, status = SimpleNamespace(answer=AsyncMock()), SimpleNamespace(edit_text=AsyncMock())
    await deliver(message, status, text)
    assert message.answer.await_count == len(parts) - 1


async def test_absent_and_native_redis(monkeypatch):
    assert create_redis() is None
    assert (await health(None))["backend"] == "absent"
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/1")
    assert isinstance(create_redis(), NativeAsyncRedis)
    monkeypatch.setenv("REDIS_URL", "https://invalid.example")
    with pytest.raises(ValueError, match="REDIS_URL"):
        create_redis()
