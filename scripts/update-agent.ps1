param(
    [string]$PythonPath,
    [string]$PreviousAgentPath,
    [switch]$EnableMonitoring,
    [switch]$AllowOfflineMonitoring
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $PythonPath) { $PythonPath = Join-Path $repoPath '.venv\Scripts\python.exe' }
$pythonResolved = (Resolve-Path -LiteralPath $PythonPath).Path
$pythonw = Join-Path ([System.IO.Path]::GetDirectoryName($pythonResolved)) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe is required for hidden interactive startup.' }
if ($PreviousAgentPath) {
    $previousResolved = (Resolve-Path -LiteralPath $PreviousAgentPath).Path
    if ([System.IO.Path]::GetFileName($previousResolved) -ne 'agent.py') { throw 'PreviousAgentPath must name the identified agent.py script.' }
}
$statePath = Join-Path $env:LOCALAPPDATA 'Jarvis'
$configPath = Join-Path $statePath 'agent.env'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'Existing local agent.env is required. This updater does not request or generate credentials.' }

$reportText = & $pythonResolved (Join-Path $repoPath 'agent.py') --doctor
if ($LASTEXITCODE -ne 0) { throw 'Agent doctor could not start. Check the installed dependencies.' }
$report = ($reportText -join [Environment]::NewLine) | ConvertFrom-Json
if (-not $report.admin_configured -or -not $report.secret_configured) { throw 'The existing agent configuration is incomplete.' }
if (-not $report.redis_healthy -and -not ($EnableMonitoring -and $AllowOfflineMonitoring)) {
    throw ('Redis is unavailable: ' + $report.reason + '. Resolve networking first, or explicitly enable offline monitoring.')
}

# Stop only the named Jarvis task and an explicitly identified previous script.
$task = Get-ScheduledTask -TaskName 'JarvisPersonalAgent' -ErrorAction SilentlyContinue
if ($task) { Stop-ScheduledTask -TaskName 'JarvisPersonalAgent' -ErrorAction SilentlyContinue }
if ($PreviousAgentPath) {
    $previousPattern = '(?i)(?:"' + [regex]::Escape($previousResolved) + '"|(?<!\S)' + [regex]::Escape($previousResolved) + '(?!\S))'
    $processes = Get-CimInstance Win32_Process -Filter "name = 'pythonw.exe' OR name = 'python.exe'"
    foreach ($process in $processes) {
        if ($process.CommandLine -and $process.CommandLine -match $previousPattern) {
            Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        }
    }
}

if ($EnableMonitoring) {
    # Preserve existing credentials and the file's ACL. Never print its contents.
    $lines = @([System.IO.File]::ReadAllLines($configPath) | Where-Object { $_ -notmatch '^\s*PC_MONITOR_ENABLED\s*=' })
    $lines += 'PC_MONITOR_ENABLED=1'
    [System.IO.File]::WriteAllLines($configPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
}
& (Join-Path $PSScriptRoot 'install-agent.ps1') -PythonPath $pythonw
Start-ScheduledTask -TaskName 'JarvisPersonalAgent'
if ($report.redis_healthy) { Write-Output 'Updated the local agent using its existing configuration. Verify /pc status.' }
else { Write-Output ('Local observation enabled; command transport remains unavailable: ' + $report.reason + '. No power command was sent.') }
