"""Named Chrome never becomes a path/argument supplied by a chat or model."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from desktop import known_apps


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    root = tmp_path / 'Programs'
    executable = root / 'Google/Chrome/Application/chrome.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'fixture only, never executed')
    for key in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ProgramFiles', str(root))
    return executable


def response(path, **changes):
    data = {'protocol': 1, 'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    data.update(changes)
    return SimpleNamespace(returncode=0, stdout=json.dumps(data).encode())


def test_named_browser_signature_probe_and_fixed_launch(chrome, tmp_path):
    calls = []
    def probe(arguments, **options):
        assert arguments[0] == str(tmp_path/'WindowsPowerShell/v1.0/powershell.exe')
        assert options['shell'] is False and options['timeout'] == 8
        assert options['env']['PSModulePath'] == str(tmp_path/'WindowsPowerShell/v1.0/Modules')
        assert '-EncodedCommand' in arguments and 'Google LLC' in known_apps._CHROME_PROBE
        return response(chrome)
    result = known_apps.open_chrome(tmp_path, runner=probe, launcher=lambda *args, **kw: calls.append((args, kw)))
    assert result == 'Открываю Google Chrome.'
    assert calls[0][0] == ([str(chrome)],) and calls[0][1]['shell'] is False


@pytest.mark.parametrize('changes', [{'protocol': True}, {'protocol': 2}, {'sha256': '0'*64},
    {'path': '//server/chrome.exe'}, {'path': 'https://example.com/chrome.exe'},
    {'path': 'chrome.exe'}, {'path': None}, {'extra': 'arbitrary arguments'}])
def test_invalid_probe_never_launches_anything(chrome, tmp_path, changes):
    assert known_apps.verified_chrome(tmp_path, runner=lambda *_a, **_kw: response(chrome, **changes)) is None
    assert 'по умолчанию' in known_apps.open_chrome(tmp_path, runner=lambda *_a, **_kw: response(chrome, **changes),
        launcher=lambda *_a, **_kw: pytest.fail('Unsafe program was launched'))


def test_even_matching_digest_outside_fixed_install_locations_is_rejected(chrome, tmp_path):
    unrelated = tmp_path/'chrome.exe'
    unrelated.write_bytes(chrome.read_bytes())
    assert known_apps.verified_chrome(tmp_path, runner=lambda *_a, **_kw: response(unrelated)) is None


def test_changed_binary_and_unavailable_signature_never_launch(chrome, tmp_path):
    original = response(chrome)
    chrome.write_bytes(b'replaced after signature probe')
    assert known_apps.verified_chrome(tmp_path, runner=lambda *_a, **_kw: original) is None
    assert known_apps.verified_chrome(tmp_path, runner=lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout=b'')) is None


def test_native_bridge_does_not_inherit_sdk_modules_or_provider_secrets(tmp_path, monkeypatch):
    from desktop.native_runtime import powershell_environment
    monkeypatch.setenv('PSModulePath', 'unrelated-sdk-modules')
    monkeypatch.setenv('UNRELATED_PROVIDER_TOKEN', 'synthetic-not-for-child')
    result = powershell_environment(tmp_path)
    assert result['PSModulePath'] == str(tmp_path/'WindowsPowerShell/v1.0/Modules')
    assert 'UNRELATED_PROVIDER_TOKEN' not in result
