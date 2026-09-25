$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'JarvisPersonalAgent' -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName 'JarvisPersonalAgent' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'JarvisPersonalAgent' -Confirm:$false
}
Write-Output 'Agent startup removed. Replay database and logs remain in LOCALAPPDATA\Jarvis.'
