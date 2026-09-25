from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from openai import AsyncOpenAI

from config import get_settings
from services.telemetry import observe

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_FILE_BYTES = 45 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024

_OPENAI_FILE_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/rtf",
    "text/rtf",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "application/csv",
    "text/tab-separated-values",
    "text/plain",
    "text/markdown",
    "application/json",
    "text/html",
    "text/xml",
    "application/xml",
    "text/css",
    "text/javascript",
    "application/javascript",
    "text/x-python",
    "text/x-java",
    "text/x-c",
    "text/x-c++",
}


class AttachmentError(ValueError):
    pass


class AttachmentTooLarge(AttachmentError):
    pass


class UnsupportedAttachment(AttachmentError):
    pass


@dataclass(slots=True)
class Attachment:
    data: bytes
    filename: str
    mime_type: str
    kind: str

    @property
    def size(self) -> int:
        return len(self.data)


def ensure_size(size: int | None, *, audio: bool = False) -> None:
    if size is None:
        return
    limit = MAX_AUDIO_BYTES if audio else MAX_FILE_BYTES
    if size > limit:
        mb = limit // (1024 * 1024)
        raise AttachmentTooLarge(f"Файл слишком большой. Лимит для этого типа: {mb} MB.")


def openai_file_supported(mime_type: str | None, filename: str | None = None) -> bool:
    mime = (mime_type or "").lower()
    if mime in _OPENAI_FILE_MIME_TYPES or mime.startswith("text/"):
        return True
    suffix = Path(filename or "").suffix.lower()
    return suffix in {
        ".pdf", ".doc", ".docx", ".rtf", ".odt",
        ".ppt", ".pptx", ".csv", ".tsv", ".xls", ".xlsx",
        ".txt", ".md", ".json", ".html", ".xml",
        ".py", ".java", ".c", ".cpp", ".js", ".ts", ".css", ".sql",
    }


def _convert_ogg_to_mp3_sync(data: bytes) -> bytes:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="jarvis_voice_") as temp_dir:
        source = Path(temp_dir) / "voice.ogg"
        target = Path(temp_dir) / "voice.mp3"
        source.write_bytes(data)
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(source),
                "-t",
                "1800",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                str(target),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        if result.returncode != 0 or not target.exists():
            raise AttachmentError("Не удалось декодировать голосовое сообщение.")
        converted = target.read_bytes()
        if len(converted) > MAX_AUDIO_BYTES:
            raise AttachmentTooLarge("Голосовое после конвертации превышает лимит 25 MB.")
        return converted


async def convert_telegram_voice(data: bytes) -> tuple[bytes, str, str]:
    if not data:
        raise AttachmentError("Пустое голосовое сообщение.")
    ensure_size(len(data), audio=True)
    converted = await asyncio.to_thread(_convert_ogg_to_mp3_sync, data)
    return converted, "voice.mp3", "audio/mpeg"


async def transcribe_telegram_voice(data: bytes) -> str:
    if not settings.openai_api_key or not settings.openai_transcribe_model:
        raise AttachmentError("Для расшифровки голосовых сейчас не настроен OpenAI API.")

    converted, filename, _ = await convert_telegram_voice(data)
    audio_file = io.BytesIO(converted)
    audio_file.name = filename

    async with AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    ) as client:
        response = await observe("openai", settings.openai_transcribe_model, "transcription", asyncio.wait_for(
            client.audio.transcriptions.create(
                model=settings.openai_transcribe_model,
                file=audio_file,
                prompt=(
                    "Personal assistant context. The speaker may use Russian, Kazakh and English. "
                    "Common terms: AITU, Astana IT University, Jarvis, LMS, SDP, DAA, WEB, OS, "
                    "Python, Java, GitHub, Render."
                ),
            ),
            timeout=settings.llm_timeout_seconds,
        ))

    transcript = (getattr(response, "text", "") or "").strip()
    if not transcript:
        raise AttachmentError("Не удалось получить текст из голосового сообщения.")
    return transcript
