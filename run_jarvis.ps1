$ErrorActionPreference = 'Stop'
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    throw 'Virtual environment not found. Run .\setup_windows.ps1 first.'
}
if (-not (Test-Path '.env')) {
    throw '.env not found. Copy .env.example to .env and fill credentials.'
}
& .\.venv\Scripts\python.exe aihelper.py
