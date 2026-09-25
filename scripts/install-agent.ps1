param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $PythonPath) { $PythonPath = Join-Path $repoPath '.venv\Scripts\pythonw.exe' }
$pythonResolved = (Resolve-Path -LiteralPath $PythonPath).Path
$agentPath = (Resolve-Path -LiteralPath (Join-Path $repoPath 'agent.py')).Path
$action = New-ScheduledTaskAction -Execute $pythonResolved -Argument ('"' + $agentPath + '"') -WorkingDirectory $repoPath
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'JarvisPersonalAgent' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output 'Installed for the current user at next logon. Configure env vars before starting. Start-ScheduledTask JarvisPersonalAgent starts it now.'
