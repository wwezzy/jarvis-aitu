"""Routing uses fakes or the real HTTP adapter with an injected byte response."""
import io
import json
import threading
from types import SimpleNamespace

import pytest

from desktop.ai_routing import ChatRouter, FallbackTranscription
from desktop.cloud_ai import CloudCancelled, CloudUnavailable, CloudBudgetExceeded


KEY = 'sk-' + 'proj-synthetic_ROUTER_NOT_A_REAL_KEY_0123456789'


class Chat:
    def __init__(self, result="cloud final", error=None, ready=True):
        self.result, self.error, self.ready = result, error, ready
        self.calls, self.prepared, self.closed, self.canceled = [], 0, 0, 0
        self.model = "gpt-6.1-terra"
        self.last_usage = {"input_tokens": 10, "output_tokens": 5, "elapsed_ms": 3, "private": "must not project"}
        self.before_answer = lambda: None

    def prepare(self):
        self.prepared += 1
        return self.ready

    def answer(self, text, **options):
        self.calls.append((text, options))
        self.before_answer()
        if options.get("on_chunk") is not None:
            options["on_chunk"]("PROVISIONAL")
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed += 1

    def cancel(self):
        self.canceled += 1


def setup(tmp_path, **options):
    cloud, local, constructors = Chat(), Chat(result="local final"), []
    env = {"OPENAI_API_KEY": KEY, "JARVIS_CLOUD_CHAT_ENABLED": "1", "JARVIS_CLOUD_STT_ENABLED": "1"}
    def cloud_factory(**kwargs):
        constructors.append(("cloud", kwargs))
        return cloud
    def local_factory(model):
        constructors.append(("local", model))
        return local
    router = ChatRouter(tmp_path, environ=env, local_enabled=options.pop("local_enabled", True),
                        local_factory=local_factory, cloud_factory=cloud_factory, **options)
    return SimpleNamespace(router=router, env=env, cloud=cloud, local=local, constructors=constructors)


def test_router_constructor_has_no_provider_model_or_network_side_effects(tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Constructor created a provider")
    router = ChatRouter(tmp_path, environ={}, local_factory=forbidden, cloud_factory=forbidden)
    assert router.last_usage is router.last_provider is router.last_model is None
    with pytest.raises(CloudUnavailable):
        router.answer("Hello")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("flag,key", [("0", KEY), ("true", KEY), (1, KEY), (None, KEY), ("1", "bad")])
def test_cloud_requires_exact_opt_in_and_valid_key_before_factory(tmp_path, flag, key):
    value = setup(tmp_path)
    value.env.update(JARVIS_CLOUD_CHAT_ENABLED=flag, OPENAI_API_KEY=key)
    assert value.router.answer("Hello") == "local final"
    assert [item[0] for item in value.constructors] == ["local"]
    assert value.router.last_provider == "local"


def test_cloud_final_has_provenance_safe_usage_and_no_unvalidated_preview(tmp_path):
    value, chunks = setup(tmp_path), []
    assert value.router.answer("Hello", on_chunk=chunks.append) == "cloud final"
    assert chunks == ["cloud final"] and not value.local.calls
    assert value.cloud.closed == 1 and value.cloud.prepared == 1 and len(value.cloud.calls) == 1
    assert value.router.last_provider == "openai" and value.router.last_model == "gpt-6.1-terra"
    assert value.router.last_usage == {"input_tokens": 10, "output_tokens": 5, "elapsed_ms": 3}


@pytest.mark.parametrize("history_flag,plan_flag", [(None, None), ("1", None), (None, "1"), ("1", "1")])
def test_cloud_context_has_two_separate_opt_ins(tmp_path, history_flag, plan_flag):
    value = setup(tmp_path)
    value.env.update(JARVIS_CLOUD_HISTORY_ENABLED=history_flag, JARVIS_CLOUD_PLAN_CONTEXT_ENABLED=plan_flag)
    history, plan = [{"role": "user", "content": "local private history"}], {"today": "local private plan"}
    value.router.answer("Hello", history=history, plan_context=plan)
    options = value.cloud.calls[0][1]
    assert options["history"] == (history if history_flag == "1" else None)
    assert options["plan_context"] == (plan if plan_flag == "1" else None)


@pytest.mark.parametrize("failure", [CloudUnavailable("network_unavailable"), CloudBudgetExceeded("budget_exceeded")])
def test_one_failed_cloud_attempt_falls_back_once_without_leaking_its_preview(tmp_path, failure):
    value, chunks = setup(tmp_path), []
    value.cloud.error = failure
    history, plan = ["local context"], {"local plan": "allowed only locally"}
    assert value.router.answer("Hello", history=history, plan_context=plan, on_chunk=chunks.append) == "local final"
    assert len(value.cloud.calls) == len(value.local.calls) == 1
    assert chunks == ["PROVISIONAL"]  # The local adapter may stream its own output.
    assert value.local.calls[0][1]["history"] == history and value.local.calls[0][1]["plan_context"] == plan
    assert value.router.last_provider == "local"


def test_canceled_cloud_and_rejected_final_preview_never_fall_back(tmp_path):
    value, chunks = setup(tmp_path), []
    value.cloud.error = CloudCancelled("canceled")
    with pytest.raises(CloudCancelled):
        value.router.answer("Hello", on_chunk=chunks.append)
    assert not chunks and not value.local.calls
    value.cloud.error = None
    with pytest.raises(CloudCancelled):
        value.router.answer("Hello", on_chunk=lambda _chunk: False)
    assert not value.local.calls


def test_cloud_failure_never_enables_disabled_local_model(tmp_path):
    value = setup(tmp_path, local_enabled=False)
    value.cloud.error = CloudUnavailable("authentication")
    with pytest.raises(CloudUnavailable, match="authentication"):
        value.router.answer("Hello")
    assert not value.local.calls and [item[0] for item in value.constructors] == ["cloud"]


def test_unavailable_prepare_falls_back_without_calling_cloud_answer(tmp_path):
    value = setup(tmp_path)
    def unavailable():
        raise CloudBudgetExceeded("budget_unavailable")
    value.cloud.prepare = unavailable
    assert value.router.answer("Hello") == "local final" and not value.cloud.calls


def test_pre_cancellation_close_and_late_local_output_are_terminal(tmp_path):
    value = setup(tmp_path)
    event = threading.Event()
    event.set()
    with pytest.raises(CloudCancelled):
        value.router.answer("Hello", cancel_event=event)
    assert not value.constructors
    value.env["JARVIS_CLOUD_CHAT_ENABLED"] = "0"
    value.local.before_answer = value.router.cancel
    with pytest.raises(CloudCancelled):
        value.router.answer("Hello")
    value.router.close()
    before = len(value.constructors)
    with pytest.raises(CloudCancelled):
        value.router.answer("Hello")
    assert len(value.constructors) == before


def test_real_local_chat_accepts_composite_threading_event_with_fake_http(tmp_path):
    from desktop.chat import LocalChat, ENDPOINT
    captured = []
    class Response(io.BytesIO):
        status = 200
        def geturl(self):
            return ENDPOINT
    class Opener:
        def open(self, request, **_options):
            captured.append(json.loads(request.data))
            return Response(json.dumps({"done": True, "done_reason": "stop", "message": {
                "role": "assistant", "content": "Real local adapter final"}, "prompt_eval_count": 12, "eval_count": 3}).encode())
    router = ChatRouter(tmp_path, environ={}, local_enabled=True,
                        local_factory=lambda model: LocalChat(model, opener=Opener()))
    assert router.answer("Hello") == "Real local adapter final"
    assert router.last_provider == "local" and router.last_usage["input_tokens"] == 12
    assert captured[0]["messages"][-1] == {"role": "user", "content": "Hello"}


class Decoder:
    def __init__(self, result="Jarvis, hello", error=None, ready=True):
        self.result, self.error, self.ready = result, error, ready
        self.prepared, self.closed, self.calls = 0, 0, []
        self.last_language, self.last_confidence = "en", 0.98
        self.before = lambda: None

    def prepare(self):
        self.prepared += 1
        return self.ready

    def transcribe(self, pcm, rate):
        self.before()
        if self.closed:
            raise CloudCancelled("closed")
        self.calls.append((pcm, rate))
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed += 1


def transcription(tmp_path, env=None):
    cloud, local, made = Decoder("Джарвис, привет"), Decoder(), []
    if env is None:
        env = {"OPENAI_API_KEY": KEY, "JARVIS_CLOUD_STT_ENABLED": "1"}
    def factory(kind, value):
        def construct(*_args, **_kwargs):
            made.append(kind)
            return value
        return construct
    facade = FallbackTranscription(tmp_path, environ=env,
        local_factory=factory("local", local), cloud_factory=factory("cloud", cloud))
    return SimpleNamespace(facade=facade, cloud=cloud, local=local, made=made, env=env)


def test_transcription_cloud_ready_does_not_eagerly_load_local_model(tmp_path):
    value = transcription(tmp_path)
    assert value.made == []
    assert value.facade.prepare()
    assert value.made == ["cloud"] and value.local.prepared == 0
    assert value.facade.transcribe(b"00" * 8000, 8000) == "Джарвис, привет"
    assert value.facade.last_provider == "openai" and not value.local.calls
    assert value.facade.last_language is value.facade.last_confidence is None


def test_transcription_cloud_one_failure_then_cached_local_once(tmp_path):
    value = transcription(tmp_path)
    value.cloud.error = CloudBudgetExceeded("budget_exceeded")
    for _ in range(2):
        assert value.facade.transcribe(b"00" * 8000, 8000) == "Jarvis, hello"
    assert value.made == ["cloud", "local"] and len(value.cloud.calls) == len(value.local.calls) == 2
    assert value.facade.last_provider == "local" and value.facade.last_confidence == 0.98


def test_transcription_prepare_failure_preserves_lazy_local_fallback(tmp_path):
    value = transcription(tmp_path)
    def unavailable():
        raise CloudBudgetExceeded("budget_unavailable")
    value.cloud.prepare = unavailable
    assert value.facade.prepare() and value.made == ["cloud", "local"]
    assert value.facade.transcribe(b"00" * 8000, 8000) == "Jarvis, hello" and not value.cloud.calls


def test_transcription_cancellation_closes_old_cloud_and_never_falls_back(tmp_path):
    value = transcription(tmp_path)
    value.cloud.before = value.facade.cancel
    with pytest.raises(CloudCancelled):
        value.facade.transcribe(b"00" * 8000, 8000)
    assert value.cloud.closed == 1 and not value.cloud.calls and not value.local.calls
    value.facade.close()
    assert value.facade.prepare() is False
    with pytest.raises(CloudCancelled):
        value.facade.transcribe(b"00" * 8000, 8000)


def test_local_before_wake_never_reads_cloud_flags_keys_or_constructs_cloud(tmp_path):
    class ForbiddenEnvironment(dict):
        def get(self, *_args):
            pytest.fail("Idle microphone verification inspected cloud configuration")
    value = transcription(tmp_path, env=ForbiddenEnvironment(OPENAI_API_KEY=KEY, JARVIS_CLOUD_STT_ENABLED="1"))
    assert value.facade.transcribe_local(b"00" * 8000, 8000) == "Jarvis, hello"
    assert value.made == ["local"] and value.cloud.prepared == 0 and not value.cloud.calls
    value.local.error = RuntimeError("private local diagnostics")
    with pytest.raises(CloudUnavailable, match="local_unavailable"):
        value.facade.transcribe_local(b"00" * 8000, 8000)
    assert value.made == ["local"] and not value.cloud.calls


def test_env_revocation_before_next_transcription_uses_only_local(tmp_path):
    value = transcription(tmp_path)
    assert value.facade.prepare() and value.made == ["cloud"]
    value.env["JARVIS_CLOUD_STT_ENABLED"] = "0"
    assert value.facade.transcribe(b"00" * 8000, 8000) == "Jarvis, hello"
    assert not value.cloud.calls and value.facade.last_provider == "local"


def test_late_local_native_decode_is_discarded_after_cancel(tmp_path):
    value = transcription(tmp_path, env={})
    value.local.before = value.facade.cancel
    with pytest.raises(CloudCancelled):
        value.facade.transcribe_local(b"00" * 8000, 8000)
    assert value.facade.last_provider is None and not value.cloud.calls
