"""Independent voice lifecycle regressions; no model, microphone or playback."""
import threading

import pytest

from desktop import clone_speech
from desktop.voice import VoiceService, _PREPARE_SPEECH


def service_worker():
    service = VoiceService(lambda _text: None, lambda _state: None)
    service.speech_engine = "clone"
    worker = threading.Thread(target=service._speak_loop, daemon=True)
    service._speaker = worker
    worker.start()
    return service, worker


def enqueue_prepare(service):
    service._speech.put((service._speech_generation, _PREPARE_SPEECH))


def blocked_constructor(monkeypatch):
    entered, release, disposed = threading.Event(), threading.Event(), threading.Event()
    instances = []

    class Backend:
        def __init__(self, **_kwargs):
            self.prepared = self.closed = 0
            instances.append(self)
            entered.set()
            assert release.wait(2)

        @property
        def is_ready(self):
            return False

        def prepare_background(self, **_kwargs):
            self.prepared += 1

        def close(self):
            self.closed += 1
            disposed.set()

    monkeypatch.setattr(clone_speech, "CloneSpeech", Backend)
    return entered, release, disposed, instances


@pytest.mark.parametrize("preferences", [{"voice_engine": "windows"}, {"speech_enabled": False}])
def test_constructor_finishing_after_engine_switch_or_mute_cannot_start_backend(monkeypatch, preferences):
    entered, release, disposed, instances = blocked_constructor(monkeypatch)
    service, worker = service_worker()
    try:
        enqueue_prepare(service)
        assert entered.wait(1)
        service.apply_preferences(preferences)
        release.set()
        assert disposed.wait(1)
        assert instances[0].prepared == 0 and instances[0].closed == 1
        assert service._active_backend is None
    finally:
        release.set()
        service.close()
        worker.join(1)
    assert not worker.is_alive()


def test_constructor_finishing_during_close_is_disposed_without_late_preparation(monkeypatch):
    entered, release, disposed, instances = blocked_constructor(monkeypatch)
    service, worker = service_worker()
    closing = None
    try:
        enqueue_prepare(service)
        assert entered.wait(1)
        closing = threading.Thread(target=service.close)
        closing.start()
        assert service._closed.wait(1)
        release.set()
        assert disposed.wait(1)
        closing.join(1)
        worker.join(1)
        assert not closing.is_alive() and not worker.is_alive()
        assert service._active_backend is None
        assert instances[0].closed == 1 and instances[0].prepared == 0
    finally:
        release.set()
        service.close()
        if closing:
            closing.join(1)
        worker.join(1)


def test_switch_away_and_back_constructs_new_backend_instead_of_reusing_closed_local_reference(monkeypatch):
    instances, prepared = [], threading.Semaphore(0)

    class Backend:
        def __init__(self, **_kwargs):
            self.closed = 0
            instances.append(self)

        @property
        def is_ready(self):
            return False

        def prepare_background(self, **_kwargs):
            assert not self.closed
            prepared.release()

        def close(self):
            self.closed += 1

    monkeypatch.setattr(clone_speech, "CloneSpeech", Backend)
    service, worker = service_worker()
    try:
        enqueue_prepare(service)
        assert prepared.acquire(timeout=1)
        first = instances[0]
        service.apply_preferences({"voice_engine": "windows"})
        assert first.closed == 1 and service._active_backend is None
        service.apply_preferences({"voice_engine": "clone"})
        enqueue_prepare(service)
        assert prepared.acquire(timeout=1)
        assert len(instances) == 2 and instances[1] is service._active_backend
        assert instances[1] is not first and not instances[1].closed
    finally:
        service.close()
        worker.join(1)
    assert not worker.is_alive() and instances[1].closed == 1
