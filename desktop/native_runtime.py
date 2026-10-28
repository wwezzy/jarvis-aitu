"""Child Windows bridges use their own Windows modules, not an SDK's PowerShell."""
from __future__ import annotations

import os
from pathlib import Path


def powershell_environment(system_dir):
    allowed = {'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
               'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'TEMP', 'TMP', 'COMSPEC',
               'HOMEDRIVE', 'HOMEPATH', 'PROGRAMDATA', 'PROCESSOR_ARCHITECTURE'}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    system = Path(system_dir)
    shell = system / 'WindowsPowerShell/v1.0'
    result['PSModulePath'] = str(shell / 'Modules')
    result['PATH'] = os.pathsep.join(map(str, (system, shell, system.parent)))
    return result
