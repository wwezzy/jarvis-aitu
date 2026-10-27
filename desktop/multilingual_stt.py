"""Optional, bounded RU/EN transcription after an externally confirmed wake.

Uses the official Whisper-small CTranslate2 conversion, NumPy log-mel features
and Silero VAD. It does not import PyAV/faster_whisper, capture a microphone,
download anything, retain transcripts or authorize any desktop action.
Feature conventions follow OpenAI Whisper (MIT); VAD input conventions follow
SYSTRAN faster-whisper (MIT). Exact source licenses accompany pinned assets.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import importlib.util
import math
from pathlib import Path
import sys
import threading
import zlib


MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
WHISPER_REVISION = "86098128c0b4f24f0e2aa2994de830614b474227"
FASTER_REVISION = "65882eee9f5cdbeeb2d877f1131d48cf241b327d"
VAD_REVISION = "066409f3d9db45a30d21dfcbf9147826ffdd7052"
_BASE = f"https://huggingface.co/Systran/faster-whisper-small/resolve/{MODEL_REVISION}/"
MODEL_FILES = {
    "config.json": (2370, "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828"),
    "model.bin": (483546902, "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"),
    "tokenizer.json": (2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
    "vocabulary.txt": (459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
    "README.md": (1998, "329373481008c7c38654aff8ecdcf0163c211557cc7ba8e2ef6f2f84b4f75ec8"),
    "silero_vad_v6.onnx": (1245151, "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2"),
    "mel_filters.npz": (4271, "7450ae70723a5ef9d341e3cee628c7cb0177f36ce42c44b7ed2bf3325f0f6d4c"),
    "LICENSE_WHISPER.txt": (1063, "b5d65a59060e68c4ff940e1eddfa6f94b2d68fdf58ed7f4dd57721c997e35e9d"),
    "LICENSE_FASTER_WHISPER.txt": (1064, "af6798135e729f8aa6c853936d037dfdea449734d26b8ea6a89805fca758c0d5"),
    "LICENSE_SILERO_VAD.txt": (1075, "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"),
}
MODEL_URLS = {name: _BASE + name for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "README.md")}
MODEL_URLS.update({
    "silero_vad_v6.onnx": f"https://raw.githubusercontent.com/SYSTRAN/faster-whisper/{FASTER_REVISION}/faster_whisper/assets/silero_vad_v6.onnx",
    "mel_filters.npz": f"https://raw.githubusercontent.com/openai/whisper/{WHISPER_REVISION}/whisper/assets/mel_filters.npz",
    "LICENSE_WHISPER.txt": f"https://raw.githubusercontent.com/openai/whisper/{WHISPER_REVISION}/LICENSE",
    "LICENSE_FASTER_WHISPER.txt": f"https://raw.githubusercontent.com/SYSTRAN/faster-whisper/{FASTER_REVISION}/LICENSE",
    "LICENSE_SILERO_VAD.txt": f"https://raw.githubusercontent.com/snakers4/silero-vad/{VAD_REVISION}/LICENSE",
})


class MultilingualSTTUnavailable(RuntimeError):
    """A fixed reason label; no private audio, transcription, path or stack."""


@dataclass(frozen=True)
class _Decoded:
    text: str
    language: str
    language_probability: float
    avg_logprob: float
    no_speech_probability: float
    speech_seconds: float


@dataclass
class _Backend:
    model: object
    tokenizer: object
    vad: object
    filters: object


def default_model_dir() -> Path:
    return Path.home() / "JarvisData" / "publicmodels" / "faster-whisper-small-ru-en"


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


def _dependencies_ready():
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("numpy", "ctranslate2", "tokenizers", "onnxruntime"))
    except (ImportError, ValueError):
        return False


def _pcm_samples(pcm, sample_rate):
    if (type(sample_rate) is not int or not 8000 <= sample_rate <= 48000
            or not isinstance(pcm, bytes) or len(pcm) % 2
            or not sample_rate // 4 <= len(pcm) // 2 <= sample_rate * 12):
        raise MultilingualSTTUnavailable("audio_invalid")
    values = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    mean = sum(values) / len(values)
    variance = max(0, sum(value * value for value in values) / len(values) - mean * mean)
    if math.sqrt(variance) / 32768 < 0.0015:
        raise MultilingualSTTUnavailable("silence")
    frame = sample_rate // 50
    active = sum(math.sqrt(sum((value - mean) ** 2 for value in values[start:start + frame]) / frame) / 32768 >= 0.003
                 for start in range(0, len(values) - frame + 1, frame))
    if active < 6:
        raise MultilingualSTTUnavailable("no_speech")
    if sum(abs(value) >= 32700 for value in values) > len(values) / 5:
        raise MultilingualSTTUnavailable("audio_clipped")
    return values


def _load_backend(path):
    import ctranslate2
    import numpy as np
    import onnxruntime
    from tokenizers import Tokenizer

    model = ctranslate2.models.Whisper(str(path), device="cpu", compute_type="int8",
                                      intra_threads=4, inter_threads=1, max_queued_batches=1)
    if not model.is_multilingual or model.n_mels != 80:
        raise ValueError("model_metadata_invalid")
    tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
    if any(tokenizer.token_to_id(token) is None for token in
           ("<|startoftranscript|>", "<|ru|>", "<|en|>", "<|transcribe|>", "<|notimestamps|>")):
        raise ValueError("tokenizer_invalid")
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 4
    vad = onnxruntime.InferenceSession(str(path / "silero_vad_v6.onnx"), providers=["CPUExecutionProvider"], sess_options=options)
    with np.load(path / "mel_filters.npz", allow_pickle=False) as archive:
        filters = archive["mel_80"].astype("float32")
    if filters.shape != (80, 201) or not np.isfinite(filters).all():
        raise ValueError("filters_invalid")
    return _Backend(model, tokenizer, vad, filters)


def _audio_16k(values, sample_rate):
    import numpy as np

    waveform = np.asarray(values, dtype="float32") / 32768
    if sample_rate != 16000:
        if sample_rate > 16000:
            # A short windowed-sinc low-pass prevents high-frequency aliasing.
            taps = (int(16 * sample_rate / 16000) | 1)
            n = np.arange(taps) - (taps - 1) / 2
            cutoff = 0.47 * 16000 / sample_rate
            kernel = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hanning(taps)
            waveform = np.convolve(waveform, kernel / kernel.sum(), mode="same")
        target = round(len(values) * 16000 / sample_rate)
        waveform = np.interp(np.arange(target) * sample_rate / 16000, np.arange(len(waveform)), waveform)
    return waveform.astype("float32")


def _speech_window(vad, waveform):
    import numpy as np

    padding = (-len(waveform)) % 512
    chunks = np.pad(waveform, (0, padding)).reshape(-1, 512)
    context = np.concatenate((np.zeros((1, 64), dtype="float32"), chunks[:-1, -64:]), axis=0)
    inputs = np.concatenate((context, chunks), axis=1)
    h = np.zeros((1, 1, 128), dtype="float32")
    output, _, _ = vad.run(None, {"input": inputs, "h": h, "c": h.copy()})
    probabilities = np.asarray(output).reshape(-1)
    if len(probabilities) != len(chunks) or not np.isfinite(probabilities).all():
        raise MultilingualSTTUnavailable("vad_invalid")
    voiced = probabilities >= 0.55
    indices = np.flatnonzero(voiced)
    if len(indices) < 6 or not np.any(np.convolve(voiced.astype(int), np.ones(3, dtype=int), mode="valid") >= 3):
        raise MultilingualSTTUnavailable("no_speech")
    start, end = max(0, int(indices[0]) * 512 - 1280), min(len(waveform), (int(indices[-1]) + 1) * 512 + 1280)
    return waveform[start:end], len(indices) * 512 / 16000


def _features(waveform, filters):
    import numpy as np

    padded = np.pad(waveform, (0, 480000 - len(waveform)))
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(padded, (200, 200), mode="reflect"), 400)[::160]
    spectrum = np.fft.rfft(frames * np.hanning(401)[:-1].astype("float32"), axis=-1).astype("complex64")
    power = np.abs(spectrum[:-1]) ** 2
    log_mel = np.log10(np.maximum(filters @ power.T, 1e-10))
    log_mel = np.maximum(log_mel, log_mel.max() - 8)
    return np.ascontiguousarray(((log_mel + 4) / 4)[None], dtype="float32")


def _decode(backend, values, sample_rate):
    import ctranslate2

    waveform, speech_seconds = _speech_window(backend.vad, _audio_16k(values, sample_rate))
    features = ctranslate2.StorageView.from_array(_features(waveform, backend.filters))
    encoded = backend.model.encode(features)
    language_token, probability = backend.model.detect_language(encoded)[0][0]
    if language_token not in ("<|ru|>", "<|en|>"):
        raise MultilingualSTTUnavailable("language_unsupported")
    if not math.isfinite(probability) or probability < 0.55:
        raise MultilingualSTTUnavailable("low_confidence")
    # Only structural transcription tokens; no command list, prefix, hotwords,
    # arbitrary prompt text, conversation history or translation instruction.
    prompt = [backend.tokenizer.token_to_id(token) for token in
              ("<|startoftranscript|>", language_token, "<|transcribe|>", "<|notimestamps|>")]
    result = backend.model.generate(encoded, [prompt], beam_size=3, max_length=128,
                                    return_scores=True, return_no_speech_prob=True,
                                    suppress_blank=True, sampling_temperature=0)[0]
    tokens = result.sequences_ids[0]
    if not tokens or len(tokens) >= 124:
        raise MultilingualSTTUnavailable("transcript_limit")
    text = backend.tokenizer.decode(tokens, skip_special_tokens=True).strip()
    avg_logprob = result.scores[0] * len(tokens) / (len(tokens) + 1)
    return _Decoded(text, language_token[2:-2], probability, avg_logprob, result.no_speech_prob, speech_seconds)


class MultilingualSTT:
    def __init__(self, model_dir=None):
        self.model_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        self.reason, self.error_class = "not_prepared", None
        self.last_language = self.last_confidence = None
        self._backend = None
        self._lock = threading.RLock()

    def available(self):
        if self._backend is not None:
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

    def prepare(self):
        try:
            with self._lock:
                if self._backend is None:
                    if not self.available():
                        return False
                    self._backend = _load_backend(self.model_dir)
                self.reason, self.error_class = "ready", None
                return True
        except Exception as error:
            self._backend = None
            self.reason, self.error_class = "backend_unavailable", type(error).__name__
            return False

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.last_language = self.last_confidence = None
        try:
            values = _pcm_samples(pcm, sample_rate)
            with self._lock:
                if not self.prepare():
                    raise MultilingualSTTUnavailable(self.reason)
                result = _decode(self._backend, values, sample_rate)
                metrics = (result.language_probability, result.avg_logprob, result.no_speech_probability, result.speech_seconds)
                if (any(not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value) for value in metrics)
                        or not 0.55 <= result.language_probability <= 1 or result.avg_logprob < -0.8
                        or not 0 <= result.no_speech_probability <= 0.4 or not 0.18 <= result.speech_seconds <= 12.1):
                    raise MultilingualSTTUnavailable("low_confidence")
                if result.language not in ("ru", "en"):
                    raise MultilingualSTTUnavailable("language_unsupported")
                text = result.text
                if (not isinstance(text, str) or not text.strip() or len(text) > 500
                        or any(ord(char) < 32 and char not in "\n\t" for char in text)
                        or "<|" in text):
                    raise MultilingualSTTUnavailable("transcript_invalid")
                text = " ".join(text.split())
                raw = text.encode("utf-8")
                if len(raw) / len(zlib.compress(raw)) > 2.2:
                    raise MultilingualSTTUnavailable("repetition_rejected")
                self.last_language, self.last_confidence = result.language, result.language_probability
                self.reason, self.error_class = "ready", None
                return text
        except MultilingualSTTUnavailable as error:
            self.last_language = self.last_confidence = None
            self.reason = str(error)
            raise
        except Exception as error:
            self.last_language = self.last_confidence = None
            self.reason, self.error_class = "transcription_failed", type(error).__name__
            raise MultilingualSTTUnavailable("transcription_failed") from None
