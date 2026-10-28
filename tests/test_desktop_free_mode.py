"""Free mode is a live local-only boundary, including inherited paid flags."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from desktop import ai_routing, cloud_ai, cloud_speech, openai_mode, voice
from desktop.store import LocalStore


FLAGS = ("JARVIS_CLOUD_CHAT_ENABLED", "JARVIS_CLOUD_STT_ENABLED", "JARVIS_CLOUD_SPEECH_ENABLED")
KEY = "sk-proj-SYNTHETIC_FREE_MODE_NOT_A_REAL_KEY_0123456789012345"
PCM = b"\x00\x04" * 8000


def forbidden(*_args, **_kwargs):
    pytest.fail("Free operation touched a paid provider, credential source or ledger")


class GuardedEnvironment(dict):
    """Fail even on reading a synthetic credential when free mode is active."""
    def __init__(self):
        super().__init__({flag: "1" for flag in FLAGS}, OPENAI_API_KEY=KEY, PUBLIC_OPTION="public")
        self.allow_key_read = False
        self.key_reads = 0

    def get(self, key, default=None):
        if key == "OPENAI_API_KEY":
            assert self.allow_key_read, "Free mode read the raw process credential"
            self.key_reads += 1
        return super().get(key, default)

    def __getitem__(self, key):
        if key == "OPENAI_API_KEY":
            assert self.allow_key_read, "Free mode indexed the raw process credential"
            self.key_reads += 1
        return super().__getitem__(key)


class Local:
    last_usage = {"input_tokens": 3, "output_tokens": 2, "elapsed_ms": 1}
    last_language, last_confidence = "ru", .99
    reason = "ready"

    def __init__(self):
        self.answers, self.audio, self.prepared = [], [], 0

    def prepare(self):
        self.prepared += 1
        return True

    def answer(self, text, **options):
        self.answers.append((text, options))
        return "Небольшой локальный ответ."

    def transcribe(self, pcm, rate):
        self.audio.append((pcm, rate))
        return "Открой калькулятор."

    def close(self):
        pass


@pytest.fixture
def free_environment(tmp_path, monkeypatch):
    raw = GuardedEnvironment()
    preferences = {"free_mode": True, "voice_engine": "openai"}
    environment = openai_mode.OpenAIEnvironment(tmp_path / "uncreated", lambda: dict(preferences), environ=raw)
    monkeypatch.setattr(environment, "_file_key", forbidden)
    monkeypatch.setattr(openai_mode, "_regular", forbidden)
    return SimpleNamespace(raw=raw, preferences=preferences, environment=environment)


def test_master_free_flag_overrides_all_paid_flags_and_old_mode_without_reading_any_credential(free_environment):
    value = free_environment
    assert all(value.environment[flag] == "0" for flag in FLAGS)
    assert value.environment.get("OPENAI_API_KEY") is None
    assert "OPENAI_API_KEY" not in value.environment
    assert dict(value.environment) == {**dict.fromkeys(FLAGS, "0"), "PUBLIC_OPTION": "public"}
    assert value.raw.key_reads == 0 and not value.environment.state_dir.exists()


def test_free_mode_does_not_even_probe_an_explicit_neighbor_key_source(free_environment, monkeypatch):
    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert free_environment.environment.get("OPENAI_API_KEY") is None
    assert all(free_environment.environment.get(flag) == "0" for flag in FLAGS)
    assert free_environment.raw.key_reads == 0


def test_chat_router_uses_local_provider_only_without_paid_factory_or_budget(free_environment, monkeypatch):
    monkeypatch.setattr(cloud_ai._Budget, "available", forbidden)
    monkeypatch.setattr(cloud_ai._Budget, "reserve", forbidden)
    local = Local()
    router = ai_routing.ChatRouter(free_environment.environment.state_dir, environ=free_environment.environment,
                                  local_enabled=True, local_factory=lambda _: local, cloud_factory=forbidden)
    try:
        assert router.answer("Как начать учиться?") == "Небольшой локальный ответ."
        assert router.last_provider == "local" and len(local.answers) == 1
        assert free_environment.raw.key_reads == 0 and not free_environment.environment.state_dir.exists()
    finally:
        router.close()


def test_disabled_local_chat_fails_honestly_without_trying_inherited_paid_flags(free_environment):
    router = ai_routing.ChatRouter(free_environment.environment.state_dir, environ=free_environment.environment,
                                  local_enabled=False, local_factory=forbidden, cloud_factory=forbidden)
    try:
        with pytest.raises(cloud_ai.CloudUnavailable, match="^no_enabled_provider$"):
            router.answer("Объясни новый подход")
        assert router.last_provider is None and free_environment.raw.key_reads == 0
        assert not free_environment.environment.state_dir.exists()
    finally:
        router.close()


def test_fallback_transcription_prepares_and_decodes_local_only_without_paid_io(free_environment, monkeypatch):
    monkeypatch.setattr(cloud_ai._Budget, "available", forbidden)
    monkeypatch.setattr(cloud_ai._Budget, "reserve", forbidden)
    local = Local()
    decoder = ai_routing.FallbackTranscription(free_environment.environment.state_dir,
        environ=free_environment.environment, local_factory=lambda: local, cloud_factory=forbidden)
    try:
        assert decoder.prepare()
        assert decoder.transcribe(PCM, 16000) == "Открой калькулятор."
        assert decoder.last_provider == "local" and local.audio == [(PCM, 16000)]
        assert free_environment.raw.key_reads == 0 and not free_environment.environment.state_dir.exists()
    finally:
        decoder.close()


def test_realtime_speech_prepare_and_stream_refuse_before_connection_or_ledger(free_environment, monkeypatch):
    monkeypatch.setattr(cloud_ai._Budget, "available", forbidden)
    monkeypatch.setattr(cloud_ai._Budget, "reserve", forbidden)
    client = cloud_speech.OpenAIRealtimeSpeech(free_environment.environment.state_dir,
                                              environ=free_environment.environment, connector=forbidden)
    try:
        assert not client.prepare(rate=185) and client.reason == "not_configured"
        with pytest.raises(cloud_ai.CloudUnavailable, match="^not_configured$"):
            list(client.stream("Короткий синтетический ответ.", rate=185))
        assert free_environment.raw.key_reads == 0 and not free_environment.environment.state_dir.exists()
    finally:
        client.close()


def test_live_free_flip_overrides_raw_flags_without_restarting_the_mapping(free_environment):
    value = free_environment
    value.preferences["free_mode"] = False
    value.raw.allow_key_read = True
    assert all(value.environment.get(flag) == "1" for flag in FLAGS)
    assert value.environment["OPENAI_API_KEY"] == KEY and value.raw.key_reads == 1
    # Simulate an already cached owner-selected key; the free transition must
    # revoke it even when every inherited opt-in flag remains enabled.
    value.environment._key, value.environment._signature = KEY, ("synthetic-cached-source",)
    value.preferences["free_mode"] = True
    value.raw.allow_key_read = False
    assert all(value.environment.get(flag) == "0" for flag in FLAGS)
    assert value.environment.get("OPENAI_API_KEY") is None
    assert value.environment._key is value.environment._signature is None
    assert value.raw.key_reads == 1


@pytest.mark.parametrize("free", [None, False])
def test_legacy_default_keeps_existing_explicit_cloud_mode_behavior_without_file_discovery(tmp_path, free):
    preferences = {"voice_engine": "openai"}
    if free is not None:
        preferences["free_mode"] = free
    raw = GuardedEnvironment()
    raw.allow_key_read = True
    environment = openai_mode.OpenAIEnvironment(tmp_path, lambda: preferences, environ=raw)
    assert all(environment[flag] == "1" for flag in FLAGS)
    assert environment["OPENAI_API_KEY"] == KEY
    assert raw.key_reads == 1


@pytest.mark.parametrize("adapter", ["chat", "stt"])
def test_free_flip_during_provider_prepare_revokes_the_request_before_paid_content_io(free_environment, adapter):
    value, local, paid_requests = free_environment, Local(), []
    value.preferences["free_mode"] = False
    value.raw.allow_key_read = True

    class Paid:
        def prepare(self):
            value.preferences["free_mode"] = True
            value.raw.allow_key_read = False
            return True

        def answer(self, *_args, **_kwargs):
            paid_requests.append("chat")
            pytest.fail("Revoked chat request was sent")

        def transcribe(self, *_args, **_kwargs):
            paid_requests.append("stt")
            pytest.fail("Revoked audio request was sent")

        def close(self):
            pass

    if adapter == "chat":
        client = ai_routing.ChatRouter(value.environment.state_dir, environ=value.environment,
            local_enabled=True, local_factory=lambda _: local, cloud_factory=lambda **_: Paid())
        def operation():
            return client.answer("Объясни синтетический вопрос")
    else:
        client = ai_routing.FallbackTranscription(value.environment.state_dir, environ=value.environment,
            local_factory=lambda: local, cloud_factory=lambda **_: Paid())
        def operation():
            return client.transcribe(PCM, 16000)
    try:
        with pytest.raises(cloud_ai.CloudCancelled, match="^configuration_changed$"):
            operation()
        assert not paid_requests and not local.answers and not local.audio
        assert not value.environment.state_dir.exists()
    finally:
        client.close()


def test_live_free_flip_rejects_previously_cached_cloud_ack_before_another_chunk(free_environment):
    value = free_environment
    value.preferences["free_mode"] = False
    value.raw.allow_key_read = True
    client = cloud_speech.OpenAIRealtimeSpeech(value.environment.state_dir, environ=value.environment, connector=forbidden)
    identity = (hashlib.sha256(KEY.encode("ascii")).hexdigest(), client.model, client.voice)
    client._cache_identity = identity
    client._ack_cache[(*identity, 185, "Да, сэр")] = (b"\1\0" * 128, b"\2\0" * 128)
    output = client.stream("Да, сэр", rate=185)
    try:
        assert next(output) == (b"\1\0" * 128, cloud_speech.SAMPLE_RATE)
        reads = value.raw.key_reads
        value.preferences["free_mode"] = True
        value.raw.allow_key_read = False
        with pytest.raises(cloud_ai.CloudCancelled, match="^configuration_changed$"):
            next(output)
        assert value.raw.key_reads == reads and not client._ack_cache
        assert not value.environment.state_dir.exists()
    finally:
        output.close()
        client.close()


def test_store_free_bool_persists_and_changes_existing_paid_voice_atomically(tmp_path):
    store = LocalStore(tmp_path)
    assert store.preferences()["free_mode"] is False
    store.set_preferences({"voice_engine": "openai", "voice_rate": 205, "voice_enabled": True})
    updates = store.set_preferences({"free_mode": True})
    assert updates["free_mode"] is True and updates["voice_engine"] == "auto"
    reloaded = LocalStore(tmp_path).preferences()
    assert reloaded == updates and reloaded["voice_rate"] == 205 and reloaded["voice_enabled"] is True
    with store.connection() as db:
        raw = json.loads(db.execute("SELECT value FROM preferences WHERE id=1").fetchone()[0])
    assert raw["free_mode"] is True and raw["voice_engine"] == "auto"


@pytest.mark.parametrize("invalid", [0, 1, 0.0, 1.0, "true", "false", None, [], {}])
def test_free_mode_requires_a_real_boolean_and_rejection_preserves_previous_settings(tmp_path, invalid):
    store = LocalStore(tmp_path)
    before = store.set_preferences({"free_mode": True, "voice_rate": 195})
    with pytest.raises(ValueError, match="Boolean setting required"):
        store.set_preferences({"free_mode": invalid, "voice_rate": 210})
    assert store.preferences() == before


def test_paid_voice_selection_requires_explicit_free_mode_opt_out_in_same_atomic_update(tmp_path):
    store = LocalStore(tmp_path)
    before = store.set_preferences({"free_mode": True})
    with pytest.raises(ValueError, match="Disable free mode"):
        store.set_preferences({"voice_engine": "openai", "voice_rate": 220})
    assert store.preferences() == before
    accepted = store.set_preferences({"free_mode": False, "voice_engine": "openai"})
    assert accepted["free_mode"] is False and accepted["voice_engine"] == "openai"


def test_manual_conflicting_storage_is_read_as_local_and_normalized_on_next_write(tmp_path):
    store = LocalStore(tmp_path)
    corrupted = {"free_mode": True, "voice_engine": "openai", "voice_rate": 190}
    with store.connection() as db:
        db.execute("INSERT INTO preferences(id,value) VALUES(1,?)", (json.dumps(corrupted),))
    assert store.preferences()["voice_engine"] == "auto"
    changed = store.set_preferences({"organizer_dnd": True})
    assert changed["free_mode"] is True and changed["voice_engine"] == "auto" and changed["voice_rate"] == 190


def test_direct_voice_preferences_cannot_construct_paid_renderer_in_free_mode(monkeypatch):
    prepared = []

    class LocalSpeech:
        def prepare(self, *, rate):
            prepared.append(rate)
            return True

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "desktop.neural_speech", SimpleNamespace(NeuralSpeech=LocalSpeech))
    service = voice.VoiceService(lambda _: None, lambda _: None, speech_factory=forbidden)
    try:
        service.apply_preferences({"free_mode": True, "voice_engine": "openai"})
        assert service.speech_engine == "auto" and not service.multilingual_stt_enabled
        service._speech.put((service._speech_generation, voice._PREPARE_SPEECH))
        service._speech.put(None)
        service._speak_loop()
        assert prepared == [185] and service.speech_backend != "openai"
    finally:
        service.close()


def test_real_core_free_mode_uses_direct_local_chat_despite_raw_cloud_flags(tmp_path, monkeypatch):
    from desktop import chat
    from desktop.core import DesktopCore

    local, raw = Local(), GuardedEnvironment()
    original_environment = openai_mode.OpenAIEnvironment

    def environment(state_dir, preferences):
        result = original_environment(state_dir, preferences, environ=raw)
        result._file_key = forbidden
        return result

    monkeypatch.setattr(openai_mode, "OpenAIEnvironment", environment)
    monkeypatch.setattr(ai_routing, "ChatRouter", forbidden)
    monkeypatch.setattr(chat, "LocalChat", lambda _: local)
    core = DesktopCore(tmp_path, executor=forbidden)
    try:
        core.store.set_preferences({"free_mode": True, "local_chat_enabled": True})
        reply = core.handle("Чем полезны короткие рабочие интервалы?")
        assert reply.text == "Локальная модель: Небольшой локальный ответ."
        assert reply.action is reply.confirmation_id is None and len(local.answers) == 1
        assert raw.key_reads == 0
        assert not (tmp_path / "cloud-ai.sqlite3").exists()
    finally:
        core.close()
