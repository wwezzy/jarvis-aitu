"""Personal cue validation never plays audio, follows links or mutates files."""
import hashlib
import io
import json
import struct
import wave
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from desktop import audio_cues as module
from desktop.audio_cues import CueLibrary


def audio_bytes(*, rate=24000, channels=1, width=2, frames=2400):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(b"\x00" * frames * channels * width)
    return output.getvalue()


def manifest(directory, content=None, *, path="welcome-home.wav", **changes):
    directory.mkdir(parents=True, exist_ok=True)
    wav = directory / "welcome-home.wav"
    wav.write_bytes(content if content is not None else audio_bytes())
    data = {"version": 1, "cues": {"welcome_home": {"path": path, "sha256": hashlib.sha256(wav.read_bytes()).hexdigest()}}}
    data.update(changes)
    config = directory / "audio-cues.json"
    config.write_text(json.dumps(data), encoding="utf-8")
    return config, wav, data


def test_default_is_private_readonly_missing_and_never_creates_profile(tmp_path, monkeypatch):
    home = tmp_path / "unused-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    library = CueLibrary()
    assert library.config_path == home / "JarvisData/VoiceLab/audio-cues.json"
    assert library.read("welcome_home") is None and not library.available("welcome_home")
    assert not home.exists()


@pytest.mark.parametrize("rate,channels", [(16000, 1), (24000, 1), (44100, 2), (48000, 2)])
def test_valid_pinned_cue_returns_exact_immutable_pcm_without_changing_files(tmp_path, rate, channels):
    raw = audio_bytes(rate=rate, channels=channels)
    config, wav, _data = manifest(tmp_path, raw)
    before = {path: path.read_bytes() for path in (config, wav)}
    cue = CueLibrary(config).read("welcome_home")
    assert cue.pcm == b"\x00" * 2400 * channels * 2
    assert (cue.sample_rate, cue.channels) == (rate, channels)
    assert CueLibrary(config).available("welcome_home")
    assert {path: path.read_bytes() for path in before} == before
    with pytest.raises(FrozenInstanceError):
        cue.channels = 99


def test_absolute_path_is_allowed_only_inside_manifest_private_directory(tmp_path):
    config, wav, data = manifest(tmp_path / "Private")
    data["cues"]["welcome_home"]["path"] = str(wav)
    config.write_text(json.dumps(data), encoding="utf-8")
    assert CueLibrary(config).available("welcome_home")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(wav.read_bytes())
    data["cues"]["welcome_home"]["path"] = str(outside)
    config.write_text(json.dumps(data), encoding="utf-8")
    assert not CueLibrary(config).available("welcome_home")


@pytest.mark.parametrize("name", [None, True, "../welcome-home.wav", "welcome-home.wav", "https://example.com", "shutdown", "WELCOME_HOME", "welcome_home;run"])
def test_unknown_cue_identifiers_are_never_interpreted_as_paths(tmp_path, name):
    config, _wav, _data = manifest(tmp_path)
    assert CueLibrary(config).read(name) is None


@pytest.mark.parametrize("path", ["../outside.wav", "https://example.com/cue.wav", "//server/share/cue.wav", "\\\\server\\share\\cue.wav",
                                  "C:drive-relative.wav", "C:welcome-home.wav", "welcome-home.wav:stream", "bad\x00path.wav", "welcome-home.mp3"])
def test_paths_cannot_escape_redirect_or_select_nonwav_files(tmp_path, path):
    config, _wav, _data = manifest(tmp_path, path=path)
    assert CueLibrary(config).read("welcome_home") is None


@pytest.mark.parametrize("changes", [{"version": True}, {"version": 2}, {"extra": "data"}, {"cues": []},
                                     {"cues": {"arbitrary": {"path": "welcome-home.wav", "sha256": "0" * 64}}}])
def test_manifest_version_shape_and_finite_entries_are_strict(tmp_path, changes):
    config, _wav, _data = manifest(tmp_path, **changes)
    assert CueLibrary(config).read("welcome_home") is None


@pytest.mark.parametrize("entry", [{}, {"path": "welcome-home.wav"}, {"path": "welcome-home.wav", "sha256": "0" * 64},
                                   {"path": "welcome-home.wav", "sha256": None}, {"path": "welcome-home.wav", "sha256": "g" * 64},
                                   {"path": 42, "sha256": "0" * 64}, {"path": "welcome-home.wav", "sha256": "0" * 64, "command": "execute"}])
def test_invalid_hash_or_entry_fields_fail_closed(tmp_path, entry):
    config, _wav, data = manifest(tmp_path)
    data["cues"]["welcome_home"] = entry
    config.write_text(json.dumps(data), encoding="utf-8")
    assert CueLibrary(config).read("welcome_home") is None


def test_uppercase_hash_is_valid_but_replaced_audio_requires_new_pin(tmp_path):
    config, wav, data = manifest(tmp_path)
    data["cues"]["welcome_home"]["sha256"] = data["cues"]["welcome_home"]["sha256"].upper()
    config.write_text(json.dumps(data), encoding="utf-8")
    assert CueLibrary(config).available("welcome_home")
    wav.write_bytes(audio_bytes(frames=1200))
    assert CueLibrary(config).read("welcome_home") is None


@pytest.mark.parametrize("settings", [{"rate": 8000}, {"rate": 96000}, {"channels": 3}, {"width": 1}, {"width": 3},
                                      {"frames": 0}, {"rate": 16000, "frames": 16000 * 20 + 1}])
def test_only_bounded_pcm16_mono_or_stereo_is_accepted(tmp_path, settings):
    config, _wav, _data = manifest(tmp_path, audio_bytes(**settings))
    assert CueLibrary(config).read("welcome_home") is None


def test_exact_twenty_second_boundary_is_allowed(tmp_path):
    config, _wav, _data = manifest(tmp_path, audio_bytes(rate=16000, frames=16000 * 20))
    assert CueLibrary(config).read("welcome_home") is not None


@pytest.mark.parametrize("mutation", ["truncated", "appended", "wrong_riff", "wrong_wave", "wrong_length", "odd_pcm", "compressed"])
def test_corrupted_wav_with_matching_hash_still_fails_actual_pcm_validation(tmp_path, mutation):
    raw = bytearray(audio_bytes())
    if mutation == "truncated":
        raw = raw[:-4]
    elif mutation == "appended":
        raw.extend(b"arbitrary trailing content")
    elif mutation == "wrong_riff":
        raw[:4] = b"FAKE"
    elif mutation == "wrong_wave":
        raw[8:12] = b"FAKE"
    elif mutation == "wrong_length":
        struct.pack_into("<I", raw, 40, 1000000)
    elif mutation == "odd_pcm":
        raw.append(1)
        struct.pack_into("<I", raw, 4, len(raw) - 8)
        struct.pack_into("<I", raw, 40, len(raw) - 44)
    else:
        struct.pack_into("<H", raw, 20, 6)  # A-law rather than PCM.
    config, _wav, _data = manifest(tmp_path, bytes(raw))
    assert CueLibrary(config).read("welcome_home") is None


def test_manifest_duplicate_keys_and_oversize_are_rejected(tmp_path):
    config, _wav, _data = manifest(tmp_path)
    config.write_text('{"version":2,"version":1,"cues":{}}', encoding="utf-8")
    assert CueLibrary(config).read("welcome_home") is None
    config.write_bytes(b" " * (module.MAX_MANIFEST_BYTES + 1))
    assert CueLibrary(config).read("welcome_home") is None


def test_file_byte_limit_applies_before_wave_parsing(tmp_path, monkeypatch):
    config, _wav, _data = manifest(tmp_path)
    monkeypatch.setattr(module, "MAX_CUE_BYTES", 128)
    assert CueLibrary(config).read("welcome_home") is None


def test_symbolic_link_escape_is_rejected_even_when_the_hash_matches(tmp_path):
    config, wav, data = manifest(tmp_path / "Private")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(wav.read_bytes())
    link = config.parent / "linked.wav"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable to this user")
    data["cues"]["welcome_home"]["path"] = "linked.wav"
    config.write_text(json.dumps(data), encoding="utf-8")
    assert CueLibrary(config).read("welcome_home") is None


def test_canonical_path_redirect_is_rejected_without_symlink_privileges(tmp_path, monkeypatch):
    config, wav, _data = manifest(tmp_path / "Private")
    original_resolve = Path.resolve
    def redirected(path, *args, **kwargs):
        return tmp_path / "outside.wav" if path == wav else original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", redirected)
    assert CueLibrary(config).read("welcome_home") is None


def test_invalid_json_or_deep_structure_never_leaks_errors_or_private_paths(tmp_path, capsys):
    config, _wav, _data = manifest(tmp_path)
    for text in ("not-json", "[" * 1200 + "]" * 1200):
        config.write_text(text, encoding="utf-8")
        assert CueLibrary(config).read("welcome_home") is None
    assert capsys.readouterr() == ("", "")
