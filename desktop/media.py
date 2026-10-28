"""Finite local music control with honest browser fallback.

Windows' built-in WinRT projection is used through a fixed, hidden PowerShell
bridge. No user text, URL, source app ID or model output becomes script code.
Only a current, explicitly Music-typed, recognized music-player session may
receive TryPlayAsync. Browser/video/unknown sessions are never resumed blindly.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from desktop.native_runtime import powershell_environment

MUSIC_URL = "https://music.yandex.ru/"
MUSIC_ACTIONS = frozenset({"music_open", "music_play", "music_morning"})
MEDIA_STATES = frozenset({"playing", "already_playing", "no_session", "not_music", "unknown_source",
                          "not_playable", "rejected", "not_confirmed", "unavailable", "timeout"})

# This exact policy is also exercised with in-memory fake sessions in Windows
# tests; neither GetCurrentSession nor any live player is invoked by those tests.
_SMTC_POLICY = r"""
function Invoke-CurrentMusicPlay {
    param($Session, [ScriptBlock]$Await, [switch]$YandexOnly)
    if ($null -eq $Session) { return 'no_session' }
    $info = $Session.GetPlaybackInfo()
    if ([string]$info.PlaybackType -ne 'Music') { return 'not_music' }
    $allowed = @('Spotify.exe', 'SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify',
                 'YandexMusic.exe', 'Yandex.Music.exe', 'Яндекс Музыка.exe', 'ru.yandex.desktop.music',
                 'Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic')
    if ($YandexOnly) {
        $allowed = @('YandexMusic.exe', 'Yandex.Music.exe', 'Яндекс Музыка.exe', 'ru.yandex.desktop.music')
    }
    if ([string]$Session.SourceAppUserModelId -notin $allowed) { return 'unknown_source' }
    if ([string]$info.PlaybackStatus -eq 'Playing') { return 'already_playing' }
    if (-not $info.Controls.IsPlayEnabled) { return 'not_playable' }
    $accepted = & $Await ($Session.TryPlayAsync()) ([bool])
    if (-not $accepted) { return 'rejected' }
    for ($index = 0; $index -lt 10; $index++) {
        $observed = $Session.GetPlaybackInfo()
        if ([string]$observed.PlaybackType -ne 'Music') { return 'not_music' }
        if ([string]$observed.PlaybackStatus -eq 'Playing') { return 'playing' }
        Start-Sleep -Milliseconds 100
    }
    return 'not_confirmed'
}
"""

_SMTC_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
function Emit-State([string]$state) {
    @{protocol=1;state=$state} | ConvertTo-Json -Compress
}
""" + _SMTC_POLICY + r"""
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]
    $script:asTask = @([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
        $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })
    if ($script:asTask.Count -ne 1) { throw 'projection_unavailable' }
    function Wait-WinRT($operation, [Type]$resultType) {
        $task = $script:asTask[0].MakeGenericMethod($resultType).Invoke($null, @($operation))
        if (-not $task.Wait(1200)) {
            try { $operation.Cancel() } catch { }
            throw [TimeoutException]::new()
        }
        return $task.GetAwaiter().GetResult()
    }
    $manager = Wait-WinRT ($managerType::RequestAsync()) $managerType
    $current = $manager.GetCurrentSession()
    $state = Invoke-CurrentMusicPlay $current { param($operation, $resultType) Wait-WinRT $operation $resultType }
    Emit-State $state
} catch [TimeoutException] {
    Emit-State 'timeout'
} catch {
    Emit-State 'unavailable'
}
"""

_SMTC_SCRIPT_YANDEX = _SMTC_SCRIPT.replace(
    "$state = Invoke-CurrentMusicPlay $current { param($operation, $resultType) Wait-WinRT $operation $resultType }",
    "$state = Invoke-CurrentMusicPlay $current { param($operation, $resultType) Wait-WinRT $operation $resultType } -YandexOnly")


def _request_music_play(system_dir: Path, runner=None, *, timeout=6, yandex_only=False) -> str:
    """At most six seconds; outputs fixed state only, never media metadata.

    Windows API: https://learn.microsoft.com/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession.tryplayasync
    The API's boolean acknowledgment alone is insufficient: policy additionally
    observes the session's Music type and Playing status before claiming play.
    """
    run = subprocess.run if runner is None else runner
    executable = Path(system_dir) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if type(yandex_only) is not bool:
        raise ValueError("Player selection must be an explicit boolean")
    script = _SMTC_SCRIPT_YANDEX if yandex_only else _SMTC_SCRIPT
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = run([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-EncodedCommand", encoded],
                     shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                     env=powershell_environment(system_dir),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not isinstance(result.stdout, bytes) or len(result.stdout) > 4096:
            return "unavailable"
        data = json.loads(result.stdout.decode("utf-8-sig"))
        if (not isinstance(data, dict) or set(data) != {"protocol", "state"}
                or type(data["protocol"]) is not int or data["protocol"] != 1
                or not isinstance(data["state"], str) or data["state"] not in MEDIA_STATES):
            return "unavailable"
        return data["state"]
    except subprocess.TimeoutExpired:
        return "timeout"
    except (OSError, ValueError, TypeError):
        return "unavailable"


def _open_music_site(url):
    if url != MUSIC_URL or os.name != "nt":
        raise ValueError("Unsupported music launch")
    os.startfile(MUSIC_URL)


def _native_music_executable() -> Path | None:
    """Only the setup-verified executable pinned by this installed app.

    A repository/developer run never searches global settings for programs.
    Setup records the digest only after validating YANDEX LLC's signature.
    """
    try:
        module = Path(__file__).resolve()
        app = module.parent.parent
        if module.parent.name != "desktop" or app.name != "app":
            return None
        manifest = app.parent / "installation.json"
        if not manifest.is_file() or manifest.stat().st_size > 65536:
            return None
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("kind") != "isolated-source-runtime":
            return None
        raw, expected = data.get("yandexMusicExe"), data.get("yandexMusicSha256")
        if (not isinstance(raw, str) or not raw or "\x00" in raw or "://" in raw
                or raw.startswith(("\\\\", "//")) or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)):
            return None
        executable = Path(raw)
        allowed_names = {"yandexmusic.exe", "яндекс музыка.exe"}
        if not executable.is_absolute() or executable.name.casefold() not in allowed_names:
            return None
        executable = executable.resolve(strict=True)
        if (str(executable).startswith(("\\\\", "//")) or executable.name.casefold() not in allowed_names
                or not executable.is_file() or not 0 < executable.stat().st_size <= 512 * 1024 * 1024):
            return None
        with executable.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected.lower():
                return None
        return executable
    except (OSError, ValueError, TypeError):
        return None


def _require_current(guard):
    if guard is not None and (not callable(guard) or guard() is not True):
        raise PermissionError("Music request canceled")


def _open_player(opener, launcher, authorization_guard=None):
    _require_current(authorization_guard)
    executable = _native_music_executable()
    _require_current(authorization_guard)
    if executable is not None:
        launch = subprocess.Popen if launcher is None else launcher
        try:
            launch([str(executable)], shell=False, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(executable.parent))
            return "native"
        except OSError:
            pass
    opener(MUSIC_URL)
    return "browser"


def _confirmed_result(state):
    if state == "already_playing":
        return "Музыка уже играет."
    if state == "playing":
        return "Продолжил музыку."
    return None


def _start_morning_music(system_dir, *, runner, opener, launcher, authorization_guard=None):
    """Start the installed Yandex player, then try its music session only.

    This does not invent a random track or morning playlist. If no authorized
    music queue/session exists, opening the app/site remains an honest fallback.
    No blind keyboard shortcut or undocumented URL can toggle a different app.
    """
    opened = (_open_player(opener, launcher) if authorization_guard is None else
              _open_player(opener, launcher, authorization_guard))
    if opened == "native":
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _require_current(authorization_guard)
            state = _request_music_play(system_dir, runner=runner, timeout=remaining, yandex_only=True)
            if state in {"playing", "already_playing"}:
                return "Продолжил музыку в Яндекс Музыке." if state == "playing" else "Музыка в Яндекс Музыке уже играет."
            if state not in {"no_session", "not_music", "unknown_source", "not_playable"}:
                # A missing acknowledgement might follow a successful Play.
                # Never send another Play automatically after that ambiguity.
                break
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.25, remaining))
    return ("Яндекс Музыка открывается. " if opened == "native" else "Открываю сайт Яндекс Музыки. ") + (
        "Пока не могу подтвердить воспроизведение. Если потребуется, войди и нажми Play в плеере.")


def execute_music_action(action: str, system_dir: Path, *, runner=None, opener=None, launcher=None, authorization_guard=None) -> str:
    """Original typed/voice authorization is enforced by DesktopCore upstream."""
    if action not in MUSIC_ACTIONS:
        raise ValueError("Unsupported music action")
    _require_current(authorization_guard)
    open_site = _open_music_site if opener is None else opener
    if action == "music_morning":
        return _start_morning_music(system_dir, runner=runner, opener=open_site, launcher=launcher,
                                    authorization_guard=authorization_guard)
    if action == "music_play":
        state = _request_music_play(system_dir, runner=runner)
        confirmed = _confirmed_result(state)
        if confirmed:
            return confirmed
        # A video/unknown player is never controlled to satisfy a music request.
        # Opening a site cannot prove login, browser autoplay or audible output.
        opened = (_open_player(open_site, launcher) if authorization_guard is None else
                  _open_player(open_site, launcher, authorization_guard))
        safe_discovery_states = {"no_session", "not_music", "unknown_source", "not_playable"}
        if state in safe_discovery_states and opened == "native":
            # Cold native launch may register its session shortly afterward.
            # Retry discovery for at most 2.5s; each bridge still revalidates
            # source/Music type and never sends a toggle or controls video.
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _require_current(authorization_guard)
                state = _request_music_play(system_dir, runner=runner, timeout=remaining)
                confirmed = _confirmed_result(state)
                if confirmed:
                    return confirmed
                if state not in safe_discovery_states:
                    break
                time.sleep(min(0.15, max(0, deadline - time.monotonic())))
        opening = "Яндекс Музыка открывается." if opened == "native" else "Открываю сайт Яндекс Музыки."
        return (opening + " Пока не могу подтвердить воспроизведение. "
                "Если потребуется, войди и нажми Play в плеере.")
    opened = (_open_player(open_site, launcher) if authorization_guard is None else
              _open_player(open_site, launcher, authorization_guard))
    opening = "Яндекс Музыка открывается." if opened == "native" else "Открываю сайт Яндекс Музыки."
    return opening + " Если потребуется, войди и нажми Play в плеере."
