param(
    [string]$InstallRoot,
    [string]$PythonPath,
    [string]$ModelDir,
    [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $PythonPath) { $PythonPath = Join-Path $repoPath '.venv\Scripts\python.exe' }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not $InstallRoot) { $InstallRoot = Join-Path $env:USERPROFILE 'Applications\JarvisDesktopLocal' }
$installPath = [IO.Path]::GetFullPath($InstallRoot)
if ((Test-Path -LiteralPath $installPath) -and (Get-ChildItem -LiteralPath $installPath -Force | Select-Object -First 1)) {
    throw 'The destination is not empty. Choose a new versioned installation directory; existing files are never deleted.'
}
$basePath = (& $PythonPath -I -c 'import sys; print(sys.base_prefix)').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $basePath 'pythonw.exe'))) {
    throw 'A complete existing Windows CPython runtime is required.'
}
$signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $basePath 'pythonw.exe')
if ($signature.Status -ne 'Valid') {
    throw 'The existing Python windowed runtime does not have a valid Authenticode signature. No security settings have been changed.'
}
$runtimePath = Join-Path $installPath 'runtime'
$appPath = Join-Path $installPath 'app'
$sitePath = Join-Path $installPath 'appsite'
New-Item -ItemType Directory -Path $runtimePath, $appPath, $sitePath -Force | Out-Null

# Copy only the installed interpreter and its standard library, never its
# third-party site-packages, scripts, credentials or unrelated SDK libraries.
foreach ($file in @('python.exe', 'pythonw.exe', 'python3.dll', 'LICENSE.txt')) {
    Copy-Item -LiteralPath (Join-Path $basePath $file) -Destination $runtimePath
}
Get-ChildItem -LiteralPath $basePath -File | Where-Object { $_.Name -match '^python\d+\.dll$|^vcruntime[\w.]*\.dll$' } |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $runtimePath }
Copy-Item -LiteralPath (Join-Path $basePath 'DLLs') -Destination $runtimePath -Recurse
$libPath = Join-Path $runtimePath 'Lib'
New-Item -ItemType Directory -Path $libPath -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $basePath 'Lib') -Force |
    Where-Object { $_.Name -notin @('site-packages', '__pycache__', 'test', 'idlelib', 'turtledemo', 'tkinter') } |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $libPath -Recurse }
foreach ($file in @('python.exe', 'pythonw.exe')) {
    $original = (Get-FileHash -LiteralPath (Join-Path $basePath $file) -Algorithm SHA256).Hash
    $copied = (Get-FileHash -LiteralPath (Join-Path $runtimePath $file) -Algorithm SHA256).Hash
    if ($original -ne $copied -or (Get-AuthenticodeSignature -LiteralPath (Join-Path $runtimePath $file)).Status -ne 'Valid') {
        throw 'The copied interpreter identity could not be verified.'
    }
}

$packageArguments = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile',
    '--index-url', 'https://pypi.org/simple', '--target', $sitePath,
    'PySide6-Essentials==6.12.0', 'vosk==0.3.45', 'sounddevice==0.5.6', 'pywin32==312')
& $PythonPath @packageArguments
if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency installation failed; no shortcut was created.' }
Copy-Item -LiteralPath (Join-Path $repoPath 'desktop_app.py') -Destination $appPath
$desktopPath = Join-Path $appPath 'desktop'
New-Item -ItemType Directory -Path $desktopPath -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $repoPath 'desktop') -File -Filter '*.py' |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $desktopPath }
Copy-Item -LiteralPath (Join-Path $repoPath 'desktop\assets') -Destination $desktopPath -Recurse
Copy-Item -LiteralPath (Join-Path $repoPath 'desktop\NOTICE.txt') -Destination $desktopPath
Copy-Item -LiteralPath (Join-Path $repoPath 'desktop\licenses') -Destination $desktopPath -Recurse
if (-not $ModelDir) {
    Push-Location $repoPath
    try { $ModelDir = (& $PythonPath -c 'from desktop.model_setup import default_model_dir; print(default_model_dir())').Trim() }
    finally { Pop-Location }
}
if (-not $ModelDir -or -not (Test-Path -LiteralPath $ModelDir)) {
    throw 'Install the official offline model first or pass its public model directory.'
}
foreach ($file in @('am\final.mdl', 'conf\model.conf', 'conf\mfcc.conf', 'graph\Gr.fst', 'graph\HCLr.fst')) {
    if (-not (Test-Path -LiteralPath (Join-Path $ModelDir $file))) { throw 'The public recognition model is incomplete.' }
}
$modelRoot = Join-Path $appPath 'models'
New-Item -ItemType Directory -Path $modelRoot -Force | Out-Null
Copy-Item -LiteralPath $ModelDir -Destination (Join-Path $modelRoot 'vosk-model-small-ru-0.22') -Recurse
$forbidden = Get-ChildItem -LiteralPath $appPath -Recurse -File -Force | Where-Object {
    $_.Name -match '^\.env($|\.)|^agent\.env$|\.(db|sqlite|sqlite3|dump|key)$'
}
if ($forbidden) { throw 'Private data was found in application sources; shortcut creation refused.' }

$launcher = @'
"""Private local launcher: isolated signed CPython, explicit app dependencies."""
import os
import runpy
import site
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
PACKAGES = ROOT / "appsite"
os.environ["PYTHONNOUSERSITE"] = "1"
# Keep this process independent of unrelated SDK/Poppler DLL directories.
# This changes only the child application's environment, not Windows settings.
windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
os.environ["PATH"] = os.pathsep.join(str(path) for path in
    (ROOT / "runtime", ROOT / "runtime" / "DLLs", windows / "System32", windows))
site.addsitedir(str(PACKAGES))
for relative in ("win32", "win32/lib", "Pythonwin"):
    candidate = PACKAGES / relative
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))
_dll_handles = []
if os.name == "nt":
    for candidate in (PACKAGES / "pywin32_system32", PACKAGES / "win32", PACKAGES / "PySide6"):
        if candidate.is_dir():
            _dll_handles.append(os.add_dll_directory(str(candidate)))
sys.path.insert(0, str(APP))
runpy.run_path(str(APP / "desktop_app.py"), run_name="__main__")
'@
$launcherPath = Join-Path $installPath 'launcher.pyw'
[IO.File]::WriteAllText($launcherPath, $launcher, [Text.UTF8Encoding]::new($false))
$manifest = @{
    version = '4.2.0'; kind = 'isolated-source-runtime'; sourceCommit = (& git -C $repoPath rev-parse HEAD).Trim()
    runtimeSigner = $signature.SignerCertificate.Subject
    pythonwSha256 = (Get-FileHash -LiteralPath (Join-Path $runtimePath 'pythonw.exe') -Algorithm SHA256).Hash
    personalDataBundled = $false; cloudCredentialsBundled = $false
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installPath 'installation.json') -Encoding UTF8

# Verify actual copied interpreter, native voice modules/model and UI before
# putting a launch shortcut in front of the user. These probes never capture
# microphone audio, speak, load personal data or execute PC actions.
$diagnosticPath = Join-Path $installPath 'diagnostics'
New-Item -ItemType Directory -Path $diagnosticPath -Force | Out-Null
$consolePython = Join-Path $runtimePath 'python.exe'
& $consolePython -I $launcherPath --diagnose-voice --report (Join-Path $diagnosticPath 'voice.json')
if ($LASTEXITCODE -ne 0) { throw 'The installed voice runtime did not pass diagnosis.' }
& $consolePython -I $launcherPath --smoke-test --report (Join-Path $diagnosticPath 'ui.json') --screenshot (Join-Path $diagnosticPath 'ui.png')
if ($LASTEXITCODE -ne 0) { throw 'The installed UI did not pass its isolated smoke test.' }
if (-not $NoShortcut) {
    $programs = [Environment]::GetFolderPath('Programs')
    $shortcutPath = Join-Path $programs 'JARVIS Desktop.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $runtimePath 'pythonw.exe'
    $shortcut.Arguments = '-I "' + $launcherPath + '"'
    $shortcut.WorkingDirectory = $appPath
    $shortcut.IconLocation = Join-Path $desktopPath 'assets\jarvis.ico'
    $shortcut.Description = 'JARVIS — локальный голосовой ассистент'
    $shortcut.Save()
    Write-Output ('Start Menu shortcut: ' + $shortcutPath)
    $downloadPath = Join-Path $env:USERPROFILE 'Downloads\Jarvis-Desktop-4.2.0'
    New-Item -ItemType Directory -Path $downloadPath -Force | Out-Null
    Copy-Item -LiteralPath $shortcutPath -Destination (Join-Path $downloadPath 'JARVIS.lnk') -Force
    Write-Output ('Ready launch shortcut: ' + (Join-Path $downloadPath 'JARVIS.lnk'))
}
Write-Output ('Installed verified local application: ' + $installPath)
