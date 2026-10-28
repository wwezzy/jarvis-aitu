"""Cue completion notifies trusted UI only; fake output never plays audio."""
import queue
import sys
import threading
from types import SimpleNamespace

import pytest

from desktop import voice


@pytest.fixture
def output(tmp_path, monkeypatch):
    completed, writes, aborts = [], [], []
    entered, drain, draining, exited = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    notified = threading.Event()
    pcm = b"\x11\x00" * 5000
    cue = SimpleNamespace(pcm=pcm, sample_rate=16000, channels=1)
    monkeypatch.setitem(sys.modules, "desktop.audio_cues",
                        SimpleNamespace(CueLibrary=lambda: SimpleNamespace(read=lambda _name: cue)))
    class Stream:
        def __enter__(self):
            entered.set()
            return self
        def write(self, data):
            writes.append(data)
        def abort(self):
            aborts.append(True)
        def __exit__(self, *_args):
            draining.set()
            assert drain.wait(2), "Synthetic device drain was not released"
            exited.set()
    def audio_only(name):
        assert name == "sounddevice"
        return SimpleNamespace(RawOutputStream=lambda **_kwargs: Stream())
    monkeypatch.setattr(voice, "importlib", SimpleNamespace(import_module=audio_only))
    monkeypatch.setattr(voice, "os", SimpleNamespace(name="nt"))
    service = voice.VoiceService(lambda _text: pytest.fail("Cue authorized a command"), lambda _state: None, tmp_path)
    def callback(result):
        completed.append((result, threading.get_ident()))
        notified.set()
    yield SimpleNamespace(service=service, completed=completed, writes=writes, aborts=aborts,
                          entered=entered, drain=drain, draining=draining, exited=exited, pcm=pcm,
                          callback=callback, notified=notified)
    drain.set()
    service.close()


def test_success_callback_waits_for_output_device_drain_and_only_fires_once(output):
    value = output
    assert value.service.play_cue("welcome_home", on_complete=value.callback)
    assert value.entered.wait(2)
    assert value.draining.wait(2)
    assert value.completed == []
    value.drain.set()
    assert value.exited.wait(2)
    assert value.notified.wait(2)
    value.service.close()
    assert value.completed == [(True, value.service._speaker.ident)]
    assert b"".join(value.writes) == value.pcm and not value.aborts
    assert not value.service._speaking.is_set()


@pytest.mark.parametrize("change", ["interrupt", "mute", "closed"])
def test_no_followup_success_if_canceled_even_after_last_pcm_block_before_drain(output, change):
    value = output
    assert value.service.play_cue("welcome_home", on_complete=value.callback)
    assert value.entered.wait(2)
    assert value.draining.wait(2)
    # The fixed fake sink reaches __exit__ after writing every block, but the
    # device has not completed playback. A callback must still report failure.
    if change == "interrupt":
        value.service.interrupt()
    elif change == "mute":
        value.service.speech_enabled = False
    else:
        value.service._closed.set()
    value.drain.set()
    assert value.exited.wait(2)
    value.service.close()
    assert value.completed == [(False, value.service._speaker.ident)]


def test_queued_cue_cancellation_calls_once_outside_lifecycle_lock(output, monkeypatch):
    value = output
    monkeypatch.setattr(value.service, "_enqueue_speech",
                        lambda item: value.service._speech.put((value.service._speech_generation, item)) or True)
    def completed(result):
        assert value.service._lock.acquire(blocking=False), "Completion ran under lifecycle lock"
        value.service._lock.release()
        value.completed.append(result)
    assert value.service.play_cue("welcome_home", on_complete=completed)
    value.service.interrupt()
    value.service.interrupt()
    assert value.completed == [False] and not value.entered.is_set()
    with pytest.raises(queue.Empty):
        value.service._speech.get_nowait()


@pytest.mark.parametrize("cause", ["missing", "muted", "closed", "unknown", "queue"])
def test_unqueued_or_unavailable_cue_reports_failure_once_without_initializing_output(output, monkeypatch, cause):
    value = output
    name = "welcome_home"
    if cause == "missing":
        monkeypatch.setitem(sys.modules, "desktop.audio_cues",
                            SimpleNamespace(CueLibrary=lambda: SimpleNamespace(read=lambda _name: None)))
    elif cause == "muted":
        value.service.speech_enabled = False
    elif cause == "closed":
        value.service._closed.set()
    elif cause == "unknown":
        name = "../unapproved.wav"
    else:
        monkeypatch.setattr(value.service, "_enqueue_speech", lambda _item: False)
    assert value.service.play_cue(name, on_complete=value.callback) is False
    assert value.completed == [(False, threading.get_ident())] and not value.entered.is_set()


def test_output_failure_and_raising_ui_callback_do_not_strand_speech_worker(output, monkeypatch):
    value = output
    def failure(*_args, **_kwargs):
        raise OSError("synthetic unavailable audio")
    monkeypatch.setattr(value.service, "_play_pcm", failure)
    def callback(played):
        value.completed.append(played)
        raise RuntimeError("disposed UI bridge")
    assert value.service.play_cue("welcome_home", on_complete=callback)
    value.service._speech.put(None)
    value.service._speaker.join(2)
    assert value.completed == [False] and not value.service._speaker.is_alive()
