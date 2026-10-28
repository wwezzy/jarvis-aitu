"""Launch a named installed browser; never accepts a user path or arguments."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from desktop.native_runtime import powershell_environment


_CHROME_PROBE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)
foreach ($root in $roots) {
    if (-not $root) { continue }
    $path = Join-Path $root 'Google\Chrome\Application\chrome.exe'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $signature = Get-AuthenticodeSignature -LiteralPath $path
    if ($signature.Status -ne 'Valid') { continue }
    $publisher = $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    if ($publisher -notin @('Google LLC', 'Google Inc')) { continue }
    @{protocol=1;path=$path;sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash} | ConvertTo-Json -Compress
    exit 0
}
@{protocol=1;path=$null;sha256=$null} | ConvertTo-Json -Compress
"""


def _chrome_candidates():
    roots = [os.environ.get(key) for key in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA')]
    return {Path(root).resolve() / 'Google/Chrome/Application/chrome.exe' for root in roots if root}


def verified_chrome(system_dir, *, runner=None):
    """Fixed official locations and a valid Google signature, rechecked after updates."""
    try:
        executable = Path(system_dir) / 'WindowsPowerShell/v1.0/powershell.exe'
        encoded = base64.b64encode(_CHROME_PROBE.encode('utf-16-le')).decode('ascii')
        result = (runner or subprocess.run)(
            [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
            env=powershell_environment(system_dir),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not isinstance(result.stdout, bytes) or len(result.stdout) > 8192:
            return None
        data = json.loads(result.stdout.decode('utf-8-sig'))
        if not isinstance(data, dict) or set(data) != {'protocol', 'path', 'sha256'} or type(data['protocol']) is not int or data['protocol'] != 1:
            return None
        raw, expected = data['path'], data['sha256']
        if (not isinstance(raw, str) or '\x00' in raw or '://' in raw or raw.startswith(('\\\\', '//'))
                or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected)):
            return None
        path = Path(raw)
        if not path.is_absolute() or path not in _chrome_candidates():
            return None
        path = path.resolve(strict=True)
        if not path.is_file() or path.name.casefold() != 'chrome.exe' or not 0 < path.stat().st_size <= 512 * 1024 * 1024:
            return None
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected.lower():
                return None
        return path
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return None


def open_chrome(system_dir, *, runner=None, launcher=None):
    executable = verified_chrome(system_dir, runner=runner)
    if executable is None:
        return 'Не нашёл проверенную установку Google Chrome. Можно сказать «открой браузер» для браузера по умолчанию.'
    (launcher or subprocess.Popen)([str(executable)], shell=False, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(executable.parent))
    return 'Открываю Google Chrome.'
