"""Synthetic credentials/private temp profiles only; no provider or user files."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop import gemini_ai, gemini_mode


KEY = "AQ." + "SYNTHETIC_NEVER_REAL_0123456789abcdefghijklmnopqrstuv"
OTHER = "AIzaSYNTHETIC_NEVER_REAL_0123456789abcdefghijklmnopqrstuv"


def setup(tmp_path):
    state = tmp_path / "private"
    state.mkdir()
    result = gemini_mode.save_existing_key(state, KEY, "synthetic-free-project", free_project_verified=True)
    preferences = {"gemini_enabled": True, "free_mode": True, "voice_engine": "clone"}
    environ = gemini_mode.GeminiEnvironment(state, lambda: dict(preferences), environ={})
    return SimpleNamespace(state=state, prefs=preferences, env=environ, result=result)


def test_disabled_preferences_never_read_key_files_or_process_credentials(tmp_path, monkeypatch):
    class Forbidden(dict):
        def get(self, *_args):
            pytest.fail("Disabled Gemini read process credentials")
    def forbidden(*_args, **_kwargs):
        pytest.fail("Disabled Gemini read credential files")
    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    env = gemini_mode.GeminiEnvironment(tmp_path, lambda: {"gemini_enabled": False}, environ=Forbidden())
    assert env.get("GEMINI_API_KEY") is None
    assert env["JARVIS_GEMINI_ENABLED"] == "0"
    assert env["JARVIS_GEMINI_SPEECH_ENABLED"] == "0"
    assert env.get("OPENAI_API_KEY") is None


def test_process_key_priority_and_live_opt_out_without_file_discovery(tmp_path, monkeypatch):
    preferences, raw = {"gemini_enabled": True}, {"GEMINI_API_KEY": KEY}
    def forbidden(*_args, **_kwargs):
        pytest.fail("Explicit process credential searched private files")
    monkeypatch.setattr(Path, "lstat", forbidden)
    env = gemini_mode.GeminiEnvironment(tmp_path, lambda: preferences, environ=raw)
    assert env["GEMINI_API_KEY"] == KEY
    raw["GEMINI_API_KEY"] = OTHER
    assert env["GEMINI_API_KEY"] == OTHER
    preferences["gemini_enabled"] = False
    assert env.get("GEMINI_API_KEY") is None and env["JARVIS_GEMINI_ENABLED"] == "0"


def test_saved_source_key_marker_binding_and_no_key_in_public_result(tmp_path, capsys):
    value = setup(tmp_path)
    assert value.env["GEMINI_API_KEY"] == KEY
    assert KEY not in json.dumps(value.result)
    marker = json.loads((value.state / gemini_ai.MARKER_NAME).read_text())
    assert marker["key_sha256"] == hashlib.sha256(KEY.encode()).hexdigest() and marker["billing_enabled"] is False
    chat = gemini_ai.GeminiChat(value.state, environ=value.env)
    assert chat.prepare() and chat.model == "gemini-3.5-flash-lite"
    (value.state / "gemini.env").write_text("GEMINI_API_KEY=" + OTHER, encoding="utf-8")
    assert not chat.prepare() and chat.reason == "free_project_unverified"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("changes,reason", [
    ({"billing_enabled": True}, "free_project_unverified"), ({"version": True}, "free_project_unverified"),
    ({"project_id": ""}, "free_project_unverified"), ({"key_sha256": "0" * 64}, "free_project_unverified"),
    ({"verified_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
      "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}, "free_project_expired"),
])
def test_model_client_enforces_free_marker_after_environment_key_resolution(tmp_path, changes, reason):
    value = setup(tmp_path)
    path = value.state / gemini_ai.MARKER_NAME
    marker = json.loads(path.read_text())
    marker.update(changes)
    path.write_text(json.dumps(marker), encoding="utf-8")
    assert value.env.get("GEMINI_API_KEY") == KEY  # availability isn't a free-project assertion
    chat = gemini_ai.GeminiChat(value.state, environ=value.env)
    assert not chat.prepare() and chat.reason == reason


def test_independent_context_and_speech_gates_revoke_immediately(tmp_path):
    value = setup(tmp_path)
    flags = {"local_chat_history": "HISTORY", "local_chat_plan_context": "PLAN_CONTEXT",
             "personal_context_enabled": "PERSONALITY_CONTEXT"}
    for preference, flag in flags.items():
        assert value.env["JARVIS_GEMINI_" + flag + "_ENABLED"] == "0"
        value.prefs[preference] = True
        assert value.env["JARVIS_GEMINI_" + flag + "_ENABLED"] == "1"
    assert value.env["JARVIS_GEMINI_DOCUMENT_CONTEXT_ENABLED"] == "1"
    assert value.env["JARVIS_GEMINI_SPEECH_ENABLED"] == "0"
    value.prefs["voice_engine"] = "gemini"
    assert value.env["JARVIS_GEMINI_SPEECH_ENABLED"] == "1"
    value.prefs["gemini_enabled"] = False
    assert all(value.env["JARVIS_GEMINI_" + flag + "_ENABLED"] == "0" for flag in (*flags.values(), "DOCUMENT_CONTEXT", "SPEECH"))


def test_paid_key_and_flags_forced_off_and_mapping_contains_only_resolvable_keys(tmp_path):
    raw = {"OPENAI_API_KEY": "sk-SYNTHETIC", "JARVIS_CLOUD_CHAT_ENABLED": "1", "JARVIS_CLOUD_STT_ENABLED": "1",
           "JARVIS_CLOUD_SPEECH_ENABLED": "1", "GEMINI_API_KEY": KEY, "PUBLIC": "safe"}
    env = gemini_mode.GeminiEnvironment(tmp_path, lambda: {"gemini_enabled": True}, environ=raw)
    mapping = dict(env)
    assert "OPENAI_API_KEY" not in mapping and mapping["GEMINI_API_KEY"] == KEY
    assert all(mapping[flag] == "0" for flag in raw if flag.startswith("JARVIS_CLOUD_"))
    assert mapping["PUBLIC"] == "safe" and len(env) == len(mapping)
    assert all(env[key] == item for key, item in mapping.items())


def test_mapping_iteration_includes_synthetic_paid_off_flags_without_process_values(tmp_path):
    env = gemini_mode.GeminiEnvironment(tmp_path, lambda: {"gemini_enabled": False}, environ={})
    mapping = dict(env)
    for flag in ("JARVIS_CLOUD_CHAT_ENABLED", "JARVIS_CLOUD_STT_ENABLED", "JARVIS_CLOUD_SPEECH_ENABLED"):
        assert env[flag] == "0" and flag in mapping and mapping[flag] == "0"
    assert "GEMINI_API_KEY" not in mapping and "OPENAI_API_KEY" not in mapping


def test_explicit_manifest_can_reference_only_chosen_regular_absolute_file(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    chosen = tmp_path / "explicit.env"
    chosen.write_text("UNRELATED=$(must-not-execute)\nGEMINI_API_KEY=" + KEY, encoding="utf-8")
    manifest = state / "gemini-key-source.json"
    manifest.write_text(json.dumps({"version": 1, "env_file": str(chosen), "env_name": "GEMINI_API_KEY"}), encoding="utf-8")
    original = chosen.read_bytes()
    env = gemini_mode.GeminiEnvironment(state, lambda: {"gemini_enabled": True}, environ={})
    assert env["GEMINI_API_KEY"] == KEY and chosen.read_bytes() == original
    chosen.unlink()
    assert env.get("GEMINI_API_KEY") is None


@pytest.mark.parametrize("change", [{"version": True}, {"env_name": "OPENAI_API_KEY"}, {"env_file": "relative.env"},
                                    {"extra": "unsupported"}, {"env_file": 12}])
def test_invalid_manifest_is_not_credential_discovery(tmp_path, change):
    value = setup(tmp_path)
    path = value.state / "gemini-key-source.json"
    manifest = json.loads(path.read_text())
    manifest.update(change)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert value.env.get("GEMINI_API_KEY") is None


@pytest.mark.parametrize("content", ["GEMINI_API_KEY={key}\nGEMINI_API_KEY={key}",
                                     "GEMINI_API_KEY=invalid\nGEMINI_API_KEY={key}",
                                     "GEMINI_API_KEY={key} # suffix", "GEMINI_API_KEY=$(do-not-execute)"])
def test_ambiguous_or_executable_key_assignment_rejected(tmp_path, content):
    value = setup(tmp_path)
    (value.state / "gemini.env").write_text(content.format(key=KEY), encoding="utf-8")
    assert value.env.get("GEMINI_API_KEY") is None


@pytest.mark.parametrize("name", ["gemini.env", "gemini-key-source.json"])
def test_hardlinked_sources_refused_without_mutation(tmp_path, name):
    value = setup(tmp_path)
    path = value.state / name
    before = path.read_bytes()
    os.link(path, tmp_path / "duplicate")
    assert value.env.get("GEMINI_API_KEY") is None and path.read_bytes() == before


@pytest.mark.parametrize("name,maximum", [("gemini.env", 8192), ("gemini-key-source.json", 4096)])
def test_oversized_private_source_refused_before_parsing(tmp_path, name, maximum):
    value = setup(tmp_path)
    (value.state / name).write_text(" " * (maximum + 1), encoding="utf-8")
    assert value.env.get("GEMINI_API_KEY") is None


def test_reparse_credential_source_is_rejected_before_reading(tmp_path, monkeypatch):
    value = setup(tmp_path)
    original = Path.lstat
    target = value.state / "gemini.env"
    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", reparse)
    assert value.env.get("GEMINI_API_KEY") is None


def test_duplicate_manifest_fields_and_neighbor_env_are_not_used(tmp_path):
    value = setup(tmp_path)
    manifest = value.state / "gemini-key-source.json"
    manifest.write_text('{"version":1,"version":1,"env_name":"GEMINI_API_KEY","env_file":' + json.dumps(str(value.state / "gemini.env")) + '}')
    assert value.env.get("GEMINI_API_KEY") is None
    manifest.unlink()
    (value.state / ".env").write_text("GEMINI_API_KEY=" + KEY)
    assert value.env.get("GEMINI_API_KEY") is None


@pytest.mark.parametrize("verified", [False, 1, "true", None])
def test_save_requires_explicit_verified_free_project_before_any_writes(tmp_path, verified):
    with pytest.raises(ValueError):
        gemini_mode.save_existing_key(tmp_path, KEY, "synthetic-project", free_project_verified=verified)
    assert list(tmp_path.iterdir()) == []


def test_save_rejects_downstream_invalid_target_without_overwriting_old_key(tmp_path):
    value = setup(tmp_path)
    targets = [value.state / name for name in ("gemini.env", "gemini-key-source.json", "gemini-free-project.json")]
    before = {path: path.read_bytes() for path in targets}
    os.link(targets[1], tmp_path / "linked-manifest")
    with pytest.raises((ValueError, OSError)):
        gemini_mode.save_existing_key(value.state, OTHER, "synthetic-other-project", free_project_verified=True)
    assert all(path.read_bytes() == data for path, data in before.items())


def test_stale_downstream_temporary_file_cannot_overwrite_existing_credential(tmp_path):
    value = setup(tmp_path)
    paths = list(value.state.iterdir())
    before = {path: path.read_bytes() for path in paths}
    temporary = value.state / "gemini-free-project.json.setup.tmp"
    temporary.write_text("UNFINISHED_LOCAL_OPERATION", encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        gemini_mode.save_existing_key(value.state, OTHER, "synthetic-other-project", free_project_verified=True)
    assert all(path.read_bytes() == data for path, data in before.items())
    assert temporary.read_text() == "UNFINISHED_LOCAL_OPERATION"


def test_truthy_preferences_and_paid_model_cannot_enable_cloud(tmp_path):
    value = setup(tmp_path)
    value.prefs["gemini_enabled"] = 1
    assert value.env.get("GEMINI_API_KEY") is None
    value.prefs["gemini_enabled"] = True
    value.prefs["gemini_model"] = "gemini-4-argon"
    chat = gemini_ai.GeminiChat(value.state, environ=value.env)
    assert not chat.prepare() and chat.reason == "model_unsupported"


@pytest.mark.parametrize("directory", ["//untrusted.invalid/share/profile", "/tmp/../not-a-profile"])
def test_save_lexically_invalid_path_never_probes_filesystem(directory, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Invalid private path touched filesystem")
    monkeypatch.setattr(Path, "is_dir", forbidden)
    monkeypatch.setattr(Path, "lstat", forbidden)
    with pytest.raises(ValueError):
        gemini_mode.save_existing_key(directory, KEY, "synthetic-project", free_project_verified=True)


def test_saved_credential_is_private_on_posix(tmp_path):
    value = setup(tmp_path)
    if os.name != "nt":
        assert (value.state / "gemini.env").stat().st_mode & 0o077 == 0
