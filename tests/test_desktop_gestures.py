"""Fake camera/model and pure temporal gates; never activate a real camera."""
import hashlib
import io
import math
import sys
import threading
from types import SimpleNamespace

import pytest

from desktop import gestures


def neutral(gate, start=0):
    for offset in (0, 0.2, 0.4):
        assert gate.feed(None, 0, 0, start + offset) is None


def hold(gate, label="Thumb_Up", start=0.6, score=0.95, hands=1):
    return [gate.feed(label, score, hands, start + offset) for offset in (0, 0.2, 0.4, 0.6, 0.8)]


@pytest.mark.parametrize("label,intent", list(gestures.INTENTS.items()))
def test_initial_neutral_stable_hold_and_finite_intents(label, intent):
    gate = gestures.GestureGate()
    assert not any(hold(gate, label, start=0))  # Existing pose when camera starts is not consent.
    neutral(gate, start=1)
    assert hold(gate, label, start=1.6) == [None, None, None, None, intent]


def test_held_pose_or_opposite_pose_never_repeats_without_neutral_and_cooldown():
    gate = gestures.GestureGate()
    neutral(gate)
    assert hold(gate)[-1] == "volume_up"
    for index in range(30):
        assert gate.feed("Thumb_Up" if index < 15 else "Thumb_Down", 0.99, 1, 1.6 + index * 0.2) is None
    neutral(gate, start=7.8)
    assert hold(gate, "Thumb_Down", start=8.4)[-1] == "volume_down"


def test_short_neutral_or_low_confidence_flicker_cannot_rearm_a_held_gesture():
    gate = gestures.GestureGate()
    neutral(gate)
    assert hold(gate)[-1] == "volume_up"
    for index in range(20):
        now = 1.6 + index * 0.2
        label, score, hands = (None, 0, 0) if index % 3 == 0 else ("Thumb_Up", 0.3, 1)
        assert gate.feed(label, score, hands, now) is None
    assert not any(hold(gate, start=5.6))


@pytest.mark.parametrize("label,score,hands", [("Thumb_Up", 0.79, 1), ("Thumb_Up", math.nan, 1),
    ("Thumb_Up", True, 1), ("Thumb_Up", 0.99, 2), ("Thumb_Up", 0.99, True),
    ("shutdown", 1, 1), ("volume_up", 1, 1), (None, 1, 1)])
def test_ambiguous_multihand_unknown_and_malformed_results_never_issue_intent(label, score, hands):
    gate = gestures.GestureGate()
    neutral(gate)
    assert not any(hold(gate, label, score=score, hands=hands))


@pytest.mark.parametrize("next_time", [0.3, 5, math.nan])
def test_clock_reversal_gap_and_invalid_time_require_fresh_neutral(next_time):
    gate = gestures.GestureGate()
    neutral(gate)
    gate.feed("Thumb_Up", 0.99, 1, 0.6)
    assert gate.feed("Thumb_Up", 0.99, 1, next_time) is None
    assert not any(hold(gate, start=6))


@pytest.fixture
def service_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(gestures, "_dependencies_ready", lambda: True)
    monkeypatch.setattr(gestures, "model_ready", lambda _path: True)
    state, intents, calls = [], [], []
    clock = [0.0]
    permission = {"session": True, "intent": True}
    frames = [(None, 0, 0)] * 3 + [("Thumb_Up", 0.99, 1)] * 6
    service_ref = []
    class Camera:
        released = False
        def read(self):
            clock[0] += 0.2
            if frames:
                return True, frames.pop(0)
            service_ref[0]._stop.set()
            return False, None
        def release(self):
            self.released = True
            calls.append("release")
    class Recognizer:
        closed = False
        def recognize(self, frame, timestamp):
            calls.append(("recognize", timestamp))
            return frame
        def close(self):
            self.closed = True
            calls.append("close")
    camera, recognizer = Camera(), Recognizer()
    def open_camera(index):
        calls.append(("open", index))
        return camera
    def owner_gate(intent):
        calls.append(("owner", intent))
        return permission["session"] if intent is None else permission["intent"]
    service = gestures.GestureService(intents.append, state.append, owner_gate=owner_gate,
        model_dir=tmp_path, capture_factory=open_camera, recognizer_factory=lambda _path: recognizer,
        clock=lambda: clock[0])
    service_ref.append(service)
    return service, intents, state, calls, camera, recognizer, permission, clock, frames


def test_constructor_and_available_never_open_camera_or_model(service_setup):
    service, intents, _state, calls, _camera, _recognizer, _permission, _clock, _frames = service_setup
    report = service.available()
    assert report["ready"] and not report["running"] and not report["active"]
    assert report["metadata_policy"] == "sdk_metrics_possible"
    assert not calls and not intents
    assert service.stop() and not service.available()["active"]


@pytest.mark.parametrize("gate", [None, lambda _intent: False])
def test_explicit_start_requires_actual_owner_gate(service_setup, gate):
    service, intents, _states, calls, *_rest = service_setup
    service.owner_gate = gate
    assert not service.start() and service.reason == "owner_required"
    assert not calls and not intents


def test_missing_dependencies_or_model_keep_camera_closed(service_setup, monkeypatch):
    service, intents, _state, calls, *_rest = service_setup
    monkeypatch.setattr(gestures, "_dependencies_ready", lambda: False)
    assert not service.start() and service.reason == "dependencies_missing"
    assert not any(call[0] == "open" for call in calls) and not intents


def test_camera_worker_emits_only_intent_and_releases_owned_resources(service_setup):
    service, intents, _states, calls, camera, recognizer, *_rest = service_setup
    assert service.start()
    service._thread.join(1)
    assert not service._thread.is_alive()
    assert intents == ["volume_up"]
    assert ("owner", None) in calls and ("owner", "volume_up") in calls
    assert camera.released and recognizer.closed
    timestamps = [call[1] for call in calls if isinstance(call, tuple) and call[0] == "recognize"]
    assert all(later > earlier for earlier, later in zip(timestamps, timestamps[1:]))
    assert service.stop()


def test_owner_revoked_at_dispatch_prevents_action_even_after_valid_hold(service_setup):
    service, intents, _states, calls, _camera, _recognizer, permission, *_rest = service_setup
    permission["intent"] = False
    service._run()
    assert not intents and ("owner", "volume_up") in calls


def test_owner_revoked_before_camera_open_never_opens_camera(service_setup):
    service, intents, _states, calls, _camera, _recognizer, permission, *_rest = service_setup
    permission["session"] = False
    service._run()
    assert not any(isinstance(call, tuple) and call[0] == "open" for call in calls)
    assert not intents


def test_stale_slow_frames_do_not_turn_elapsed_time_into_a_stable_hold(service_setup):
    service, intents, _states, _calls, camera, _recognizer, _permission, clock, _frames = service_setup
    read = camera.read
    def slow():
        clock[0] += 1
        return read()
    camera.read = slow
    service._run()
    assert not intents and camera.released


def test_stop_during_inference_discards_late_intent(service_setup):
    service, intents, _states, _calls, _camera, recognizer, *_rest = service_setup
    recognize = recognizer.recognize
    def stop(frame, timestamp):
        if frame[0] == "Thumb_Up":
            service._stop.set()
        return recognize(frame, timestamp)
    recognizer.recognize = stop
    service._run()
    assert not intents


def test_blocked_camera_reports_stopping_until_actual_worker_cleanup(service_setup):
    service, intents, states, _calls, camera, _recognizer, *_rest = service_setup
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(4)
        return False, None
    camera.read = blocked
    assert service.start() and entered.wait(1)
    try:
        assert not service.stop()
        report = service.available()
        assert report["running"] and not report["active"] and report["reason"] == "stopping"
        assert not intents and "Останавливаю" in states[-1]
        assert not service.start()  # Never create another capture while one is stopping.
    finally:
        release.set()
        service._thread.join(1)
    assert camera.released and service.stop()


def test_mediapipe_options_use_cpu_detect_two_hands_and_fixed_canned_allowlist(monkeypatch, tmp_path):
    options, images = [], []
    result = SimpleNamespace(hand_landmarks=[object()], gestures=[[SimpleNamespace(category_name="Open_Palm", score=0.98)]])
    task = SimpleNamespace(recognize_for_video=lambda image, timestamp: result, close=lambda: None)
    class Base:
        Delegate = SimpleNamespace(CPU="CPU")
        def __init__(self, **kwargs):
            self.options = kwargs
    vision = SimpleNamespace(RunningMode=SimpleNamespace(VIDEO="VIDEO"),
        GestureRecognizerOptions=lambda **kwargs: options.append(kwargs) or kwargs,
        GestureRecognizer=SimpleNamespace(create_from_options=lambda _options: task))
    mp = SimpleNamespace(tasks=SimpleNamespace(BaseOptions=Base, vision=vision,
        components=SimpleNamespace(processors=SimpleNamespace(ClassifierOptions=lambda **kwargs: kwargs))),
        ImageFormat=SimpleNamespace(SRGB="RGB"), Image=lambda **kwargs: images.append(kwargs) or object())
    cv2 = SimpleNamespace(COLOR_BGR2RGB="BGR2RGB", cvtColor=lambda frame, code: frame)
    monkeypatch.setitem(sys.modules, "mediapipe", mp)
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    backend = gestures._Recognizer(tmp_path / "model.task")
    frame = SimpleNamespace(ndim=3, shape=(480, 640, 3), dtype="uint8")
    assert backend.recognize(frame, 100) == ("Open_Palm", 0.98, 1)
    assert options[0]["num_hands"] == 2 and options[0]["base_options"].options["delegate"] == "CPU"
    assert set(options[0]["canned_gesture_classifier_options"]["category_allowlist"]) == {*gestures.INTENTS, "None"}
    assert images[0]["image_format"] == "RGB"
    result.hand_landmarks.append(object())
    assert backend.recognize(frame, 101) == (None, 0.0, 2)
    backend.close()


@pytest.fixture
def assets(monkeypatch):
    model, license_text = b"public synthetic model", b"synthetic upstream license"
    for prefix, data in (("MODEL", model), ("LICENSE", license_text)):
        monkeypatch.setattr(gestures, prefix + "_SIZE", len(data))
        monkeypatch.setattr(gestures, prefix + "_SHA256", hashlib.sha256(data).hexdigest())
    class Response(io.BytesIO):
        def __init__(self, data, url):
            super().__init__(data)
            self.url = url
        def geturl(self):
            return self.url
    class Opener:
        calls = []
        def open(self, url, timeout):
            self.calls.append(url)
            return Response(model if url == gestures.MODEL_URL else license_text, url)
    return Opener(), model, license_text


def test_explicit_install_pins_assets_preserves_license_and_never_opens_camera(tmp_path, assets, monkeypatch):
    opener, model, license_text = assets
    monkeypatch.setattr(gestures, "_open_camera", lambda _index: pytest.fail("Setup must not open camera"))
    target = tmp_path / "public-model"
    assert gestures.install_model(target, opener=opener) == target
    assert (target / gestures.MODEL_NAME).read_bytes() == model
    assert (target / "LICENSE").read_bytes() == license_text
    assert "metadata" in (target / "NOTICE.txt").read_text()
    assert gestures.model_ready(target)
    before = len(opener.calls)
    assert gestures.install_model(target, opener=opener) == target and len(opener.calls) == before
    assert not list(tmp_path.glob(".jarvis-gesture-*"))


def test_failed_download_checksum_never_replaces_existing_user_files(tmp_path, assets, monkeypatch):
    opener, _model, _license = assets
    monkeypatch.setattr(gestures, "MODEL_SHA256", "0" * 64)
    target = tmp_path / "new-model"
    with pytest.raises(ValueError, match="checksum"):
        gestures.install_model(target, opener=opener)
    assert not target.exists() and not list(tmp_path.glob(".jarvis-gesture-*"))
    target.mkdir()
    (target / "owned.txt").write_text("preserve")
    with pytest.raises(FileExistsError):
        gestures.install_model(target, opener=opener)
    assert (target / "owned.txt").read_text() == "preserve"
