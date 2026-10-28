"""Cold preparation/admission regressions: fake helper only, no GPU or audio."""
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import clone_speech as clone
from test_clone_speech import adapter, manifest as manifest


def test_preparation_has_no_hidden_english_synthesis(manifest):
    voice, process, launches = adapter(manifest)
    try:
        assert voice.prepare()
        assert voice.is_ready and len(launches) == 1
        assert process.requests == []
        assert voice.fixed_ack("en") is None
        assert voice.synthesize("Обычный русский ответ") == (b"\x01\x00" * 100, 24000)
        assert [request["language"] for request in process.requests] == ["ru"]
    finally:
        voice.close()


def test_readiness_is_published_only_after_preparation_releases_request_lock(manifest):
    voice, process, _ = adapter(manifest)
    releasing, release = threading.Event(), threading.Event()
    original = voice._request_lock

    class ReleaseBarrier:
        def acquire(self, **kwargs):
            return original.acquire(**kwargs)

        def release(self):
            releasing.set()
            assert release.wait(2)
            original.release()

    voice._request_lock = ReleaseBarrier()
    results = []
    worker = threading.Thread(target=lambda: results.append(voice.prepare()))
    try:
        worker.start()
        assert releasing.wait(1)
        assert not voice.is_ready
        assert process.requests == []
        assert voice.fixed_ack("ru") == (b"\x01\x00" * 2400, 24000)
        release.set()
        worker.join(1)
        assert not worker.is_alive() and results == [True]
        assert voice.is_ready
    finally:
        release.set()
        worker.join(2)
        voice.close()


@pytest.mark.parametrize("finish", ["cancel", "close", "validator_error"])
def test_stream_waiting_for_other_request_is_interruptible_without_starting_helper(manifest, finish):
    voice, process, launches = adapter(manifest)
    entered, cancelled = threading.Event(), threading.Event()
    errors = []

    def cancel():
        entered.set()
        if finish == "validator_error" and cancelled.is_set():
            raise RuntimeError("Synthetic private validator detail")
        return cancelled.is_set()

    def render():
        try:
            list(voice.stream("Русский ответ", cancel=cancel))
        except clone.CloneSpeechUnavailable as error:
            errors.append(str(error))

    worker = threading.Thread(target=render)
    with voice._request_lock:
        try:
            worker.start()
            assert entered.wait(1)
            if finish == "close":
                voice.close()
            else:
                cancelled.set()
            worker.join(1)
            assert not worker.is_alive()
            assert errors == ["closed" if finish == "close" else "cancelled"]
            assert not launches and process.requests == [] and process.terminated == 0
        finally:
            cancelled.set()
    worker.join(1)
    voice.close()


def test_request_lock_wait_deadline_does_not_stop_inflight_owned_helper(manifest):
    voice, process, launches = adapter(manifest, request_timeout=0.1)
    assert voice.prepare()
    errors = []

    def render():
        try:
            list(voice.stream("Ответ не должен попасть в очередь помощника"))
        except clone.CloneSpeechUnavailable as error:
            errors.append(str(error))

    worker = threading.Thread(target=render)
    try:
        with voice._request_lock:
            started = time.monotonic()
            worker.start()
            worker.join(1)
            assert not worker.is_alive() and time.monotonic() - started < 1
            assert errors == ["helper_timeout"]
            assert voice.is_ready and process.terminated == 0 and len(launches) == 1
            assert process.requests == []
    finally:
        worker.join(1)
        voice.close()


def test_prepare_admission_timeout_cannot_kill_another_inflight_request(manifest):
    voice, process, _ = adapter(manifest, request_timeout=0.1)
    assert voice.prepare()
    results = []
    worker = threading.Thread(target=lambda: results.append(voice.prepare()))
    try:
        with voice._request_lock:
            worker.start()
            worker.join(1)
            assert not worker.is_alive() and results == [False]
            assert process.terminated == 0 and voice.is_ready
    finally:
        worker.join(1)
        voice.close()


def test_failed_background_prepare_has_one_attempt_per_cooldown(manifest, monkeypatch):
    moment, launches = [100.0], []
    monkeypatch.setattr(clone, "time", SimpleNamespace(monotonic=lambda: moment[0]))

    def unavailable(*args, **kwargs):
        launches.append(1)
        raise OSError("Synthetic private runtime path")

    voice = clone.CloneSpeech(manifest, personal_use=True, _process_factory=unavailable)
    try:
        assert voice.prepare_background()
        voice._background_thread.join(1)
        assert not voice.is_warming and not voice.is_ready
        assert voice.reason == "helper_unavailable" and len(launches) == 1
        for _ in range(5):
            assert not voice.prepare_background()
            assert voice.fixed_ack("ru") == (b"\x01\x00" * 2400, 24000)
        moment[0] = 159.99
        assert not voice.prepare_background() and len(launches) == 1
        moment[0] = 160.0
        assert voice.prepare_background()
        voice._background_thread.join(1)
        assert len(launches) == 2 and not voice.is_warming
    finally:
        voice.close()


def test_new_explicit_backend_can_retry_without_inheriting_failure_cooldown(manifest):
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(1)
        raise OSError("Synthetic runtime unavailable")

    failed = clone.CloneSpeech(manifest, personal_use=True, _process_factory=unavailable)
    fresh, process, launches = adapter(manifest)
    try:
        assert failed.prepare_background()
        failed._background_thread.join(1)
        assert not failed.prepare_background() and len(attempts) == 1
        assert fresh.prepare_background()
        fresh._background_thread.join(1)
        assert fresh.is_ready and len(launches) == 1 and process.requests == []
    finally:
        failed.close()
        fresh.close()
