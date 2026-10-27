"""Optional offline Piper speech. Normal execution never downloads anything.

Piper 1.8 is GPL-3.0; this Dmitri voice's published dataset is CC0. Retain the
upstream licenses and review distribution obligations before a commercial build.
No actor voice or reference recording is used to create this voice.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import threading

MODEL_NAME = "ru_RU-dmitri-medium"
MODEL_REVISION = "5041a7a49ae5b3a0f30e6837769eb03fdbb231bd"
MODEL_BASE_URL = f"https://huggingface.co/rhasspy/piper-voices/resolve/{MODEL_REVISION}/ru/ru_RU/dmitri/medium/"
MODEL_FILES = {
    MODEL_NAME + ".onnx": (63201294, "f073356ebc4bd0f80c5af58df2953a5988bd5bdab1eb38635ce960b071fbefcb"),
    MODEL_NAME + ".onnx.json": (4824, "667ef3117bc642c2892dff7690d8bdc8ca4228aeaa783b2dc1416df632855e0d"),
    "MODEL_CARD": (277, "6d59c756776d57860232cea6484e1b2ea1fc1c8d2c3446ef246f706fd9875821"),
}
ACKNOWLEDGMENT = "Да, сэр"


class NeuralSpeechUnavailable(RuntimeError):
    """A fixed reason label, never private text, paths or backend diagnostics."""


def default_model_dir() -> Path:
    return Path.home() / "JarvisData" / "publicmodels" / "piper-ru-dmitri-medium"


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
    return all(importlib.util.find_spec(name) is not None for name in ("piper", "onnxruntime"))


def _load_backend(path: Path):
    import onnxruntime
    from piper import PiperVoice, SynthesisConfig
    from piper.config import PiperConfig
    from piper.voice import ESPEAK_DATA_DIR

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    config = json.loads((path / (MODEL_NAME + ".onnx.json")).read_text(encoding="utf-8"))
    voice = PiperVoice(config=PiperConfig.from_dict(config),
        session=onnxruntime.InferenceSession(str(path / (MODEL_NAME + ".onnx")),
            sess_options=options, providers=["CPUExecutionProvider"]),
        espeak_data_dir=ESPEAK_DATA_DIR, download_dir=path)
    return voice, SynthesisConfig


class NeuralSpeech:
    """PCM is signed 16-bit little-endian mono; rate is a relative speed control.

    165 is the model's native duration; 195 shortens duration to 165/195,
    preserving the sample rate. Initialization belongs on a worker thread.
    Only the fixed wake acknowledgment is cached, at at most eight rates.
    No arbitrary user speech is saved to disk or retained in the cache.
    """

    def __init__(self, model_dir=None):
        self.model_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        self.reason = "not_prepared"
        self.error_class = None
        self._voice = self._config = None
        self._lock = threading.RLock()
        self._ack_cache = OrderedDict()

    def available(self) -> bool:
        if self._voice is not None:
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

    def prepare(self, rate=195) -> bool:
        if not isinstance(rate, int) or isinstance(rate, bool) or not 90 <= rate <= 240:
            self.reason, self.error_class = "rate_invalid", None
            return False
        try:
            with self._lock:
                if self._voice is None:
                    if not self.available():
                        return False
                    self._voice, self._config = _load_backend(self.model_dir)
                self.synthesize(ACKNOWLEDGMENT, rate)
                self.reason, self.error_class = "ready", None
                return True
        except Exception as error:
            self.error_class = type(error).__name__
            self.reason = "backend_unavailable"
            self._voice = self._config = None
            return False

    def synthesize(self, text: str, rate=195) -> tuple[bytes, int]:
        if (not isinstance(text, str) or not text.strip() or len(text) > 700
                or "\x00" in text or re.search(r"<[^>]*>", text)):
            raise NeuralSpeechUnavailable("text_invalid")
        if not isinstance(rate, int) or isinstance(rate, bool) or not 90 <= rate <= 240:
            raise NeuralSpeechUnavailable("rate_invalid")
        text = " ".join(text.split())
        acknowledgment = text.casefold().rstrip(".!?") in {"да, сэр", "да сэр"}
        with self._lock:
            if acknowledgment and rate in self._ack_cache:
                self._ack_cache.move_to_end(rate)
                return self._ack_cache[rate]
            if self._voice is None and not self.prepare(rate):
                raise NeuralSpeechUnavailable(self.reason)
            if acknowledgment and rate in self._ack_cache:
                return self._ack_cache[rate]
            try:
                settings = self._config(length_scale=165 / rate, noise_scale=0.667,
                                        noise_w_scale=0.8, normalize_audio=True)
                buffers, total, sample_rate = [], 0, None
                for chunk in self._voice.synthesize(text, syn_config=settings):
                    if (chunk.sample_width != 2 or chunk.sample_channels != 1
                            or not 16000 <= chunk.sample_rate <= 48000
                            or sample_rate is not None and sample_rate != chunk.sample_rate):
                        raise NeuralSpeechUnavailable("audio_invalid")
                    sample_rate = chunk.sample_rate
                    block = chunk.audio_int16_bytes
                    if not isinstance(block, bytes) or len(block) % 2:
                        raise NeuralSpeechUnavailable("audio_invalid")
                    total += len(block)
                    if total > 48000 * 2 * 60:
                        raise NeuralSpeechUnavailable("audio_limit")
                    buffers.append(block)
                if not total or sample_rate is None:
                    raise NeuralSpeechUnavailable("audio_empty")
                result = b"".join(buffers), sample_rate
                if acknowledgment:
                    self._ack_cache[rate] = result
                    while len(self._ack_cache) > 8:
                        self._ack_cache.popitem(last=False)
                return result
            except NeuralSpeechUnavailable:
                raise
            except Exception as error:
                self.error_class = type(error).__name__
                raise NeuralSpeechUnavailable("synthesis_failed") from None
