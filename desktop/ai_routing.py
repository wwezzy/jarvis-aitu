"""Explicit cloud opt-in with one local fallback; never an action dispatcher.

Construction does not load models, read secret files or make network requests.
Only the caller's question/audio is passed on. Cloud history/plan require their
own switches even when local-context preferences are enabled. Cancellation is
terminal for the current request, not permission to send it to another model.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time

from desktop.cloud_ai import CloudCancelled, CloudUnavailable


def _cloud_configuration(environ, flag):
    key = environ.get("OPENAI_API_KEY")
    if environ.get(flag) != "1" or not isinstance(key, str) or not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,500}", key):
        return None
    return key, environ.get("JARVIS_CLOUD_CHAT_MODEL") if flag == "JARVIS_CLOUD_CHAT_ENABLED" else None


def _close(provider):
    method = getattr(provider, "close", None)
    if callable(method):
        try:
            method()
        except Exception:
            pass


def _cloud_ready(provider):
    try:
        return provider.prepare() is True
    except CloudCancelled:
        raise
    except CloudUnavailable:
        return False


def _usage(provider):
    raw = getattr(provider, "last_usage", None)
    if not isinstance(raw, dict):
        return None
    result = {}
    for field in ("input_tokens", "output_tokens"):
        value = raw.get(field)
        result[field] = value if type(value) is int and 0 <= value <= 1000000000 else None
    elapsed = raw.get("elapsed_ms")
    result["elapsed_ms"] = elapsed if type(elapsed) in {int, float} and math.isfinite(elapsed) and 0 <= elapsed <= 3600000 else None
    return result


class _Cancellation(threading.Event):
    def __init__(self, owned, external, closed):
        super().__init__()
        self.owned, self.external, self.closed = owned, external, closed

    def is_set(self):
        return super().is_set() or self.owned.is_set() or self.closed.is_set() or self.external is not None and self.external.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            # No new watcher is created. Existing client waiters observe all
            # three cancellation sources within this bounded polling interval.
            super().wait(0.025 if remaining is None else min(0.025, remaining))
        return True

    def check(self):
        if self.is_set():
            raise CloudCancelled("canceled")


class _Lifecycle:
    def _initialize_lifecycle(self):
        self._closed, self._lock = threading.Event(), threading.RLock()
        self._jobs, self._providers = set(), {}

    def _begin(self, external=None):
        if external is not None and not isinstance(external, threading.Event):
            raise ValueError("A cancellation event is required")
        if external is not None and external.is_set():
            raise CloudCancelled("canceled")
        owned = threading.Event()
        with self._lock:
            if self._closed.is_set():
                raise CloudCancelled("closed")
            if external is not None and external.is_set():
                raise CloudCancelled("canceled")
            self._jobs.add(owned)
        return _Cancellation(owned, external, self._closed)

    def _end(self, cancellation):
        with self._lock:
            self._jobs.discard(cancellation.owned)

    def _register(self, provider, cancellation):
        with self._lock:
            cancellation.check()
            self._providers[id(provider)] = provider
        return provider

    def _release(self, provider):
        with self._lock:
            self._providers.pop(id(provider), None)

    def cancel(self):
        with self._lock:
            jobs, providers = list(self._jobs), list(self._providers.values())
        for event in jobs:
            event.set()
        for provider in providers:
            method = getattr(provider, "cancel", None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass

    def close(self):
        self._closed.set()
        self.cancel()
        with self._lock:
            providers = list(self._providers.values())
            self._providers.clear()
        for provider in providers:
            _close(provider)


def _local_chat_factory(model):
    from desktop.chat import LocalChat
    return LocalChat(model)


def _cloud_chat_factory(**options):
    from desktop.cloud_ai import OpenAIChat
    return OpenAIChat(**options)


class ChatRouter(_Lifecycle):
    def __init__(self, state_dir, *, environ=None, local_enabled=False,
                 local_model="qwen3:4b-instruct-2507-q4_K_M", local_factory=None, cloud_factory=None):
        if type(local_enabled) is not bool:
            raise ValueError("Local chat requires explicit enablement")
        self.state_dir, self.environ = state_dir, os.environ if environ is None else environ
        self.local_enabled, self.local_model = local_enabled, local_model
        self.local_factory, self.cloud_factory = local_factory or _local_chat_factory, cloud_factory or _cloud_chat_factory
        self.last_usage = self.last_provider = self.last_model = None
        self._initialize_lifecycle()

    @staticmethod
    def _final(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 12000 or "\x00" in value:
            raise CloudUnavailable("invalid_response")
        return value.strip()

    def answer(self, text, history=None, on_chunk=None, *, plan_context=None, cancel_event=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000 or "\x00" in text:
            raise ValueError("Question must contain 1..8000 characters")
        if on_chunk is not None and not callable(on_chunk):
            raise ValueError("Invalid preview callback")
        self.last_usage = self.last_provider = self.last_model = None
        cancellation = self._begin(cancel_event)
        cloud_failure, final = None, None
        try:
            cancellation.check()
            configuration = _cloud_configuration(self.environ, "JARVIS_CLOUD_CHAT_ENABLED")
            if configuration is not None:
                cloud = self.cloud_factory(state_dir=self.state_dir, environ=self.environ)
                try:
                    self._register(cloud, cancellation)
                    if _cloud_ready(cloud):
                        cancellation.check()
                        if _cloud_configuration(self.environ, "JARVIS_CLOUD_CHAT_ENABLED") != configuration:
                            raise CloudCancelled("configuration_changed")
                        # Provisional cloud text is never published before its
                        # final response validates. A failed cloud generation
                        # cannot leave previews beside the local fallback.
                        def cloud_preview(_delta):
                            cancellation.check()
                            return True
                        try:
                            final = self._final(cloud.answer(text,
                                history=history if self.environ.get("JARVIS_CLOUD_HISTORY_ENABLED") == "1" else None,
                                on_chunk=cloud_preview if on_chunk is not None else None,
                                plan_context=plan_context if self.environ.get("JARVIS_CLOUD_PLAN_CONTEXT_ENABLED") == "1" else None,
                                cancel_event=cancellation))
                        except CloudCancelled:
                            raise
                        except CloudUnavailable as error:
                            cancellation.check()
                            cloud_failure = error
                        if final is not None:
                            self.last_provider = "openai"
                            self.last_model = getattr(cloud, "model", None)
                            self.last_usage = _usage(cloud)
                finally:
                    self._release(cloud)
                    _close(cloud)
            cancellation.check()
            if final is not None:
                if on_chunk is not None and on_chunk(final) is False:
                    cancellation.owned.set()
                    raise CloudCancelled("canceled")
                cancellation.check()
                return final
            if not self.local_enabled:
                raise cloud_failure or CloudUnavailable("no_enabled_provider")
            local = self.local_factory(self.local_model)
            try:
                self._register(local, cancellation)
                cancellation.check()
                def local_preview(delta):
                    cancellation.check()
                    if on_chunk is not None and on_chunk(delta) is False:
                        cancellation.owned.set()
                        return False
                    return True
                final = self._final(local.answer(text, history=history,
                    on_chunk=local_preview if on_chunk is not None else None,
                    plan_context=plan_context, cancel_event=cancellation))
                cancellation.check()
                self.last_provider, self.last_model, self.last_usage = "local", self.local_model, _usage(local)
                return final
            finally:
                self._release(local)
                _close(local)
        finally:
            self._end(cancellation)


def _local_stt_factory():
    from desktop.multilingual_stt import MultilingualSTT
    return MultilingualSTT()


def _cloud_stt_factory(**options):
    from desktop.cloud_ai import OpenAITranscription
    return OpenAITranscription(**options)


class FallbackTranscription(_Lifecycle):
    """Optional full-utterance decoder; the primary local wake gate is separate.

    Cloud readiness never eagerly loads Whisper. After a cloud failure the
    explicitly enabled local decoder is prepared lazily and tried once. Local
    native decoding is not force-killed; its late output is discarded on cancel.
    """
    def __init__(self, state_dir, *, environ=None, local_factory=None, cloud_factory=None):
        self.state_dir, self.environ = state_dir, os.environ if environ is None else environ
        self.local_factory, self.cloud_factory = local_factory or _local_stt_factory, cloud_factory or _cloud_stt_factory
        self._local = self._cloud = None
        self.reason, self.last_provider = "not_prepared", None
        self.last_language = self.last_confidence = self.last_usage = None
        self._decode_lock = threading.Lock()
        self._initialize_lifecycle()

    def _cloud_decoder(self, cancellation):
        cancellation.check()
        if _cloud_configuration(self.environ, "JARVIS_CLOUD_STT_ENABLED") is None:
            return None
        cancellation.check()
        with self._lock:
            cancellation.check()
            if self._cloud is None:
                candidate = self.cloud_factory(state_dir=self.state_dir, environ=self.environ)
                try:
                    self._register(candidate, cancellation)
                    self._cloud = candidate
                except CloudCancelled:
                    _close(candidate)
                    raise
            return self._cloud

    def _local_decoder(self, cancellation):
        cancellation.check()
        with self._lock:
            cancellation.check()
            if self._local is None:
                candidate = self.local_factory()
                try:
                    self._register(candidate, cancellation)
                    self._local = candidate
                except CloudCancelled:
                    _close(candidate)
                    raise
            return self._local

    def prepare(self):
        try:
            cancellation = self._begin()
        except CloudCancelled:
            self.reason = "closed"
            return False
        try:
            cloud = self._cloud_decoder(cancellation)
            if cloud is not None and _cloud_ready(cloud):
                cancellation.check()
                self.reason = "cloud_ready"
                return True
            local = self._local_decoder(cancellation)
            ready = local.prepare() is True
            cancellation.check()
            self.reason = "ready" if ready else "local_unavailable"
            return ready
        except CloudCancelled:
            self.reason = "canceled"
            return False
        except (CloudUnavailable, OSError, ValueError, RuntimeError):
            self.reason = "unavailable"
            return False
        finally:
            self._end(cancellation)

    def transcribe(self, pcm, sample_rate):
        return self.transcribe_cancelable(pcm, sample_rate)

    def transcribe_cancelable(self, pcm, sample_rate, *, cancel_event=None):
        """A caller-captured event also fences work not yet registered here.

        A stale VoiceService job may reach this adapter only after interrupt()
        has already canceled its previous jobs. Its captured, permanently-set
        event must stop this call before a provider/configuration is touched.
        """
        if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or type(sample_rate) is not int
                or not 8000 <= sample_rate <= 48000 or len(pcm) > 12 * sample_rate * 2):
            raise ValueError("Expected mono16le audio, 8..48kHz and at most 12 seconds")
        cancellation = self._begin(cancel_event)
        if not self._decode_lock.acquire(blocking=False):
            self._end(cancellation)
            raise CloudUnavailable("busy")
        self.last_provider = self.last_language = self.last_confidence = self.last_usage = None
        try:
            cancellation.check()
            cloud = self._cloud_decoder(cancellation)
            cancellation.check()
            configuration = _cloud_configuration(self.environ, "JARVIS_CLOUD_STT_ENABLED")
            if cloud is not None and _cloud_ready(cloud):
                cancellation.check()
                if _cloud_configuration(self.environ, "JARVIS_CLOUD_STT_ENABLED") != configuration:
                    raise CloudCancelled("configuration_changed")
                try:
                    cancellation.check()
                    text = ChatRouter._final(cloud.transcribe(pcm, sample_rate))
                    cancellation.check()
                    if len(text) > 500:
                        raise CloudUnavailable("transcript_limit")
                    self.last_provider, self.reason, self.last_usage = "openai", "ready", _usage(cloud)
                    return text
                except CloudCancelled:
                    raise
                except CloudUnavailable:
                    cancellation.check()
            return self._local_result(pcm, sample_rate, cancellation)
        finally:
            self._decode_lock.release()
            self._end(cancellation)

    def _local_result(self, pcm, sample_rate, cancellation):
        try:
            local = self._local_decoder(cancellation)
            if local.prepare() is not True:
                raise CloudUnavailable("local_unavailable")
            cancellation.check()
            text = ChatRouter._final(local.transcribe(pcm, sample_rate))
            cancellation.check()
            if len(text) > 500 or "<|" in text or any(ord(char) < 32 and char not in "\n\t" for char in text):
                raise CloudUnavailable("transcript_limit")
            self.last_provider, self.reason = "local", "ready"
            self.last_language = getattr(local, "last_language", None)
            self.last_confidence = getattr(local, "last_confidence", None)
            return text
        except CloudCancelled:
            raise
        except CloudUnavailable:
            raise
        except Exception:
            raise CloudUnavailable("local_unavailable") from None

    def transcribe_local(self, pcm, sample_rate):
        """Before verified wake: no cloud construction, flags, key or HTTP.

        An English keyword candidate is not authorization to upload an idle
        microphone recording. This path can only use the local CPU decoder.
        """
        if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or type(sample_rate) is not int
                or not 8000 <= sample_rate <= 48000 or len(pcm) > 12 * sample_rate * 2):
            raise ValueError("Expected mono16le audio, 8..48kHz and at most 12 seconds")
        cancellation = self._begin()
        if not self._decode_lock.acquire(blocking=False):
            self._end(cancellation)
            raise CloudUnavailable("busy")
        self.last_provider = self.last_language = self.last_confidence = self.last_usage = None
        try:
            return self._local_result(pcm, sample_rate, cancellation)
        finally:
            self._decode_lock.release()
            self._end(cancellation)

    def close(self):
        super().close()
        self._local = self._cloud = None

    def cancel(self):
        # transcribe(pcm,rate) has no external-event parameter. Permanently
        # close and detach the old cloud client so a call racing cancellation
        # cannot begin a late POST. Only a new explicit job may create another.
        with self._lock:
            for event in self._jobs:
                event.set()
            cloud, self._cloud = self._cloud, None
            if cloud is not None:
                self._providers.pop(id(cloud), None)
                _close(cloud)
            providers = list(self._providers.values())
        for provider in providers:
            method = getattr(provider, "cancel", None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
