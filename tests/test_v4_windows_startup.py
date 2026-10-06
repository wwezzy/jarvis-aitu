"""Exercise actual PowerShell installers with task cmdlets replaced by fakes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest

import agent

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows startup registration contract')
ROOT = Path(__file__).resolve().parents[1]


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_installer_registers_only_limited_interactive_current_user_task():
    script = r'''
$ErrorActionPreference = 'Stop'
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) return @{exe=$Execute;args=$Argument;cwd=$WorkingDirectory} }
function New-ScheduledTaskTrigger { param([switch]$AtLogOn,$User) return @{logon=[bool]$AtLogOn;user=$User} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) return @{user=$UserId;logon=$LogonType;level=$RunLevel} }
function New-ScheduledTaskSettingsSet { param($MultipleInstances,$ExecutionTimeLimit,[switch]$StartWhenAvailable,$RestartCount,$RestartInterval,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries) return @{instances=$MultipleInstances;restarts=$RestartCount} }
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Principal,$Settings,[switch]$Force) $global:jarvisTestRegistration=@{name=$TaskName;action=$Action;trigger=$Trigger;principal=$Principal;settings=$Settings} }
'''
    script += f"\n& {ps_quote(ROOT / 'scripts/install-agent.ps1')} -PythonPath {ps_quote(sys.executable)}\n"
    script += '$global:jarvisTestRegistration | ConvertTo-Json -Depth 5 -Compress'
    response = subprocess.run([shutil.which('pwsh') or 'powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(response.stdout.strip().splitlines()[-1])
    assert data['name'] == 'JarvisPersonalAgent'
    assert data['principal']['level'] == 'Limited' and data['principal']['logon'] == 'Interactive'
    assert data['trigger']['logon'] and data['settings']['instances'] == 'IgnoreNew'
    assert Path(data['action']['exe']) == Path(sys.executable)
    assert data['action']['args'] == f'"{ROOT / "agent.py"}"'
    assert Path(data['action']['cwd']) == ROOT


def test_uninstaller_stops_and_removes_only_its_task():
    script = r'''
function Get-ScheduledTask { param($TaskName,$ErrorAction) return @{name=$TaskName} }
function Stop-ScheduledTask { param($TaskName,$ErrorAction) $global:jarvisTestStopped=$TaskName }
function Unregister-ScheduledTask { param($TaskName,[bool]$Confirm) $global:jarvisTestRemoved=$TaskName }
'''
    script += f"\n& {ps_quote(ROOT / 'scripts/uninstall-agent.ps1')}\n"
    script += '@{stopped=$global:jarvisTestStopped;removed=$global:jarvisTestRemoved} | ConvertTo-Json -Compress'
    response = subprocess.run([shutil.which('pwsh') or 'powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(response.stdout.strip().splitlines()[-1]) == {'stopped': 'JarvisPersonalAgent', 'removed': 'JarvisPersonalAgent'}


def test_updater_preserves_configuration_and_targets_only_identified_agent(tmp_path):
    state = tmp_path / 'Jarvis'
    state.mkdir()
    config = state / 'agent.env'
    original = 'ADMIN_ID=42\nPC_AGENT_SECRET=offline-test-only\nPC_MONITOR_ENABLED=0\n'
    config.write_text(original, encoding='utf-8')
    previous = tmp_path / 'old' / 'agent.py'
    previous.parent.mkdir()
    previous.touch()
    script = r'''
$ErrorActionPreference = 'Stop'
$global:jarvisTestPids = @()
function Get-ScheduledTask { param($TaskName,$ErrorAction) return @{name=$TaskName} }
function Stop-ScheduledTask { param($TaskName,$ErrorAction) $global:jarvisTestStopped=$TaskName }
function Stop-Process { param($Id,$ErrorAction) $global:jarvisTestPids += $Id }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) return @{exe=$Execute;args=$Argument;cwd=$WorkingDirectory} }
function New-ScheduledTaskTrigger { param([switch]$AtLogOn,$User) return @{} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) return @{level=$RunLevel} }
function New-ScheduledTaskSettingsSet { param($MultipleInstances,$ExecutionTimeLimit,[switch]$StartWhenAvailable,$RestartCount,$RestartInterval,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries) return @{} }
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Principal,$Settings,[switch]$Force) $global:jarvisTestRegistration=@{name=$TaskName;action=$Action;principal=$Principal} }
function Start-ScheduledTask { param($TaskName) $global:jarvisTestStarted=$TaskName }
'''
    script += '\nfunction Get-CimInstance { param($ClassName,$Filter) return @(' + (
        '@{ProcessId=111;CommandLine=' + ps_quote(f'pythonw.exe "{previous}"') + '},'
        '@{ProcessId=222;CommandLine=\'pythonw.exe "C:\\foreign\\agent.py"\'}) }\n')
    script += f"& {ps_quote(ROOT / 'scripts/update-agent.ps1')} -PythonPath {ps_quote(sys.executable)} -PreviousAgentPath {ps_quote(previous)} -EnableMonitoring -AllowOfflineMonitoring\n"
    script += '@{pids=$global:jarvisTestPids;started=$global:jarvisTestStarted;stopped=$global:jarvisTestStopped;registration=$global:jarvisTestRegistration} | ConvertTo-Json -Depth 5 -Compress'
    environment = {**os.environ, 'LOCALAPPDATA': str(tmp_path), 'PC_AGENT_SECRET': 'offline-test-only'}
    response = subprocess.run([shutil.which('pwsh') or 'powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        env=environment, capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(response.stdout.strip().splitlines()[-1])
    assert data['pids'] == [111]
    assert data['started'] == data['stopped'] == 'JarvisPersonalAgent'
    assert data['registration']['principal']['level'] == 'Limited'
    assert Path(data['registration']['action']['cwd']) == ROOT
    assert config.read_text(encoding='utf-8') == original.replace('PC_MONITOR_ENABLED=0', 'PC_MONITOR_ENABLED=1')
    assert 'offline-test-only' not in response.stdout + response.stderr


def test_agent_reconnect_backoff_resets_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_ID', '42')
    monkeypatch.setenv('PC_AGENT_SECRET', 'offline-test-only')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(agent, 'load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, 'create_redis', lambda **kwargs: Mock())
    runtime = Mock(tick=Mock(side_effect=[TimeoutError(), TimeoutError(), None]))
    monkeypatch.setattr(agent, 'Agent', Mock(return_value=runtime))
    waits = []
    def pause(value):
        waits.append(value)
        if len(waits) == 3:
            raise KeyboardInterrupt()
    monkeypatch.setattr(agent.time, 'sleep', pause)
    with pytest.raises(KeyboardInterrupt):
        agent.start_agent()
    assert waits == [4, 8, 2]
    assert (tmp_path / 'Jarvis' / 'agent.log').exists()
    agent.SingleInstance(tmp_path / 'Jarvis' / 'agent.lock').close()
