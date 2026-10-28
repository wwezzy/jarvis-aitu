"""Explicit opt-in camera gestures; intent output only, never PC execution.

Frames/landmarks are processed transiently and never recorded or uploaded by
this adapter. MediaPipe's current SDK terms disclose possible diagnostic/
usage metadata; do not advertise the dependency as guaranteed zero-network.
Official API: https://developers.google.com/edge/mediapipe/solutions/vision/gesture_recognizer/python
Privacy/terms: https://developers.google.com/edge/mediapipe/legal/tos
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler


MODEL_NAME = "gesture_recognizer.task"
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task"
MODEL_SIZE = 8373440
MODEL_SHA256 = "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482"
LICENSE_URL = "https://raw.githubusercontent.com/google-ai-edge/mediapipe/991b5e10e520fe0cc0c515b014681b9023e43b21/LICENSE"
LICENSE_SIZE = 12331
LICENSE_SHA256 = "8707eef0533987efc5b155d64761eeb6e20793f50b9bd1a68dad1cf4719d0ed8"
MODEL_NOTICE = """Official MediaPipe Gesture Recognizer float16 v1, Google AI Edge.
Source/package license: Apache-2.0; full upstream LICENSE is preserved here.
Model is fetched separately from its publisher, not bundled with personal data.
Task guide: https://developers.google.com/edge/mediapipe/solutions/vision/gesture_recognizer
Model card: https://storage.googleapis.com/mediapipe-assets/gesture_recognizer/model_card_hand_gesture_classification_with_faireness_2022.pdf
Use/SDK terms: https://developers.google.com/edge/mediapipe/legal/tos
MediaPipe processes images on-device; SDK usage/performance metadata may be sent.
This adapter never stores frames, landmarks, screenshots or camera recordings.
No identity recognition or claim of camera-owner identification is made.
Keep upstream model terms and dependency notices when redistributing.
"""
INTENTS = {"Thumb_Up": "volume_up", "Thumb_Down": "volume_down", "Open_Palm": "minimize"}
MAX_FPS = 6
MAX_FRAME_AGE = 0.6
METADATA_POLICY = "sdk_metrics_possible"


def default_model_dir():
    return Path.home() / "JarvisData" / "publicmodels" / "mediapipe-gesture-recognizer-v1"


def _matches(path, size, digest):
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == digest
    except OSError:
        return False


def model_ready(path):
    root = Path(path)
    return (_matches(root / MODEL_NAME, MODEL_SIZE, MODEL_SHA256)
            and _matches(root / "LICENSE", LICENSE_SIZE, LICENSE_SHA256)
            and (root / "NOTICE.txt").is_file())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def install_model(target_dir=None, progress_callback=None, *, opener=None):
    """Explicit pinned public download; never opens a camera or imports ML."""
    target = Path(target_dir) if target_dir is not None else default_model_dir()
    if model_ready(target):
        return target
    if target.exists():
        raise FileExistsError("Existing incomplete model directory is preserved")
    target.parent.mkdir(parents=True, exist_ok=True)
    client = opener or build_opener(ProxyHandler({}), _NoRedirect())
    with tempfile.TemporaryDirectory(prefix=".jarvis-gesture-", dir=target.parent) as temporary:
        stage = Path(temporary) / "model"
        stage.mkdir()
        for name, url, size, digest in ((MODEL_NAME, MODEL_URL, MODEL_SIZE, MODEL_SHA256),
                                        ("LICENSE", LICENSE_URL, LICENSE_SIZE, LICENSE_SHA256)):
            deadline, total, hasher = time.monotonic() + 120, 0, hashlib.sha256()
            with client.open(url, timeout=10) as response, (stage / name).open("wb") as output:
                if response.geturl() != url:
                    raise ValueError("Model redirects are refused")
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Model download exceeded its limit")
                    chunk = response.read(min(65536, size + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size:
                        raise ValueError("Model download exceeded its size")
                    hasher.update(chunk)
                    output.write(chunk)
                    if callable(progress_callback):
                        progress_callback("Загрузка модели жестов…" if name == MODEL_NAME else "Проверяю лицензию…")
            if total != size or hasher.hexdigest() != digest:
                raise ValueError("Gesture asset checksum mismatch")
        (stage / "NOTICE.txt").write_text(MODEL_NOTICE, encoding="utf-8")
        if not model_ready(stage):
            raise ValueError("Gesture model is incomplete")
        stage.replace(target)
    if callable(progress_callback):
        progress_callback("Модель жестов установлена. Камера выключена.")
    return target


class GestureGate:
    """Stable one-hand hold, initial/repeated neutral, cooldown and no replay."""
    def __init__(self, *, hold_seconds=0.7, cooldown=2.0, neutral_seconds=0.35, maximum_gap=0.6):
        values = (hold_seconds, cooldown, neutral_seconds, maximum_gap)
        if any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Finite positive gesture timing is required")
        self.hold, self.cooldown, self.neutral, self.maximum_gap = values
        self.reset()

    def reset(self):
        self._last = self._neutral_since = self._since = self._label = None
        self._armed, self._samples, self._cool_until = False, 0, 0.0

    def feed(self, label, score, hand_count, now):
        if type(now) not in {int, float} or not math.isfinite(now):
            self.reset()
            return None
        if self._last is not None and (now <= self._last or now - self._last > self.maximum_gap):
            self.reset()
        self._last = now
        valid_score = type(score) in {int, float} and math.isfinite(score) and 0 <= score <= 1
        neutral = type(hand_count) is int and (hand_count == 0 or hand_count == 1 and label == "None" and valid_score and score >= 0.8)
        if neutral:
            self._since = self._label = None
            self._samples = 0
            if self._neutral_since is None:
                self._neutral_since = now
            if now - self._neutral_since >= self.neutral:
                self._armed = True
            return None
        self._neutral_since = None
        if (type(hand_count) is not int or hand_count != 1 or not valid_score or score < 0.8
                or not isinstance(label, str) or label not in INTENTS or not self._armed or now < self._cool_until):
            self._since = self._label = None
            self._samples = 0
            return None
        if label != self._label:
            self._label, self._since, self._samples = label, now, 1
            return None
        self._samples += 1
        if self._samples >= 4 and now - self._since >= self.hold:
            intent = INTENTS[label]
            self._armed, self._cool_until = False, now + self.cooldown
            self._since = self._label = None
            self._samples = 0
            return intent
        return None


def _dependencies_ready():
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("mediapipe", "cv2", "numpy"))
    except (ImportError, ValueError):
        return False


class _Recognizer:
    def __init__(self, model):
        import cv2
        import mediapipe as mp
        self.cv2, self.mp = cv2, mp
        options = mp.tasks.vision.GestureRecognizerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model), delegate=mp.tasks.BaseOptions.Delegate.CPU),
            running_mode=mp.tasks.vision.RunningMode.VIDEO, num_hands=2,
            min_hand_detection_confidence=0.7, min_hand_presence_confidence=0.7, min_tracking_confidence=0.7,
            # The pinned Python 1.2.0 API spells this singular, unlike the
            # configuration table in the general online guide.
            canned_gesture_classifier_options=mp.tasks.components.processors.ClassifierOptions(
                score_threshold=0.8, max_results=1, category_allowlist=[*INTENTS, "None"]))
        self.recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(options)

    def recognize(self, frame, timestamp):
        if (getattr(frame, "ndim", None) != 3 or len(frame.shape) != 3 or frame.shape[2] != 3
                or str(frame.dtype) != "uint8" or not all(0 < size <= 4096 for size in frame.shape[:2])):
            raise ValueError("Invalid camera frame")
        height, width = frame.shape[:2]
        scale = min(1.0, 640 / width, 480 / height)
        if scale < 1:
            frame = self.cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=self.cv2.INTER_AREA)
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        result = self.recognizer.recognize_for_video(image, timestamp)
        count = len(result.hand_landmarks)
        if count != 1:
            return None, 0.0, count
        if len(result.gestures) != 1 or len(result.gestures[0]) != 1:
            return None, 0.0, -1
        category = result.gestures[0][0]
        return category.category_name, category.score, count

    def close(self):
        self.recognizer.close()


def _open_camera(index):
    import cv2
    capture = cv2.VideoCapture(index, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError("camera_unavailable")
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    capture.set(cv2.CAP_PROP_FPS, MAX_FPS)
    return capture


class GestureService:
    def __init__(self, on_intent, on_state, *, owner_gate, model_dir=None, camera_index=0,
                 capture_factory=None, recognizer_factory=None, clock=None):
        if type(camera_index) is not int or not 0 <= camera_index <= 9:
            raise ValueError("A local camera index is required")
        self.on_intent, self.on_state, self.owner_gate = on_intent, on_state, owner_gate
        self.model_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        self.camera_index = camera_index
        self._capture_factory, self._recognizer_factory = capture_factory or _open_camera, recognizer_factory or _Recognizer
        self._clock = clock or time.monotonic
        self._stop, self._lock = threading.Event(), threading.Lock()
        self._thread = None
        self.reason, self._active = "disabled", False

    def _emit(self, callback, value):
        try:
            callback(value)
        except Exception:
            pass

    def _allowed(self, intent=None):
        try:
            return callable(self.owner_gate) and self.owner_gate(intent) is True
        except Exception:
            return False

    def available(self):
        dependencies, model = _dependencies_ready(), model_ready(self.model_dir)
        running = bool(self._thread and self._thread.is_alive())
        return {"ready": dependencies and model and callable(self.owner_gate), "dependencies": dependencies,
                "model": model, "running": running, "active": self._active and not self._stop.is_set(),
                "reason": self.reason, "metadata_policy": METADATA_POLICY}

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return not self._stop.is_set()
            if not self._allowed():
                self.reason = "owner_required"
                self._emit(self.on_state, "Камера выключена: подтвердите доступ владельца.")
                return False
            report = self.available()
            if not report["ready"]:
                self.reason = "dependencies_missing" if not report["dependencies"] else "model_missing"
                self._emit(self.on_state, "Камера выключена: установите компоненты и модель жестов.")
                return False
            self._stop.clear()
            self.reason = "starting"
            self._thread = threading.Thread(target=self._run, name="JarvisGestures", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        self._stop.set()  # Immediately prevent further intent dispatch.
        self._active = False
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        pending = bool(thread and thread.is_alive())
        self.reason = "stopping" if pending else "disabled"
        self._emit(self.on_state, "Останавливаю камеру…" if pending else "Камера выключена.")
        return not pending

    close = stop

    def _run(self):
        capture = recognizer = None
        failure = False
        gate, timestamp = GestureGate(), -1
        try:
            recognizer = self._recognizer_factory(self.model_dir / MODEL_NAME)
            if self._stop.is_set() or not self._allowed():
                return
            capture = self._capture_factory(self.camera_index)
            if self._stop.is_set() or not self._allowed():
                return
            self._active, self.reason = True, "ready"
            self._emit(self.on_state, "Камера включена. Сначала уберите руки, затем удерживайте жест.")
            while not self._stop.is_set():
                if not self._allowed():
                    self.reason = "owner_required"
                    self._emit(self.on_state, "Доступ владельца истёк. Камера выключается.")
                    break
                started = self._clock()
                ok, frame = capture.read()
                if not ok:
                    raise RuntimeError("camera_unavailable")
                if self._stop.is_set():
                    break
                timestamp = max(timestamp + 1, int(self._clock() * 1000))
                label, score, hands = recognizer.recognize(frame, timestamp)
                frame = None  # No frame/history queue or serialization.
                now = self._clock()
                if now - started > MAX_FRAME_AGE or now < started:
                    gate.reset()
                else:
                    intent = gate.feed(label, score, hands, now)
                    if intent in INTENTS.values() and not self._stop.is_set() and self._allowed(intent):
                        self._emit(self.on_intent, intent)
                self._stop.wait(max(0, 1 / MAX_FPS - (self._clock() - started)))
        except Exception:
            failure, self.reason = True, "camera_unavailable"
            self._emit(self.on_state, "Камера или распознавание жестов недоступны. Остальные функции работают.")
        finally:
            self._active = False
            for resource, method in ((capture, "release"), (recognizer, "close")):
                try:
                    if resource is not None:
                        getattr(resource, method)()
                except Exception:
                    pass
            if self._stop.is_set() or not failure and self.reason != "owner_required":
                self.reason = "disabled"
            self._emit(self.on_state, "Камера выключена.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit optional gesture model setup; camera is never opened here")
    parser.add_argument("--install-model", action="store_true")
    parser.add_argument("--model-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.install_model:
        parser.print_help()
        return 0
    install_model(args.model_dir)
    print("Gesture model installed. Camera remains disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
