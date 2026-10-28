"""Selected XTTS config reaches streaming without replacing the accepted voice.

All upstream tensors/model loading are fakes; no Torch, CUDA, reference files,
audio device, model download or provider is used.
"""
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from desktop import clone_speech
from desktop.clone_speech_worker import ModelEngine

pytestmark = pytest.mark.desktop


@pytest.mark.parametrize("penalty", [5.0, 2.25], ids=["pinned-model-five", "reads-selected-config"])
@pytest.mark.parametrize("text,language,rate", [
    ("Сэр, начнём спокойно. Я на связи.", "ru", 195),
    ("Good morning, sir. Your local assistant is ready.", "en", 185),
])
def test_stream_uses_selected_model_config_and_retains_voice_conditioning_and_timing(
        monkeypatch, tmp_path, penalty, text, language, rate):
    records = SimpleNamespace(conditioning=[], speaker=[], inference=[], references=[], checkpoints=[])
    accepted_reference = b"\x10\x00\xf0\xff" * 8
    accepted_embedding, accepted_conditioning = object(), object()

    class Tensor:
        ndim = 1

        def __init__(self, pcm=b"\x01\x00\x02\x00"):
            self.pcm = pcm

        def unsqueeze(self, _dimension):
            return self

        def to(self, _device_or_dtype):
            return self

        def numel(self):
            return len(self.pcm) // 2

        def detach(self):
            return self

        def reshape(self, _shape):
            return self

        def cpu(self):
            return self

        def clamp(self, _minimum, _maximum):
            return self

        def mul(self, _scale):
            return self

        def round(self):
            return self

        def numpy(self):
            return self

        def astype(self, _dtype):
            return self

        def tobytes(self):
            return self.pcm

        def __truediv__(self, _normalizer):
            return self

    class Configuration:
        repetition_penalty = penalty

        def load_json(self, path):
            assert Path(path) == tmp_path / "model" / "config.json"

    class Backend:
        def __init__(self, config):
            self.config = config

        def load_checkpoint(self, config, **settings):
            records.checkpoints.append((config, settings))

        def to(self, device):
            assert device == "cuda"  # A literal setting; this fake opens no device.

        def eval(self):
            return None  # Match upstream Coqui, rather than nn.Module.eval().

        def get_speaker_embedding(self, audio, sample_rate):
            records.speaker.append((audio, sample_rate))
            return accepted_embedding

        def get_gpt_cond_latents(self, audio, sample_rate, **settings):
            records.conditioning.append((audio, sample_rate, settings))
            return accepted_conditioning

        def inference_stream(self, original_text, selected_language, conditioning, embedding, **settings):
            records.inference.append((original_text, selected_language, conditioning, embedding, settings))
            yield Tensor()
            yield Tensor(b"\x03\x00\x04\x00")

    def read_reference(path, **bounds):
        records.references.append((path, bounds))
        return accepted_reference

    def decode_reference(data, *, dtype):
        assert data == accepted_reference and dtype == "<i2"
        return Tensor()

    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True), set_num_threads=lambda _count: None,
        from_numpy=lambda value: value, inference_mode=nullcontext, Tensor=Tensor, int16="int16",
        isfinite=lambda _value: SimpleNamespace(all=lambda: SimpleNamespace(item=lambda: True)))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(frombuffer=decode_reference))
    monkeypatch.setitem(sys.modules, "TTS.tts.configs.xtts_config", SimpleNamespace(XttsConfig=Configuration))
    monkeypatch.setitem(sys.modules, "TTS.tts.models.xtts", SimpleNamespace(Xtts=Backend, _legacy_safe_globals=nullcontext))
    monkeypatch.setattr(clone_speech, "verified_model", lambda _path: True)
    monkeypatch.setattr(clone_speech, "read_pcm", read_reference)
    manifest = {"model_dir": str(tmp_path / "model"), "reference_wav": str(tmp_path / "accepted-reference.wav"),
                "device": "cuda"}

    engine = ModelEngine(manifest, clone_speech)
    assert list(engine.stream(text, rate, language, lambda: False)) == [b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"]

    assert records.references == [(manifest["reference_wav"], {"minimum": 3, "maximum": 30})]
    assert len(records.speaker) == len(records.conditioning) == len(records.inference) == 1
    assert records.speaker[0][0] is records.conditioning[0][0]
    assert records.speaker[0][1] == records.conditioning[0][1] == 24000
    assert records.conditioning[0][2] == {"length": 12, "chunk_length": 6}
    original_text, selected_language, conditioning, embedding, settings = records.inference[0]
    assert original_text == text and selected_language == language
    assert conditioning is accepted_conditioning and embedding is accepted_embedding
    assert settings == {"stream_chunk_size": 20, "speed": rate / 185,
                        "repetition_penalty": float(penalty), "enable_text_splitting": True}
    # No competing temperature/pitch/overlap preset silently changes the voice.
    assert type(settings["repetition_penalty"]) is float
    assert records.checkpoints[0][1] == {"checkpoint_dir": manifest["model_dir"], "use_deepspeed": False, "eval": True}
