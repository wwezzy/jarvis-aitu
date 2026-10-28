"""No live player, music, volume or browser is used by these tests."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from desktop.core import DesktopCore
from desktop import media


@pytest.fixture(autouse=True)
def no_live_native_player_automation(monkeypatch):
    # Native My Vibe behavior has its own fully injected process/pipe tests.
    # Existing synthetic setup manifests must never inspect a real app window.
    monkeypatch.setattr(media, "_request_yandex_ui_play", lambda *_args, **_options: "unavailable")


def test_revocation_during_native_player_verification_prevents_launch_and_browser_fallback(monkeypatch):
    authorized = [True]
    def verify():
        authorized[0] = False  # For example, owner revocation during EXE hashing.
        return Path("C:/Approved/YandexMusic.exe")
    def forbidden(*_args, **_kwargs):
        pytest.fail("Revoked music request reached an OS action")
    monkeypatch.setattr(media, "_native_music_executable", verify)
    with pytest.raises(PermissionError):
        media._open_player(forbidden, forbidden, lambda: authorized[0])


@pytest.mark.parametrize("command,action", [
    ("Джарвис, открой яндекс музыку", "music_open"),
    ("включи музыку", "music_play"), ("возобнови музыку", "music_play"),
    ("включи утреннюю музыку", "music_morning"), ("play music", "music_play"),
    ("open yandex music", "music_open"), ("play morning music", "music_morning"),
])
def test_music_requests_route_before_model_to_finite_action(tmp_path, monkeypatch, command, action):
    calls = []
    core = DesktopCore(tmp_path, executor=lambda value: calls.append(value) or "Fake launch accepted")
    core.store.set_preferences({"local_chat_enabled": True})
    monkeypatch.setattr("desktop.chat.LocalChat.answer", lambda *_a, **_k: pytest.fail("Music called an LLM"))
    reply = core.handle(command, source="voice")
    assert calls == [action] and reply.action == action and reply.confirmation_id is None


def test_source_and_mixed_music_text_cannot_authorize_actions(tmp_path):
    calls = []
    core = DesktopCore(tmp_path, executor=calls.append)
    for source in ("model", "document", "web"):
        core.handle("включи музыку", source=source)
    for command in ('"включи музыку"', "скажи включи музыку", "включи музыку и выключи ноут", "открой музыку https://other.invalid"):
        core.handle(command)
    assert calls == []


@pytest.mark.parametrize("action", ["music_open", "music_morning"])
def test_browser_fallback_does_not_control_media_or_invent_playlist_playback(action):
    opened = []
    def denied(*_a, **_k):
        pytest.fail("Opening command attempted to control a player")
    text = media.execute_music_action(action, Path("C:/Windows/System32"), runner=denied, opener=opened.append)
    assert opened == [media.MUSIC_URL]
    assert "Открываю сайт" in text and "нажми Play" in text
    assert "Включил" not in text and "воспроизводит" not in text


@pytest.mark.parametrize("state", ["no_session", "not_music", "unknown_source", "not_playable", "rejected", "not_confirmed", "unavailable", "timeout"])
def test_unconfirmed_or_ineligible_player_gets_truthful_fixed_url_fallback(monkeypatch, state):
    opened = []
    monkeypatch.setattr(media, "_request_music_play", lambda *_a, **_k: state)
    text = media.execute_music_action("music_play", Path("C:/Windows/System32"), opener=opened.append)
    assert opened == ["https://music.yandex.ru/"]
    assert "не могу подтвердить воспроизведение" in text and "войди" in text
    assert "Продолжил" not in text


@pytest.mark.parametrize("state", ["playing", "already_playing"])
def test_observed_music_playing_does_not_launch_another_app(monkeypatch, state):
    monkeypatch.setattr(media, "_request_music_play", lambda *_a, **_k: state)
    def denied(*_a, **_k):
        pytest.fail("Confirmed player unexpectedly opened another app")
    text = media.execute_music_action("music_play", Path("C:/Windows/System32"), opener=denied, launcher=denied)
    assert text == ("Музыка уже играет." if state == "already_playing" else "Продолжил музыку.")


def test_native_bridge_is_a_fixed_bounded_hidden_command_with_no_user_parameters():
    calls = []
    def runner(arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(returncode=0, stdout=b'{"protocol":1,"state":"playing"}', stderr=b"")
    assert media._request_music_play(Path("C:/Windows/System32"), runner) == "playing"
    arguments, options = calls[0]
    assert arguments[:6] == [str(Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")), "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-EncodedCommand"]
    assert len(arguments) == 7
    assert base64.b64decode(arguments[-1]).decode("utf-16-le") == media._SMTC_SCRIPT
    assert options["shell"] is False and options["timeout"] == 6
    assert options["stdin"] == subprocess.DEVNULL
    assert "TryTogglePlayPauseAsync" not in media._SMTC_SCRIPT
    assert "TryPauseAsync" not in media._SMTC_SCRIPT


@pytest.mark.parametrize("output", [b"not JSON", b"[]", b'{"protocol":true,"state":"playing"}',
    b'{"protocol":1,"state":"arbitrary"}', b'{"protocol":1,"state":"playing","metadata":"PRIVATE"}',
    b"x" * 4097])
def test_malformed_bridge_response_cannot_claim_playback(output):
    state = media._request_music_play(Path("C:/Windows/System32"), lambda *_a, **_k:
        SimpleNamespace(returncode=0, stdout=output, stderr=b"PRIVATE diagnostics"))
    assert state == "unavailable"


def test_native_bridge_failure_or_timeout_never_exposes_process_diagnostics():
    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("private arguments", 6)
    assert media._request_music_play(Path("C:/Windows/System32"), timeout) == "timeout"
    assert media._request_music_play(Path("C:/Windows/System32"), lambda *_a, **_k:
        SimpleNamespace(returncode=1, stdout=b"PRIVATE", stderr=b"PRIVATE")) == "unavailable"


def installed_manifest(tmp_path, monkeypatch):
    module = tmp_path / "installation" / "app" / "desktop" / "media.py"
    module.parent.mkdir(parents=True)
    module.write_text("synthetic module location", encoding="utf-8")
    executable = tmp_path / "YandexMusic.exe"
    executable.write_bytes(b"synthetic setup-verified application")
    manifest = module.parents[2] / "installation.json"
    manifest.write_text(json.dumps({"kind": "isolated-source-runtime", "yandexMusicExe": str(executable),
        "yandexMusicSha256": hashlib.sha256(executable.read_bytes()).hexdigest()}), encoding="utf-8")
    monkeypatch.setattr(media, "__file__", str(module))
    return executable, manifest


def test_setup_pinned_native_app_is_preferred_without_shell_args_or_false_play_claim(tmp_path, monkeypatch):
    executable, _ = installed_manifest(tmp_path, monkeypatch)
    assert media._native_music_executable() == executable
    calls = []
    text = media.execute_music_action("music_open", Path("C:/Windows/System32"),
        launcher=lambda arguments, **options: calls.append((arguments, options)),
        opener=lambda *_: pytest.fail("Verified native app should be preferred"))
    assert calls[0][0] == [str(executable)] and calls[0][1]["shell"] is False
    assert "Музыка открывается" in text and "нажми Play" in text
    assert "Продолжил" not in text


def test_native_manifest_cannot_launch_another_basename_modified_binary_or_network_path(tmp_path, monkeypatch):
    executable, manifest = installed_manifest(tmp_path, monkeypatch)
    data = json.loads(manifest.read_text())
    for replacement in ("relative/YandexMusic.exe", r"\\server\share\YandexMusic.exe", "https://invalid/YandexMusic.exe", str(tmp_path / "other.exe")):
        manifest.write_text(json.dumps({**data, "yandexMusicExe": replacement}), encoding="utf-8")
        assert media._native_music_executable() is None
    manifest.write_text(json.dumps(data), encoding="utf-8")
    executable.write_bytes(b"different application")
    assert media._native_music_executable() is None
    opened = []
    media.execute_music_action("music_open", Path("C:/Windows/System32"), opener=opened.append,
        launcher=lambda *_a, **_k: pytest.fail("Changed executable was launched"))
    assert opened == [media.MUSIC_URL]


def test_repository_run_never_discovers_a_global_program_manifest(tmp_path, monkeypatch):
    module = tmp_path / "repository" / "desktop" / "media.py"
    module.parent.mkdir(parents=True)
    module.write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(media, "__file__", str(module))
    assert media._native_music_executable() is None


def test_cold_native_launch_waits_boundedly_for_music_session_then_observes_playing(tmp_path, monkeypatch):
    executable, _ = installed_manifest(tmp_path, monkeypatch)
    states = iter(["no_session", "no_session", "playing"])
    requests, launches = [], []
    clock = [1000.0]
    monkeypatch.setattr(media.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(media.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value))
    def request(*_a, **options):
        requests.append(options.get("timeout"))
        return next(states)
    monkeypatch.setattr(media, "_request_music_play", request)
    text = media.execute_music_action("music_play", Path("C:/Windows/System32"),
        launcher=lambda args, **kwargs: launches.append(args),
        opener=lambda *_: pytest.fail("Native music app did not require browser"))
    assert launches == [[str(executable)]]
    assert len(requests) == 3 and all(0 < value <= 2.5 for value in requests[1:])
    assert text == "Продолжил музыку."


def test_cold_native_discovery_is_bounded_and_persistent_video_never_claims_play(tmp_path, monkeypatch):
    installed_manifest(tmp_path, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(media.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(media.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value))
    calls = []
    def missing(*_a, **options):
        calls.append(options)
        return "no_session"
    monkeypatch.setattr(media, "_request_music_play", missing)
    text = media.execute_music_action("music_play", Path("C:/Windows/System32"), launcher=lambda *_a, **_k: None)
    assert clock[0] <= 1002.5 and "не могу подтвердить воспроизведение" in text
    assert len(calls) < 25
    calls.clear()
    def video(*_a, **options):
        calls.append(options)
        return "not_music"
    monkeypatch.setattr(media, "_request_music_play", video)
    media.execute_music_action("music_play", Path("C:/Windows/System32"), launcher=lambda *_a, **_k: None)
    assert len(calls) < 25 and clock[0] <= 1005.0


def test_new_eligible_yandex_session_after_prior_video_can_play_but_ambiguous_requests_never_repeat(tmp_path, monkeypatch):
    installed_manifest(tmp_path, monkeypatch)
    states = iter(["not_music", "unknown_source", "playing"])
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(media.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(media.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value))
    def discover(*_a, **options):
        calls.append(options)
        return next(states)
    monkeypatch.setattr(media, "_request_music_play", discover)
    result = media.execute_music_action("music_play", Path("C:/Windows/System32"), launcher=lambda *_a, **_k: None)
    assert result == "Продолжил музыку." and len(calls) == 3
    for ambiguous in ("rejected", "not_confirmed", "timeout", "unavailable"):
        calls.clear()
        def uncertain(*_a, **options):
            calls.append(options)
            return ambiguous
        monkeypatch.setattr(media, "_request_music_play", uncertain)
        result = media.execute_music_action("music_play", Path("C:/Windows/System32"), launcher=lambda *_a, **_k: None)
        assert "не могу подтвердить" in result and len(calls) == 1


def test_morning_launches_verified_native_yandex_before_retrying_only_yandex_music(tmp_path, monkeypatch):
    executable, _ = installed_manifest(tmp_path, monkeypatch)
    order, clock = [], [1000.0]
    states = iter(["unknown_source", "not_music", "playing"])
    monkeypatch.setattr(media.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(media.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def request(_system, **options):
        assert options["yandex_only"] is True and 0 < options["timeout"] <= 2.5
        order.append("request")
        return next(states)
    monkeypatch.setattr(media, "_request_music_play", request)
    text = media.execute_music_action("music_morning", Path("C:/Windows/System32"),
        launcher=lambda arguments, **_options: order.append(arguments),
        opener=lambda *_: pytest.fail("Verified native app should be preferred"))
    assert order == [[str(executable)], "request", "request", "request"]
    assert "Яндекс Музыке" in text and "Продолжил" in text
    assert "случайн" not in text and "подборк" not in text


@pytest.mark.parametrize("state", ["rejected", "not_confirmed", "timeout", "unavailable"])
def test_morning_never_retries_an_ambiguous_play_or_claims_playback(tmp_path, monkeypatch, state):
    installed_manifest(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(media, "_request_music_play", lambda *_a, **_k: calls.append(True) or state)
    text = media.execute_music_action("music_morning", Path("C:/Windows/System32"), launcher=lambda *_a, **_k: None)
    assert calls == [True] and "не могу подтвердить" in text and "Продолжил" not in text


def test_morning_yandex_filter_uses_fixed_script_not_user_text():
    calls = []
    def runner(arguments, **_options):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout=b'{"protocol":1,"state":"playing"}')
    assert media._request_music_play(Path("C:/Windows/System32"), runner, yandex_only=True) == "playing"
    script = base64.b64decode(calls[0][-1]).decode("utf-16-le")
    assert script == media._SMTC_SCRIPT_YANDEX and script != media._SMTC_SCRIPT
    assert "} -YandexOnly" in script
    with pytest.raises(ValueError):
        media._request_music_play(Path("C:/Windows/System32"), runner, yandex_only="other.exe")


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell projection policy test")
def test_actual_smtc_policy_with_fake_sessions_rejects_video_unknown_and_never_toggles(tmp_path):
    """Execute only the pure policy using synthetic session methods, no WinRT."""
    script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PSModuleAutoLoadingPreference = 'None'
[Console]::Error.WriteLine('policy:start')
""" + media._SMTC_POLICY + r"""
class FakeSession {
    [string]$SourceAppUserModelId
    [string]$Kind
    [string]$Status
    [bool]$CanPlay = $true
    [bool]$Accept = $true
    [bool]$BecomePlaying = $true
    [bool]$SwitchTypeAfterPlay = $false
    [int]$Calls = 0
    [object] GetPlaybackInfo() {
        return [pscustomobject]@{PlaybackType=$this.Kind;PlaybackStatus=$this.Status;Controls=@{IsPlayEnabled=$this.CanPlay}}
    }
    [bool] TryPlayAsync() {
        $this.Calls += 1
        if ($this.BecomePlaying) { $this.Status = 'Playing' }
        if ($this.SwitchTypeAfterPlay) { $this.Kind = 'Video' }
        return $this.Accept
    }
}
function Start-Sleep { param([int]$Milliseconds) }
function Invoke-TestMusicCase([string]$Name, $Session) {
    [Console]::Error.WriteLine('case:' + $Name)
    $state = Invoke-CurrentMusicPlay $Session {param($operation,$type) return $operation}
    [Console]::Out.WriteLine($Name + '|' + [string]$state + '|' + [string]$Session.Calls)
}
foreach ($kind in @('Video','Unknown','Music')) {
    $s=[FakeSession]::new();$s.Kind=$kind;$s.SourceAppUserModelId='ru.yandex.desktop.music';$s.Status='Paused'
    Invoke-TestMusicCase $kind $s
}
$s=[FakeSession]::new();$s.Kind='Music';$s.SourceAppUserModelId='chrome.exe';$s.Status='Paused'
Invoke-TestMusicCase 'Browser' $s
$s=[FakeSession]::new();$s.Kind='Music';$s.SourceAppUserModelId='Spotify.exe';$s.Status='Playing'
Invoke-TestMusicCase 'Already' $s
$s=[FakeSession]::new();$s.Kind='Music';$s.SourceAppUserModelId='Spotify.exe';$s.Status='Paused';$s.BecomePlaying=$false
Invoke-TestMusicCase 'Unconfirmed' $s
$s=[FakeSession]::new();$s.Kind='Music';$s.SourceAppUserModelId='Spotify.exe';$s.Status='Paused';$s.CanPlay=$false
Invoke-TestMusicCase 'Disabled' $s
$s=[FakeSession]::new();$s.Kind='Music';$s.SourceAppUserModelId='Spotify.exe';$s.Status='Paused';$s.Accept=$false;$s.BecomePlaying=$false
Invoke-TestMusicCase 'Rejected' $s
$s=[FakeSession]::new();$s.Kind='Music';$s.SourceAppUserModelId='Spotify.exe';$s.Status='Paused';$s.SwitchTypeAfterPlay=$true
Invoke-TestMusicCase 'ChangedType' $s
$s=[FakeSession]::new();$s.Kind='Music';$s.SourceAppUserModelId='Spotify.exe';$s.Status='Paused'
$state=Invoke-CurrentMusicPlay $s {param($op,$type) return $op} -YandexOnly
[Console]::Out.WriteLine('YandexOnlySpotify|' + [string]$state + '|' + [string]$s.Calls)
$s=[FakeSession]::new();$s.Kind='Music';$s.SourceAppUserModelId='ru.yandex.desktop.music';$s.Status='Paused'
$state=Invoke-CurrentMusicPlay $s {param($op,$type) return $op} -YandexOnly
[Console]::Out.WriteLine('YandexOnlyYandex|' + [string]$state + '|' + [string]$s.Calls)
[Console]::Error.WriteLine('policy:done')
exit 0
"""
    # The fake policy defines neither RequestAsync nor a live manager. It never
    # enumerates sessions, changes volume, opens a browser or plays any audio.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    stdout_path, stderr_path = tmp_path / "states.txt", tmp_path / "phases.txt"
    # Windows communicate() waits for pipe EOF before checking process exit.
    # An inherited writer can therefore time out an already-finished child on
    # CI. File handles let run() wait for this exact PID, without reader threads.
    # Direct .NET writes also avoid unrelated ConvertTo-Json module autoload and
    # progress CLIXML. No execution-policy override or longer deadline is used.
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            result = subprocess.run(["C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-EncodedCommand", encoded],
                shell=False, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=media.powershell_environment(Path("C:/Windows/System32")))
        except subprocess.TimeoutExpired:
            phases = stderr_path.read_bytes()[:2048].decode("ascii", errors="replace")
            pytest.fail(f"Synthetic media policy process exceeded 30s; phases: {phases}", pytrace=False)
    diagnostics = stderr_path.read_bytes()[:2048].decode("ascii", errors="replace")
    assert result.returncode == 0, f"Synthetic Windows media policy failed: {diagnostics}"
    assert diagnostics.splitlines() == ["policy:start", "case:Video", "case:Unknown", "case:Music", "case:Browser",
        "case:Already", "case:Unconfirmed", "case:Disabled", "case:Rejected", "case:ChangedType", "policy:done"]
    assert stdout_path.stat().st_size <= 4096
    rows = [line.split("|") for line in stdout_path.read_text(encoding="ascii").splitlines()]
    assert len(rows) == 11 and all(len(row) == 3 for row in rows)
    states = {kind: (state, int(calls)) for kind, state, calls in rows}
    assert states == {"Video": ("not_music", 0), "Unknown": ("not_music", 0), "Music": ("playing", 1),
                      "Browser": ("unknown_source", 0), "Already": ("already_playing", 0), "Unconfirmed": ("not_confirmed", 1),
                      "Disabled": ("not_playable", 0), "Rejected": ("rejected", 1), "ChangedType": ("not_music", 1),
                      "YandexOnlySpotify": ("unknown_source", 0), "YandexOnlyYandex": ("playing", 1)}
