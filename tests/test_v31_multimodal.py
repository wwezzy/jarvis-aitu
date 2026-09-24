from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers import assistant
from services import attachments, llm
from services.schemas import JarvisActions


def test_openai_file_input_pdf_contains_file_data():
    payload = llm._openai_input(
        [],
        "summarize",
        b"%PDF-1.4 fake",
        "application/pdf",
        "assignment.pdf",
    )
    content = payload[-1]["content"]
    file_item = next(item for item in content if item["type"] == "input_file")
    assert file_item["filename"] == "assignment.pdf"
    assert file_item["file_data"].startswith("data:application/pdf;base64,")
    assert file_item["detail"] == "low"


def test_openai_file_input_docx_is_supported():
    payload = llm._openai_input(
        [],
        "analyze",
        b"fake-docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "report.docx",
    )
    assert any(item["type"] == "input_file" for item in payload[-1]["content"])


def test_openai_rejects_unknown_binary_attachment():
    with pytest.raises(attachments.UnsupportedAttachment):
        llm._openai_input(
            [],
            "analyze",
            b"binary",
            "application/octet-stream",
            "archive.bin",
        )


def test_attachment_size_guard():
    with pytest.raises(attachments.AttachmentTooLarge):
        attachments.ensure_size(attachments.MAX_FILE_BYTES + 1)
    with pytest.raises(attachments.AttachmentTooLarge):
        attachments.ensure_size(attachments.MAX_AUDIO_BYTES + 1, audio=True)


async def test_transcription_uses_gpt_transcribe(monkeypatch):
    monkeypatch.setattr(attachments, "settings", replace(
        attachments.settings, openai_api_key="offline-transcription-test",
        openai_transcribe_model="gpt-transcribe",
    ))
    monkeypatch.setattr(
        attachments,
        "convert_telegram_voice",
        AsyncMock(return_value=(b"mp3-bytes", "voice.mp3", "audio/mpeg")),
    )
    response = SimpleNamespace(text="Привет, это тест")
    fake_client = SimpleNamespace(
        audio=SimpleNamespace(
            transcriptions=SimpleNamespace(create=AsyncMock(return_value=response))
        )
    )
    context = AsyncMock()
    context.__aenter__.return_value = fake_client
    constructor = Mock(return_value=context)
    monkeypatch.setattr(attachments, "AsyncOpenAI", constructor)

    result = await attachments.transcribe_telegram_voice(b"ogg-bytes")
    assert result == "Привет, это тест"
    call = fake_client.audio.transcriptions.create.call_args.kwargs
    assert call["model"] == "gpt-transcribe"
    assert call["file"].name == "voice.mp3"
    context.__aexit__.assert_awaited_once()


async def test_voice_transcript_is_used_for_reply_and_action_extraction(monkeypatch):
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42, full_name="Test"),
        text=None,
        caption=None,
        photo=None,
        document=None,
        voice=SimpleNamespace(file_id="voice", file_size=100),
        answer=AsyncMock(return_value=status),
    )

    monkeypatch.setattr(
        assistant,
        "_download_attachment",
        AsyncMock(return_value=("завтра надо сделать OS lab", None, None, None)),
    )
    monkeypatch.setattr(
        assistant,
        "ensure_user",
        AsyncMock(return_value=SimpleNamespace(preferences=None)),
    )
    monkeypatch.setattr(assistant, "try_direct_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(assistant, "build_memory_context", AsyncMock(return_value="ctx"))
    reply = AsyncMock(return_value="Составил план.")
    extract = AsyncMock(return_value=JarvisActions().model_dump())
    monkeypatch.setattr(assistant, "generate_reply", reply)
    monkeypatch.setattr(assistant, "extract_actions", extract)

    await assistant.assistant_message(message, Mock(), Mock())

    assert reply.call_args.kwargs["text"] == "завтра надо сделать OS lab"
    assert extract.call_args.kwargs["text"] == "завтра надо сделать OS lab"
    status.edit_text.assert_awaited_once_with("Составил план.")
