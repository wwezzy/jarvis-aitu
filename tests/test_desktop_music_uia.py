"""Owned My Vibe helper protocol and decisions; no live player or playback."""
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from desktop import media


def frame(state):
    return json.dumps({"protocol": 1, "state": state}).encode() + b"\n"


class Output:
    def __init__(self):
        self.queue = queue.Queue()
        self.closed = False

    def readline(self, maximum):
        return self.queue.get(timeout=3)[:maximum]

    def close(self):
        self.closed = True
        self.queue.put(b"")


class Process:
    def __init__(self, first=None, final="playing", *, before_ready=None, before_final=None):
        self.stdout, self.returncode, self.kills = Output(), None, 0
        self.writes, self.final = [], final
        self.first = [frame("ready")] if first is None else first
        self.before_ready, self.before_final = before_ready, before_final
        self.stdin = SimpleNamespace(write=self.write, flush=lambda: None, close=lambda: None)

    def write(self, data):
        self.writes.append(data)
        if len(self.writes) == 1:
            if self.before_ready is not None:
                self.before_ready()
            for value in self.first:
                self.stdout.queue.put(value)
            if self.first and self.first[0] != frame("ready"):
                self.stdout.queue.put(b"")
        else:
            assert data == b'{"protocol":1,"action":"play"}\n'
            if self.before_final is not None:
                self.before_final()
            self.stdout.queue.put(frame(self.final))
            self.stdout.queue.put(b"")
            self.returncode = 0
        return len(data)

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1
        self.returncode = -9
        self.stdout.queue.put(b"")

    def wait(self, timeout):
        assert 0 < timeout <= 1
        return self.returncode


@pytest.fixture
def identity(monkeypatch):
    value = (Path("C:/Approved/Яндекс Музыка.exe"), "a" * 64)
    monkeypatch.setattr(media, "_native_music_identity", lambda: value)
    return value


def invoke(process, **options):
    calls = []
    before = set(threading.enumerate())

    def start(arguments, **settings):
        calls.append((arguments, settings))
        return process

    result = media._request_yandex_ui_play(Path("C:/Windows/System32"), process_factory=start, **options)
    assert not [thread for thread in threading.enumerate()
                if thread.name == "JarvisMusicReadback" and thread not in before]
    assert process.stdout.closed
    return result, calls


def test_verified_helper_requires_fresh_go_once_and_uses_pinned_native_identity(identity, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "NEVER_PASS_SYNTHETIC_KEY")
    process = Process()
    state, calls = invoke(process, authorization_guard=lambda: True)
    assert state == "playing" and len(calls) == 1 and len(process.writes) == 2
    assert json.loads(process.writes[0]) == {"protocol": 1, "executable": str(identity[0]), "sha256": identity[1]}
    arguments, settings = calls[0]
    assert arguments[:6] == [str(Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")),
                             "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-EncodedCommand"]
    script = base64.b64decode(arguments[-1]).decode("utf-16-le")
    assert script == media._YANDEX_UIA_SCRIPT
    assert settings["shell"] is False and settings["stdin"] == settings["stdout"] == subprocess.PIPE
    assert settings["stderr"] == subprocess.DEVNULL and "OPENAI_API_KEY" not in settings["env"]
    assert "NEVER_PASS_SYNTHETIC_KEY" not in script


def test_already_advancing_player_needs_no_invoke(identity):
    process = Process(first=[frame("already_playing")])
    assert invoke(process)[0] == "already_playing"
    assert len(process.writes) == 1


@pytest.mark.parametrize("output", [b"invalid\n", b"x" * 4097 + b"\n", b"[]\n",
    b'{"protocol":true,"state":"playing"}\n', b'{"protocol":1,"state":["playing"]}\n',
    b'{"protocol":1,"state":"playing","track":"PRIVATE"}\n', frame("playing"),
    frame("unknown-unbounded-command")])
def test_bad_or_out_of_order_helper_frames_never_send_play_or_claim_success(identity, output):
    process = Process(first=[output])
    assert invoke(process)[0] == "unavailable"
    assert len(process.writes) == 1


def test_duplicate_ready_does_not_repeat_the_invoke(identity):
    process = Process(first=[frame("ready"), frame("ready")])
    assert invoke(process)[0] == "unavailable"
    assert len(process.writes) == 2


@pytest.mark.parametrize("state", ["not_music", "not_playable", "no_session", "not_confirmed", "unavailable"])
def test_scope_missing_queue_or_unconfirmed_readback_is_a_fixed_failure(identity, state):
    process = Process(first=[frame(state)])
    assert invoke(process)[0] == state and len(process.writes) == 1


@pytest.mark.parametrize("moment", ["before_ready", "before_final"])
def test_owner_revocation_aborts_only_owned_helper_and_never_reports_late_play(identity, moment):
    authorized = [True]
    def revoke():
        authorized[0] = False
    process = Process(**{moment: revoke})
    with pytest.raises(PermissionError):
        invoke(process, authorization_guard=lambda: authorized[0])
    assert len(process.writes) == (1 if moment == "before_ready" else 2)
    assert process.stdout.closed


def test_unverified_app_or_revocation_during_pin_verification_never_creates_helper(monkeypatch):
    def forbidden(*_args, **_settings):
        pytest.fail("Unverified or revoked request created native UI helper")
    monkeypatch.setattr(media, "_native_music_identity", lambda: None)
    assert media._request_yandex_ui_play(Path("C:/Windows/System32"), process_factory=forbidden) == "unavailable"
    authorized = [True]
    def identity():
        authorized[0] = False
        return Path("C:/Approved/YandexMusic.exe"), "a" * 64
    monkeypatch.setattr(media, "_native_music_identity", identity)
    with pytest.raises(PermissionError):
        media._request_yandex_ui_play(Path("C:/Windows/System32"), process_factory=forbidden,
                                     authorization_guard=lambda: authorized[0])


def test_blocked_helper_has_absolute_deadline_and_releases_owned_reader(identity):
    process = Process(first=[])
    started = time.monotonic()
    assert invoke(process, timeout=.1)[0] == "timeout"
    assert time.monotonic() - started < .8 and process.kills == 1 and len(process.writes) == 1


def test_secondary_smtc_observation_never_repeats_play_or_ignores_a_paused_yandex_session(monkeypatch):
    monkeypatch.setattr(media, "_request_yandex_ui_play", lambda *_a, **_kw: "playing")
    calls = []
    def observe(*_args, **options):
        calls.append(options)
        return "not_confirmed"
    monkeypatch.setattr(media, "_request_music_play", observe)
    assert media._try_native_vibe(Path("C:/Windows/System32")) is None
    assert calls == [{"runner": None, "timeout": 1.5, "yandex_only": True, "observe_only": True}]


@pytest.mark.parametrize("action", ["music_play", "music_morning"])
def test_absent_smtc_can_use_one_verified_my_vibe_request_after_native_launch(monkeypatch, action):
    calls, clock = [], [100.0]
    monkeypatch.setattr(media, "_open_player", lambda *_args, **_options: "native")
    monkeypatch.setattr(media.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(media.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(media, "_request_music_play", lambda *_a, **_kw: "no_session")
    monkeypatch.setattr(media, "_request_yandex_ui_play", lambda *_a, **_kw: calls.append(True) or "playing")
    result = media.execute_music_action(action, Path("C:/Windows/System32"), authorization_guard=lambda: True)
    assert calls == [True] and result == "Включил музыку в Яндекс Музыке."


@pytest.mark.parametrize("state", ["timeout", "unavailable", "rejected", "not_confirmed"])
def test_uncertain_prior_play_never_invokes_a_second_player_control(monkeypatch, state):
    monkeypatch.setattr(media, "_open_player", lambda *_args, **_options: "native")
    monkeypatch.setattr(media, "_request_music_play", lambda *_a, **_kw: state)
    monkeypatch.setattr(media, "_request_yandex_ui_play", lambda *_a, **_kw: pytest.fail("Ambiguous play repeated through UIA"))
    text = media.execute_music_action("music_play", Path("C:/Windows/System32"))
    assert "не могу подтвердить" in text and "Включил" not in text


def test_observation_script_sets_readonly_switch_and_exposes_no_external_actions():
    calls = []
    def runner(arguments, **_settings):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout=frame("not_confirmed"))
    assert media._request_music_play(Path("C:/Windows/System32"), runner,
                                     yandex_only=True, observe_only=True) == "not_confirmed"
    script = base64.b64decode(calls[0][-1]).decode("utf-16-le")
    assert "} -ObserveOnly -YandexOnly" in script
    assert script.index("if ($ObserveOnly)") < script.index("$Session.TryPlayAsync()")
    assert all(forbidden not in media._YANDEX_UIA_SCRIPT for forbidden in
               ("SendKeys", "keybd_event", "PostMessage", "WM_APPCOMMAND", "TryToggle", "Invoke-WebRequest"))


@pytest.mark.skipif(os.name != "nt", reason="Parse Windows helper syntax without executing its UI Automation code")
def test_native_uia_source_parses_without_any_live_automation():
    parser = r"""
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
$source=[Console]::In.ReadToEnd()
$tokens=$null;$errors=$null
[void][Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)
[Console]::Out.WriteLine([string]$errors.Count)
if($errors.Count -gt 0){exit 1}
"""
    encoded = base64.b64encode(parser.encode("utf-16-le")).decode()
    result = subprocess.run(["C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile",
        "-NonInteractive", "-EncodedCommand", encoded], input=media._YANDEX_UIA_SCRIPT.encode("utf-8"),
        shell=False, capture_output=True, timeout=10,
        env=media.powershell_environment(Path("C:/Windows/System32")), creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0 and result.stdout.strip() == b"0"
