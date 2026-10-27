"""Owned isolated XTTS helper. Stdio is bounded speech data, never commands.

Run only through CloneSpeech after source/runtime verification. No downloader,
server, cloud API, arbitrary file selector or OS action exists in this helper.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
import importlib.util
import json
from pathlib import Path
import queue
import sys
import threading
import time


def _load_adapter():
    # -I deliberately excludes PYTHONPATH and the caller's working directory.
    path = Path(__file__).resolve().with_name("clone_speech.py")
    spec = importlib.util.spec_from_file_location("jarvis_owned_clone_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelEngine:
    """Public Coqui model API, reference decoded using bounded stdlib WAV IO.

    Explicit PCM loading avoids importing FFmpeg/TorchCodec just to decode an
    already converted mono reference. Speaker conditioning stays warm in RAM.
    """

    def __init__(self, manifest, adapter):
        import torch
        import numpy as np
        from TTS.tts.configs.xtts_config import XttsConfig
        from TTS.tts.models.xtts import Xtts, _legacy_safe_globals

        if not adapter.verified_model(manifest["model_dir"]):
            raise adapter.CloneSpeechUnavailable("model_invalid")
        self.torch, self.adapter = torch, adapter
        if manifest["device"] == "cuda" and not torch.cuda.is_available():
            raise adapter.CloneSpeechUnavailable("cuda_unavailable")
        torch.set_num_threads(4)
        config = XttsConfig()
        config.load_json(str(Path(manifest["model_dir"]) / "config.json"))
        class ReadOnlyXtts(Xtts):
            def _load_checkpoint(self, model_path):
                # Upstream 0.28 migrates legacy config and overwrites the
                # checkpoint. Keep official pinned bytes immutable instead.
                # Only known config classes are allowlisted for weights-only
                # loading; the separately pinned JSON supplies our config.
                with _legacy_safe_globals():
                    checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)
                if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("model"), dict):
                    raise adapter.CloneSpeechUnavailable("model_invalid")
                return checkpoint["model"]

        model = ReadOnlyXtts(config)
        model.load_checkpoint(config, checkpoint_dir=manifest["model_dir"], use_deepspeed=False, eval=True)
        model.to(manifest["device"])
        model.eval()
        # Coqui's eval override returns None, unlike nn.Module.eval().
        self.model = model
        reference = adapter.read_pcm(manifest["reference_wav"], minimum=3, maximum=30)
        samples = np.frombuffer(reference, dtype="<i2").astype("float32") / 32768.0
        audio = torch.from_numpy(samples).unsqueeze(0).to(manifest["device"])
        with torch.inference_mode():
            self.embedding = model.get_speaker_embedding(audio, adapter.SAMPLE_RATE)
            self.conditioning = model.get_gpt_cond_latents(audio, adapter.SAMPLE_RATE, length=12, chunk_length=6)

    def stream(self, text, rate, language, cancelled):
        torch, adapter = self.torch, self.adapter
        text, rate, language = adapter.validate_request(text, rate, language)
        started, total = time.monotonic(), 0
        with torch.inference_mode():
            chunks = self.model.inference_stream(text, language, self.conditioning, self.embedding,
                                                  stream_chunk_size=20, speed=rate / 185,
                                                  enable_text_splitting=True)
            for chunk in chunks:
                if cancelled():
                    return
                if (not isinstance(chunk, torch.Tensor) or chunk.ndim not in {1, 2}
                        or chunk.ndim == 2 and chunk.shape[0] != 1
                        or not 0 < chunk.numel() <= adapter.MAX_CHUNK_BYTES // 2
                        or not torch.isfinite(chunk).all().item()):
                    raise adapter.CloneSpeechUnavailable("audio_invalid")
                data = (chunk.detach().reshape(-1).cpu().clamp(-1, 1).mul(32767).round().to(torch.int16)
                        .numpy().astype("<i2").tobytes())
                total += len(data)
                if total > adapter.MAX_PCM_BYTES or time.monotonic() - started > 60:
                    raise adapter.CloneSpeechUnavailable("audio_limit")
                if cancelled():
                    return
                yield data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    adapter = _load_adapter()
    output, output_lock = sys.stdout.buffer, threading.Lock()
    commands, closed, cancelled, known_requests = queue.Queue(maxsize=4), threading.Event(), set(), set()
    state_lock = threading.Lock()

    def emit(value):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(body) > adapter.MAX_FRAME_BYTES:
            raise adapter.CloneSpeechUnavailable("frame_limit")
        with output_lock:
            output.write(body)
            output.flush()

    def read_commands():
        try:
            while not closed.is_set():
                raw = sys.stdin.buffer.readline(8193)
                if not raw or len(raw) > 8192 or not raw.endswith(b"\n"):
                    break
                value = json.loads(raw)
                if (not isinstance(value, dict) or type(value.get("id")) is not int
                        or not 1 <= value["id"] <= 1000000000):
                    break
                if value.get("type") == "cancel" and set(value) == {"type", "id"}:
                    with state_lock:
                        if value["id"] in known_requests:
                            cancelled.add(value["id"])
                    continue
                if value.get("type") != "synthesize" or set(value) != {"type", "id", "text", "rate", "language"}:
                    break
                adapter.validate_request(value["text"], value["rate"], value["language"])
                with state_lock:
                    if value["id"] in known_requests or len(known_requests) >= 5:
                        break
                    known_requests.add(value["id"])
                try:
                    commands.put(value, timeout=0.1)
                except queue.Full:
                    break
        except Exception:
            pass
        closed.set()

    try:
        with redirect_stdout(sys.stderr):
            manifest = adapter.load_manifest(args.manifest)
            if manifest.get("site_packages"):
                # Use only the pinned venv's installed dependencies. -I keeps
                # environment/user paths out; no .pth startup code is run.
                sys.path[:] = [manifest["site_packages"]] + [entry for entry in sys.path
                    if "site-packages" not in Path(entry).parts]
            engine = ModelEngine(manifest, adapter)
        # Initialize native CUDA/BLAS extensions before starting a Python IO
        # thread. The parent can terminate its own job during cold preparation;
        # no synthesis request is sent before the ready frame.
        threading.Thread(target=read_commands, name="jarvis-clone-input", daemon=True).start()
        if closed.is_set():
            return
        emit({"type": "ready", "rate": adapter.SAMPLE_RATE})
        while not closed.is_set():
            try:
                request = commands.get(timeout=0.1)
            except queue.Empty:
                continue
            request_id = request["id"]

            def is_cancelled():
                with state_lock:
                    return closed.is_set() or request_id in cancelled

            try:
                with redirect_stdout(sys.stderr):
                    for pcm in engine.stream(request["text"], request["rate"], request["language"], is_cancelled):
                        if is_cancelled():
                            break
                        emit({"type": "audio", "id": request_id, "rate": adapter.SAMPLE_RATE,
                              "pcm": base64.b64encode(pcm).decode("ascii")})
                emit({"type": "done", "id": request_id})
            except Exception as error:
                emit({"type": "error", "id": request_id, "error_class": type(error).__name__[:80]})
            finally:
                with state_lock:
                    cancelled.discard(request_id)
                    known_requests.discard(request_id)
    except Exception as error:
        try:
            reason = str(error) if isinstance(error, adapter.CloneSpeechUnavailable) and str(error) in adapter.STARTUP_REASONS else "helper_unavailable"
            emit({"type": "error", "id": 0, "error_class": type(error).__name__[:80], "reason": reason})
        except Exception:
            pass
    finally:
        closed.set()


if __name__ == "__main__":
    main()
