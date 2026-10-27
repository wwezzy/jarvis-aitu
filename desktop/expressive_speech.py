"""Optional local Silero male speech for explicit personal, noncommercial use.

Silero v5_6_ru is CC-BY-NC-SA-4.0, not the MIT CIS-base model. Model code is
executable torch.package content: load only the exact checksum-pinned official
asset below. Startup never downloads a model or sends text to any service.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
from html import escape
import importlib.util
from pathlib import Path
import re
import threading


MODEL_REVISION = "e382298387332f2bf461fe6eadfde99104611b0c"
MODEL_NAME = "v5_6_ru.pt"
MODEL_FILES = {
    MODEL_NAME: (147664410, "04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e"),
    "LICENSE_SILERO.txt": (20849, "1349a4b6148492b44f629e64eed676612e234fe9a839e4f3b277c1482c8849f1"),
}
MODEL_URLS = {
    MODEL_NAME: "https://models.silero.ai/models/tts/ru/v5_6_ru.pt",
    "LICENSE_SILERO.txt": f"https://raw.githubusercontent.com/snakers4/silero-models/{MODEL_REVISION}/LICENSE",
}
VOICES = ("aidar", "eugene")
ACKNOWLEDGMENT = "Да, сэр"
SAMPLE_RATE = 24000


class ExpressiveSpeechUnavailable(RuntimeError):
    """A fixed reason label, never private backend errors or the supplied text."""


def default_model_dir() -> Path:
    return Path.home() / "JarvisData" / "publicmodels" / "silero-v5_6-ru"


def verified_model(path: Path) -> bool:
    try:
        for name, (size, expected) in MODEL_FILES.items():
            file = Path(path) / name
            if not file.is_file() or file.stat().st_size != size:
                return False
            with file.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                    return False
        return True
    except OSError:
        return False


def _dependencies_ready() -> bool:
    try:
        return importlib.util.find_spec("torch") is not None
    except (ValueError, ImportError):
        return False


def _speed_tier(rate: int) -> str:
    # Silero's documented native prosody API accepts these five named speeds.
    # This deliberately does not resample PCM or shift the speaker's pitch.
    if rate < 120:
        return "x-slow"
    if rate < 160:
        return "slow"
    if rate < 180:
        return "medium"
    if rate < 225:
        return "fast"
    return "x-fast"


def _load_backend(path: Path):
    import torch

    torch.set_num_threads(4)
    model = torch.package.PackageImporter(str(path / MODEL_NAME)).load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    if not all(voice in model.speakers for voice in VOICES):
        raise ValueError("voice_metadata_invalid")
    return model


def _render(model, text: str, voice: str, tier: str) -> tuple[bytes, int]:
    import torch

    # All markup is fixed here; neither model output nor text can inject SSML.
    markup = f'<speak><prosody rate="{tier}">{escape(text)}</prosody></speak>'
    with torch.inference_mode():
        audio = model.apply_tts(ssml_text=markup, speaker=voice, sample_rate=SAMPLE_RATE,
                                put_accent=True, put_yo=True)
        if (not isinstance(audio, torch.Tensor) or audio.ndim != 1
                or not 0 < audio.numel() <= SAMPLE_RATE * 60
                or not torch.isfinite(audio).all().item()):
            raise ExpressiveSpeechUnavailable("audio_invalid")
        pcm = (audio.detach().cpu().clamp(-1, 1).mul(32767).round().to(torch.int16)
               .numpy().astype("<i2").tobytes())
    return pcm, SAMPLE_RATE


class ExpressiveSpeech:
    """CPU-only PCM16 mono; load on a worker and retain the warm model.

    ``personal_use=True`` is an explicit noncommercial setup choice. The five
    native speed tiers cover the existing 90..240 control. Only the fixed wake
    acknowledgment is cached; arbitrary personal responses stay in memory only
    for their synthesis and are never written by this module.
    """

    def __init__(self, model_dir=None, voice="aidar", personal_use=False):
        self.model_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        self.voice = voice
        self.personal_use = personal_use is True
        self.reason = "not_prepared"
        self.error_class = None
        self._model = None
        self._lock = threading.RLock()
        self._ack_cache = OrderedDict()

    def available(self) -> bool:
        if not self.personal_use:
            self.reason = "personal_use_required"
            return False
        if self.voice not in VOICES:
            self.reason = "voice_invalid"
            return False
        if self._model is not None:
            return True
        if not _dependencies_ready():
            self.reason = "dependencies_missing"
            return False
        if not self.model_dir.is_dir():
            self.reason = "model_missing"
            return False
        if not verified_model(self.model_dir):
            self.reason = "model_invalid"
            return False
        return True

    @staticmethod
    def _valid_rate(rate) -> bool:
        return type(rate) is int and 90 <= rate <= 240

    def _ensure_loaded(self) -> bool:
        if not self.available():
            return False
        if self._model is None:
            # verified_model ran before the first torch.package deserialization.
            self._model = _load_backend(self.model_dir)
        return True

    def prepare(self, rate=185) -> bool:
        if not self._valid_rate(rate):
            self.reason, self.error_class = "rate_invalid", None
            return False
        try:
            with self._lock:
                if not self._ensure_loaded():
                    return False
                self.synthesize(ACKNOWLEDGMENT, rate)
                self.reason, self.error_class = "ready", None
                return True
        except Exception as error:
            self.reason, self.error_class = "backend_unavailable", type(error).__name__
            self._model = None
            self._ack_cache.clear()
            return False

    def synthesize(self, text: str, rate=185, voice=None) -> tuple[bytes, int]:
        if (not isinstance(text, str) or not text.strip() or len(text) > 700
                or "\x00" in text or re.search(r"<[^>]*>", text)):
            raise ExpressiveSpeechUnavailable("text_invalid")
        if not self._valid_rate(rate):
            raise ExpressiveSpeechUnavailable("rate_invalid")
        speaker = self.voice if voice is None else voice
        if speaker not in VOICES:
            raise ExpressiveSpeechUnavailable("voice_invalid")
        text = " ".join(text.split())
        acknowledgment = text.casefold().rstrip(".!?") in {"да, сэр", "да сэр"}
        tier = _speed_tier(rate)
        key = speaker, tier
        try:
            with self._lock:
                if not self._ensure_loaded():
                    raise ExpressiveSpeechUnavailable(self.reason)
                if acknowledgment and key in self._ack_cache:
                    self._ack_cache.move_to_end(key)
                    return self._ack_cache[key]
                result = _render(self._model, ACKNOWLEDGMENT if acknowledgment else text, speaker, tier)
                pcm, sample_rate = result
                if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2
                        or len(pcm) > SAMPLE_RATE * 2 * 60 or sample_rate != SAMPLE_RATE):
                    raise ExpressiveSpeechUnavailable("audio_invalid")
                if acknowledgment:
                    self._ack_cache[key] = result
                    while len(self._ack_cache) > len(VOICES) * 5:
                        self._ack_cache.popitem(last=False)
                return result
        except ExpressiveSpeechUnavailable:
            raise
        except Exception as error:
            self.error_class = type(error).__name__
            raise ExpressiveSpeechUnavailable("synthesis_failed") from None
