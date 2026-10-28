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
import queue
import threading
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
    param($Session, [ScriptBlock]$Await, [switch]$YandexOnly, [switch]$ObserveOnly)
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
    if ($ObserveOnly) { return 'not_confirmed' }
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

# Native UIA is a narrow fallback for the installed player's own My Vibe
# region. Its exact region/control names were observed in Yandex 5.124.1.
# The helper first reports readiness, then waits for a fresh parent-side
# authorization check. No track titles, account names or UI tree are emitted.
_YANDEX_UIA_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
function Emit-State([string]$state) {
    [Console]::Out.WriteLine('{"protocol":1,"state":"' + $state + '"}')
    [Console]::Out.Flush()
}
try {
    $raw = [Console]::In.ReadLine()
    if ($null -eq $raw -or $raw.Length -gt 4096) { throw 'invalid_config' }
    $config = $raw | ConvertFrom-Json
    if ((@($config.PSObject.Properties.Name | Sort-Object) -join ',') -ne 'executable,protocol,sha256' -or
        $config.protocol -ne 1 -or $config.sha256 -cnotmatch '^[a-f0-9]{64}$') { throw 'invalid_config' }
    $exe = [IO.Path]::GetFullPath([string]$config.executable)
    if (-not [IO.Path]::IsPathRooted($exe) -or $exe.StartsWith('\\') -or
        [IO.Path]::GetFileName($exe) -notin @('YandexMusic.exe','Яндекс Музыка.exe')) { throw 'invalid_app' }
    if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() -cne $config.sha256) { throw 'invalid_app' }
    $apps = @(Get-Process -Name 'YandexMusic','Яндекс Музыка' -ErrorAction SilentlyContinue | Where-Object {
        $_.MainWindowHandle -ne 0 -and $_.Path -ieq $exe
    })
    if ($apps.Count -ne 1) { Emit-State 'no_session'; exit 0 }
    $app = $apps[0]
    $ownedId, $ownedWindow = $app.Id, $app.MainWindowHandle
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [Windows.Automation.AutomationElement]::FromHandle([IntPtr]$ownedWindow)
    function Is-OwnedWindow {
        $current = Get-Process -Id $ownedId -ErrorAction Stop
        return $current.MainWindowHandle -eq $ownedWindow -and $current.Path -ieq $exe -and
               $root.Current.ProcessId -eq $ownedId
    }
    function Descendants($element, [string[]]$names, $type) {
        $items = $element.FindAll([Windows.Automation.TreeScope]::Descendants,
            [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ControlTypeProperty, $type))
        $matches = @()
        foreach ($item in $items) {
            if ($item.Current.Name -cin $names -and -not $item.Current.IsOffscreen) { $matches += $item }
        }
        return ,$matches
    }
    function Named-Regions($element, [string[]]$names) {
        $items = $element.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
        $matches = @()
        foreach ($item in $items) {
            if ($item.Current.Name -cin $names -and -not $item.Current.IsOffscreen -and
                $item.Current.ControlType -in @([Windows.Automation.ControlType]::Group,
                    [Windows.Automation.ControlType]::Custom,[Windows.Automation.ControlType]::Pane)) { $matches += $item }
        }
        return ,$matches
    }
    if (-not (Is-OwnedWindow)) { throw 'invalid_app' }
    $documents = @($root.FindAll([Windows.Automation.TreeScope]::Descendants,
        [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ControlTypeProperty,
            [Windows.Automation.ControlType]::Document)))
    if ($documents.Count -ne 1) { Emit-State 'not_playable'; exit 0 }
    $value = $documents[0].GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern)
    if (-not $value.Current.Value.StartsWith('music-application://desktop/',[StringComparison]::Ordinal)) {
        Emit-State 'unknown_source'; exit 0
    }
    $vibes = Named-Regions $documents[0] @('My Vibe','Моя волна')
    if ($vibes.Count -ne 1) { Emit-State 'not_music'; exit 0 }
    $players = Named-Regions $vibes[0] @('Player','Плеер')
    if ($players.Count -ne 1) { Emit-State 'not_playable'; exit 0 }
    $player = $players[0]
    $playerId = $player.GetRuntimeId() -join ','
    $previous = Descendants $player @('Previous song','Предыдущая песня') ([Windows.Automation.ControlType]::Button)
    $next = Descendants $player @('Next song','Следующая песня') ([Windows.Automation.ControlType]::Button)
    $seek = Descendants $player @('Manage time code','Управление таймкодом') ([Windows.Automation.ControlType]::Slider)
    if ($previous.Count -ne 1 -or $next.Count -ne 1 -or $seek.Count -ne 1) { Emit-State 'not_music'; exit 0 }
    $play = Descendants $player @('Playback','Воспроизведение') ([Windows.Automation.ControlType]::Button)
    $pause = Descendants $player @('Pause','Пауза') ([Windows.Automation.ControlType]::Button)
    $invoked = $false
    if ($pause.Count -eq 0 -and $play.Count -eq 1 -and $play[0].Current.IsEnabled) {
        $button = $play[0]
        $buttonId = $button.GetRuntimeId() -join ','
        $invoke = $button.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
        Emit-State 'ready'
        # Only the parent can issue this fixed go frame after checking current
        # owner/cancellation; UI content never supplies protocol instructions.
        if ([Console]::In.ReadLine() -cne '{"protocol":1,"action":"play"}') { throw 'canceled' }
        if (-not (Is-OwnedWindow) -or ($player.GetRuntimeId() -join ',') -cne $playerId -or
            ($button.GetRuntimeId() -join ',') -cne $buttonId -or -not $button.Current.IsEnabled -or
            $button.Current.Name -cnotin @('Playback','Воспроизведение')) { throw 'changed_control' }
        $invoke.Invoke()
        $invoked = $true
    } elseif ($pause.Count -ne 1 -or $play.Count -ne 0) {
        Emit-State 'not_playable'; exit 0
    }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $observed = $null
    while ($clock.Elapsed.TotalSeconds -lt 2.8) {
        if (-not (Is-OwnedWindow) -or ($player.GetRuntimeId() -join ',') -cne $playerId) { throw 'changed_control' }
        $pause = Descendants $player @('Pause','Пауза') ([Windows.Automation.ControlType]::Button)
        $seek = Descendants $player @('Manage time code','Управление таймкодом') ([Windows.Automation.ControlType]::Slider)
        if ($pause.Count -eq 1 -and $pause[0].Current.IsEnabled -and $seek.Count -eq 1 -and $seek[0].Current.IsEnabled) {
            $position = $seek[0].GetCurrentPattern([Windows.Automation.RangeValuePattern]::Pattern).Current.Value
            if (-not [double]::IsNaN($position) -and -not [double]::IsInfinity($position)) {
                if ($null -ne $observed -and $position -gt $observed + 0.25) {
                    if ($invoked) { Emit-State 'playing' } else { Emit-State 'already_playing' }
                    exit 0
                }
                if ($null -eq $observed) { $observed = $position }
            }
        }
        Start-Sleep -Milliseconds 100
    }
    Emit-State 'not_confirmed'
} catch {
    Emit-State 'unavailable'
}
"""


def _request_yandex_ui_play(system_dir, *, authorization_guard=None, timeout=6, process_factory=None):
    """One verified My Vibe Invoke, bounded/cancelable, with advancing readback.

    This owns only its helper process. It never presses a key, selects a track,
    changes a browser or emits names/account/media metadata.
    """
    _require_current(authorization_guard)
    identity = _native_music_identity()
    _require_current(authorization_guard)
    if identity is None:
        return "unavailable"
    executable, digest = identity
    _require_current(authorization_guard)
    encoded = base64.b64encode(_YANDEX_UIA_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = [str(Path(system_dir) / "WindowsPowerShell/v1.0/powershell.exe"),
               "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-EncodedCommand", encoded]
    start = subprocess.Popen if process_factory is None else process_factory
    process, reader, job = None, None, None
    frames = queue.Queue(maxsize=4)
    deadline = time.monotonic() + min(8, max(.1, timeout))
    from desktop.clone_speech import _OwnedJob

    def read_states():
        try:
            while True:
                raw = process.stdout.readline(4097)
                if not raw or len(raw) > 4096 or not raw.endswith(b"\n"):
                    frames.put_nowait(None)
                    return
                value = json.loads(raw)
                if (not isinstance(value, dict) or set(value) != {"protocol", "state"}
                        or type(value["protocol"]) is not int or value["protocol"] != 1
                        or value["state"] not in MEDIA_STATES | {"ready"}):
                    frames.put_nowait(None)
                    return
                frames.put_nowait(value["state"])
        except (OSError, ValueError, TypeError, queue.Full):
            try:
                frames.put_nowait(None)
            except queue.Full:
                pass

    try:
        process = start(command, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        env=powershell_environment(system_dir), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        job = _OwnedJob(process)
        config = {"protocol": 1, "executable": str(executable), "sha256": digest}
        process.stdin.write(json.dumps(config, ensure_ascii=False).encode("utf-8") + b"\n")
        process.stdin.flush()
        reader = threading.Thread(target=read_states, daemon=True, name="JarvisMusicReadback")
        reader.start()
        sent = False
        while time.monotonic() < deadline:
            _require_current(authorization_guard)
            try:
                state = frames.get(timeout=min(.05, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            _require_current(authorization_guard)
            if state == "ready":
                if sent:
                    return "unavailable"
                sent = True
                process.stdin.write(b'{"protocol":1,"action":"play"}\n')
                process.stdin.flush()
                continue
            if state == "playing" and not sent or state == "already_playing" and sent:
                return "unavailable"
            return state if state in MEDIA_STATES else "unavailable"
        return "timeout"
    except PermissionError:
        raise
    except (OSError, ValueError, TypeError):
        return "unavailable"
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for pipe in (process.stdin, process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
        if job is not None:
            job.close()
        if reader is not None:
            reader.join(1)


def _request_music_play(system_dir: Path, runner=None, *, timeout=6, yandex_only=False, observe_only=False) -> str:
    """At most six seconds; outputs fixed state only, never media metadata.

    Windows API: https://learn.microsoft.com/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession.tryplayasync
    The API's boolean acknowledgment alone is insufficient: policy additionally
    observes the session's Music type and Playing status before claiming play.
    """
    run = subprocess.run if runner is None else runner
    executable = Path(system_dir) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if type(yandex_only) is not bool or type(observe_only) is not bool:
        raise ValueError("Player selection must be an explicit boolean")
    script = _SMTC_SCRIPT_YANDEX if yandex_only else _SMTC_SCRIPT
    if observe_only:
        script = script.replace(
            "Wait-WinRT $operation $resultType }", "Wait-WinRT $operation $resultType } -ObserveOnly", 1)
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


def _native_music_identity() -> tuple[Path, str] | None:
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
        return executable, expected.lower()
    except (OSError, ValueError, TypeError):
        return None


def _native_music_executable() -> Path | None:
    identity = _native_music_identity()
    return identity[0] if identity is not None else None


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


def _try_native_vibe(system_dir, *, runner=None, authorization_guard=None):
    state = _request_yandex_ui_play(system_dir, authorization_guard=authorization_guard)
    if state not in {"playing", "already_playing"}:
        return None
    _require_current(authorization_guard)
    observed = _request_music_play(system_dir, runner=runner, timeout=1.5, yandex_only=True, observe_only=True)
    _require_current(authorization_guard)
    # A registered Yandex Music session explicitly still paused contradicts
    # UI feedback. Missing/other-app SMTC is why this narrow fallback exists.
    if observed == "not_confirmed":
        return None
    return "Музыка в Яндекс Музыке уже играет." if state == "already_playing" else "Включил музыку в Яндекс Музыке."


def _start_morning_music(system_dir, *, runner, opener, launcher, authorization_guard=None):
    """Start the installed Yandex player, then try its music session only.

    This does not invent a random track or morning playlist. If no authorized
    music queue/session exists, opening the app/site remains an honest fallback.
    No blind keyboard shortcut or undocumented URL can toggle a different app.
    """
    opened = (_open_player(opener, launcher) if authorization_guard is None else
              _open_player(opener, launcher, authorization_guard))
    if opened == "native":
        state = "no_session"
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
        if state in {"no_session", "not_music", "unknown_source", "not_playable"}:
            confirmed = _try_native_vibe(system_dir, runner=runner, authorization_guard=authorization_guard)
            if confirmed:
                return confirmed
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
            if state in safe_discovery_states:
                confirmed = _try_native_vibe(system_dir, runner=runner, authorization_guard=authorization_guard)
                if confirmed:
                    return confirmed
        opening = "Яндекс Музыка открывается." if opened == "native" else "Открываю сайт Яндекс Музыки."
        return (opening + " Пока не могу подтвердить воспроизведение. "
                "Если потребуется, войди и нажми Play в плеере.")
    opened = (_open_player(open_site, launcher) if authorization_guard is None else
              _open_player(open_site, launcher, authorization_guard))
    opening = "Яндекс Музыка открывается." if opened == "native" else "Открываю сайт Яндекс Музыки."
    return opening + " Если потребуется, войди и нажми Play в плеере."
