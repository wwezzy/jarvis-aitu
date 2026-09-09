$ErrorActionPreference = 'Stop'
Write-Host '=== Jarvis v2 setup ==='

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python launcher (py) not found. Install Python 3.11+ and enable Add Python to PATH.'
}

if (-not (Test-Path '.venv')) {
    py -3.11 -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env from .env.example. Fill BOT_TOKEN, ADMIN_ID, GEMINI_API_KEY and Redis values before starting.' -ForegroundColor Yellow
}

if (-not (Test-Path 'profile.local.md')) {
    Copy-Item 'profile.example.md' 'profile.local.md'
    Write-Host 'Created profile.local.md. Put private Jarvis context there.' -ForegroundColor Yellow
}

Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host 'Next: edit .env, then run .\run_jarvis.ps1'
