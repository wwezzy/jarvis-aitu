param(
    [string]$PythonPath,
    [string]$ModelDir,
    [string]$OutputDir,
    [string]$IsccPath,
    [switch]$PortableOnly
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not $PythonPath) { $PythonPath = Join-Path $repoPath '.venv\Scripts\python.exe' }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not $OutputDir) { $OutputDir = Join-Path $repoPath 'dist\desktop' }
$outputPath = [IO.Path]::GetFullPath($OutputDir)
$workPath = Join-Path $outputPath 'build'
$distPath = Join-Path $outputPath 'portable'
$specPath = Join-Path $outputPath 'spec'
$entrypoint = Join-Path $repoPath 'desktop_app.py'
if (-not (Test-Path -LiteralPath $entrypoint)) { throw 'desktop_app.py was not found.' }

$arguments = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--noupx', '--onedir', '--windowed',
    '--name', 'JarvisDesktop', '--distpath', $distPath, '--workpath', $workPath,
    '--specpath', $specPath, '--paths', $repoPath,
    '--collect-all', 'vosk', '--collect-all', '_sounddevice_data',
    '--collect-data', 'piper', '--collect-binaries', 'piper',
    '--hidden-import', 'piper.voice', '--hidden-import', 'piper.espeakbridge',
    '--collect-all', 'onnxruntime',
    '--collect-all', 'ctranslate2', '--collect-all', 'tokenizers',
    '--hidden-import', 'sounddevice', '--hidden-import', 'pythoncom', '--hidden-import', 'win32com.client')
$fontPath = Join-Path $PSScriptRoot 'assets\Inter.ttf'
$fontNotice = Join-Path $PSScriptRoot 'assets\OFL-Inter.txt'
if (-not (Test-Path -LiteralPath $fontPath) -or -not (Test-Path -LiteralPath $fontNotice)) {
    throw 'The bundled Inter font and its license are required.'
}
$arguments += @('--add-data', ($fontPath + ';desktop/assets'), '--add-data', ($fontNotice + ';desktop/assets'),
                '--add-data', ((Join-Path $PSScriptRoot 'NOTICE.txt') + ';desktop'))
$arguments += @('--add-data', ((Join-Path $PSScriptRoot 'phone_web') + ';desktop/phone_web'),
                '--add-data', ((Join-Path $repoPath 'HANDSFREE.md') + ';.'))
$arguments += @('--icon', (Join-Path $PSScriptRoot 'assets\jarvis.ico'))
$licensePath = Join-Path $outputPath 'licenses'
$licenseCode = @'
import importlib.metadata as metadata
import pathlib, shutil, sys
root = pathlib.Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
packages = ['PySide6-Essentials', 'shiboken6', 'vosk', 'sounddevice', 'pywin32',
            'cffi', 'pycparser', 'requests', 'certifi', 'charset-normalizer',
            'idna', 'urllib3', 'srt', 'tqdm', 'websockets', 'pyinstaller',
            'piper-tts', 'onnxruntime', 'numpy', 'pathvalidate', 'flatbuffers', 'packaging',
            'ctranslate2', 'tokenizers', 'huggingface-hub', 'PyYAML']
for package in packages:
    try:
        distribution = metadata.distribution(package)
    except metadata.PackageNotFoundError:
        continue
    for index, file in enumerate(distribution.files or []):
        if not file.name.lower().startswith(('license', 'licence', 'copying', 'notice')):
            continue
        source = distribution.locate_file(file).resolve()
        if not source.is_file() or source.stat().st_size > 1024 * 1024:
            continue
        destination = root / package / (str(index) + '-' + file.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
print('Public dependency license notices collected.')
'@
$licenseCode | & $PythonPath - $licensePath
if ($LASTEXITCODE -ne 0) { throw 'Dependency license collection failed.' }
$arguments += @('--add-data', ($licensePath + ';licenses'),
                '--add-data', ((Join-Path $PSScriptRoot 'licenses') + ';licenses/source-licenses'))
$versionPath = Join-Path $outputPath 'version-info.txt'
@'
VSVersionInfo(
  ffi=FixedFileInfo(filevers=(4,3,0,0), prodvers=(4,3,0,0), mask=0x3f, flags=0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName','Jarvis personal project'),
    StringStruct('FileDescription','Jarvis local desktop assistant'),
    StringStruct('FileVersion','4.3.0.0'),
    StringStruct('InternalName','JarvisDesktop'),
    StringStruct('OriginalFilename','JarvisDesktop.exe'),
    StringStruct('ProductName','Jarvis Desktop'),
    StringStruct('ProductVersion','4.3.0')])]),
    VarFileInfo([VarStruct('Translation',[1033,1200])])])
'@ | Set-Content -LiteralPath $versionPath -Encoding utf8
$arguments += @('--version-file', $versionPath)

# The sole optional data directory is the public recognition model. Never
# collect the repository, LOCALAPPDATA, env files, databases or user material.
if (-not $ModelDir) {
    $ModelDir = Join-Path $env:LOCALAPPDATA 'JarvisDesktop\models\vosk-model-small-ru-0.22'
}
if (Test-Path -LiteralPath $ModelDir) {
    $modelPath = (Resolve-Path -LiteralPath $ModelDir).Path
    foreach ($modelFile in @('am\final.mdl', 'conf\model.conf', 'graph\Gr.fst', 'graph\HCLr.fst')) {
        if (-not (Test-Path -LiteralPath (Join-Path $modelPath $modelFile))) { throw 'The supplied model is incomplete.' }
    }
    $arguments += @('--add-data', ($modelPath + ';models/vosk-model-small-ru-0.22'))
}
$arguments += $entrypoint
$taskPreviousBuildPath = $env:PATH
# Resolve Windows system dependencies from Windows, not unrelated developer
# tools (e.g. Poppler's version-suffixed ICU exports break Qt's system ICU ABI).
$env:PATH = @((Split-Path -Parent $PythonPath), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join [IO.Path]::PathSeparator
Push-Location $repoPath
try {
    & $PythonPath @arguments
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} finally {
    Pop-Location
    $env:PATH = $taskPreviousBuildPath
}

$bundlePath = Join-Path $distPath 'JarvisDesktop'
if (-not (Test-Path -LiteralPath (Join-Path $bundlePath 'JarvisDesktop.exe'))) { throw 'Built executable not found.' }
$cpuBundleRoot = [IO.Path]::GetFullPath($bundlePath) + [IO.Path]::DirectorySeparatorChar
Get-ChildItem -LiteralPath $bundlePath -Recurse -File -Filter 'cudnn64_9.dll' | ForEach-Object {
    $optionalPath = [IO.Path]::GetFullPath($_.FullName)
    if (-not $optionalPath.StartsWith($cpuBundleRoot, [StringComparison]::OrdinalIgnoreCase) -or
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Unexpected generated optional CUDA dependency.' }
    Remove-Item -LiteralPath $optionalPath
}
# Qt uses the ICU API provided by supported Windows versions. Do not ship a
# foreign development-tool ICU or redistribute a copy of the OS component.
$taskBundleInternal = [IO.Path]::GetFullPath((Join-Path $bundlePath '_internal'))
foreach ($taskIcuName in @('icuuc.dll', 'icuin.dll', 'icudt.dll')) {
    $taskIcuPath = [IO.Path]::GetFullPath((Join-Path $taskBundleInternal $taskIcuName))
    if ([IO.Path]::GetDirectoryName($taskIcuPath) -ne $taskBundleInternal) { throw 'Unexpected generated ICU path.' }
    if (Test-Path -LiteralPath $taskIcuPath) { Remove-Item -LiteralPath $taskIcuPath }
}
$forbidden = Get-ChildItem -LiteralPath $bundlePath -File -Recurse -Force | Where-Object {
    ($_.Name -match '^\.env($|\.)|^agent\.env$|\.(db|sqlite|sqlite3|dump|key)$') -or
    ($_.Name -match '\.pem$' -and $_.Name -ne 'cacert.pem')
}
if ($forbidden) { throw 'A private/configuration file was found in the bundle; installer creation refused.' }
if ($PortableOnly) {
    Write-Output ('Portable bundle: ' + $bundlePath)
    return
}
if (-not $IsccPath) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 7\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
    )
    $IsccPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $IsccPath) { throw 'Portable build succeeded. Install official Inno Setup or pass -IsccPath to build the per-user installer.' }
& $IsccPath ('/DBundleDir=' + $bundlePath) ('/DOutputDir=' + $outputPath) (Join-Path $PSScriptRoot 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
$installer = Join-Path $outputPath 'JarvisDesktop-Setup-4.3.0.exe'
if (-not (Test-Path -LiteralPath $installer)) { throw 'Installer output not found.' }
Get-FileHash -LiteralPath $installer -Algorithm SHA256 | Select-Object Algorithm, Hash, Path
