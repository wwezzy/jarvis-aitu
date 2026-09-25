param(
    [string]$RedisUrl,
    [int]$AdminId = 0
)

$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'Jarvis PC Agent setup must run on Windows.'
}

if (-not $RedisUrl) {
    $secureUrl = Read-Host 'Paste the EXTERNAL Render Redis URL' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureUrl)
    try {
        $RedisUrl = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if ($RedisUrl -notmatch '^rediss?://') {
    throw 'RedisUrl must start with redis:// or rediss://'
}

if ($AdminId -le 0) {
    $AdminId = [int](Read-Host 'Telegram ADMIN_ID')
}
if ($AdminId -le 0) {
    throw 'ADMIN_ID must be a positive integer.'
}

$repoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPath = Join-Path $repoPath '.venv'
$pythonPath = Join-Path $venvPath 'Scripts\python.exe'
$pythonwPath = Join-Path $venvPath 'Scripts\pythonw.exe'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) {
        throw 'Python launcher (py.exe) was not found. Install Python 3.12, then run this script again.'
    }

    & $py.Source -3.12 -m venv $venvPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath)) {
        throw 'Python 3.12 is not installed. Run: winget install -e --id Python.Python.3.12 ; then reopen PowerShell and run this setup again.'
    }
}

& $pythonPath -m pip install --upgrade pip
& $pythonPath -m pip install -r (Join-Path $repoPath 'requirements.txt')

$stateDir = Join-Path $env:LOCALAPPDATA 'Jarvis'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$envPath = Join-Path $stateDir 'agent.env'

$secret = $null
if (Test-Path -LiteralPath $envPath) {
    foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
        if ($line -like 'PC_AGENT_SECRET=*') {
            $secret = $line.Substring('PC_AGENT_SECRET='.Length).Trim()
            break
        }
    }
}

if (-not $secret) {
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    $secret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

$content = @(
    "ADMIN_ID=$AdminId",
    "PC_AGENT_SECRET=$secret",
    "REDIS_URL=$RedisUrl"
) -join [Environment]::NewLine

[System.IO.File]::WriteAllText(
    $envPath,
    $content + [Environment]::NewLine,
    (New-Object System.Text.UTF8Encoding($false))
)

$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $envPath /inheritance:r | Out-Null
& icacls $envPath /grant:r "${account}:(R,W)" "SYSTEM:(F)" | Out-Null

if (Get-Command Set-Clipboard -ErrorAction SilentlyContinue) {
    Set-Clipboard -Value $secret
} else {
    $secret | clip.exe
}

& (Join-Path $PSScriptRoot 'install-agent.ps1') -PythonPath $pythonwPath

try {
    Start-ScheduledTask -TaskName 'JarvisPersonalAgent'
} catch {
    Write-Warning 'Scheduled task was installed but could not be started immediately. It will start at next logon.'
}

Write-Output ''
Write-Output 'Jarvis PC Agent is installed for the current Windows user.'
Write-Output 'PC_AGENT_SECRET was copied to the clipboard. Paste it into the STAGING Render service as PC_AGENT_SECRET.'
Write-Output 'Do not paste the secret into GitHub, logs, or screenshots.'
Write-Output 'After the Render redeploy is live, use /pc status in the staging bot.'
