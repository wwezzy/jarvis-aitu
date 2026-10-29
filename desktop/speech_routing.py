"""Optional Google speech with the accepted local clone as a finite fallback."""
from __future__ import annotations

import threading
import time

from desktop.gemini_ai import GeminiCancelled, GeminiUnavailable


class GeminiLocalSpeech:
    def __init__(self, *, gemini_factory, clone_factory=None, clock=time.monotonic):
        if not callable(gemini_factory) or clone_factory is not None and not callable(clone_factory):
            raise ValueError("Speech factories required")
        self.gemini_factory, self.clone_factory, self.clock = gemini_factory, clone_factory, clock
        self._gemini = self._clone = None
        self._closed, self._lock = threading.Event(), threading.RLock()
        self._generation = 0
        self.backend, self.reason, self.last_usage = "gemini_unavailable", "not_prepared", None

    @staticmethod
    def _dispose(component):
        if component is not None:
            try:
                component.close()
            except Exception:
                pass

    def _component(self, kind):
        attribute = "_" + kind
        with self._lock:
            if self._closed.is_set():
                raise GeminiCancelled("closed")
            existing = getattr(self, attribute)
            generation = self._generation
        if existing is not None:
            return existing
        if kind == "clone" and self.clone_factory is None:
            from desktop.clone_speech import CloneSpeech
            candidate = CloneSpeech(personal_use=True)
        else:
            candidate = (self.clone_factory if kind == "clone" else self.gemini_factory)()
        with self._lock:
            closed = self._closed.is_set() or generation != self._generation
            existing = getattr(self, attribute)
            if not closed and existing is None:
                setattr(self, attribute, candidate)
                return candidate
        self._dispose(candidate)
        if closed:
            raise GeminiCancelled("closed")
        return existing

    def _warm_clone(self, rate):
        generation = self._generation
        try:
            clone = self._component("clone")
            with self._lock:
                if self._closed.is_set() or generation != self._generation:
                    raise GeminiCancelled("closed")
                clone.prepare_background(rate=rate)
            return clone
        except GeminiCancelled:
            raise
        except Exception:
            return None

    def prepare(self, rate=185):
        if type(rate) is not int or not 90 <= rate <= 240:
            raise ValueError("Speech rate must be 90..240")
        generation = self._generation
        clone = self._warm_clone(rate)
        try:
            ready = self._component("gemini").prepare(rate=rate) is True
        except GeminiCancelled:
            raise
        except GeminiUnavailable:
            ready = False
        if self._closed.is_set() or generation != self._generation:
            raise GeminiCancelled("canceled")
        self.backend = ("gemini" if ready else "clone_fallback" if clone is not None and clone.is_ready
                        else "clone_warming" if clone is not None and clone.is_warming else "gemini_unavailable")
        self.reason = "ready" if ready or self.backend == "clone_fallback" else "local_unavailable"
        return ready or self.backend in {"clone_fallback", "clone_warming"}

    def stream(self, text, rate=185, *, language=None, cancel=None):
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000 or "\0" in text
                or type(rate) is not int or not 90 <= rate <= 240
                or language is not None and (type(language) is not str or language not in {"ru", "en"})
                or cancel is not None and not callable(cancel)):
            raise ValueError("Invalid bounded speech request")
        return self._stream(text.strip(), rate, language, cancel)

    def _stream(self, text, rate, language, cancel):
        with self._lock:
            generation = self._generation
        self.last_usage = None

        def canceled():
            try:
                return (self._closed.is_set() or generation != self._generation
                        or cancel is not None and cancel() is True)
            except Exception:
                return True

        def check():
            if canceled():
                raise GeminiCancelled("canceled")

        emitted, chunks = False, None
        try:
            check()
            clone = self._warm_clone(rate)
            fixed = (clone.fixed_ack(language or ("en" if text.casefold().startswith("yes") else "ru")) if clone is not None
                     and text.casefold().rstrip(".!?") in {"да, сэр", "да сэр", "yes, sir", "yes sir"} else None)
            if fixed is not None:
                check()
                self.backend, self.reason = "clone_fallback", "ready"
                yield fixed
                check()
                self.last_usage = {"input_tokens": 0, "output_tokens": 0, "cache_hit": True}
                return
            try:
                gemini = self._component("gemini")
                if gemini.prepare(rate=rate) is not True:
                    raise GeminiUnavailable(getattr(gemini, "reason", "not_configured"))
                chunks = gemini.stream(text, rate=rate, language=language, cancel=canceled)
                for chunk in chunks:
                    check()
                    emitted = True
                    self.backend = "gemini"
                    yield chunk
                if not emitted:
                    raise GeminiUnavailable("invalid_response")
                check()
                self.reason, self.last_usage = "ready", getattr(gemini, "last_usage", None)
                return
            except GeminiCancelled:
                raise
            except GeminiUnavailable:
                if emitted:
                    raise
            finally:
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()
            check()
            if clone is None:
                raise GeminiUnavailable("local_unavailable")
            deadline = self.clock() + 45
            while not clone.is_ready:
                check()
                self.backend = "clone_warming"
                if not clone.is_warming or self.clock() >= deadline:
                    raise GeminiUnavailable("local_unavailable")
                self._closed.wait(.05)
            check()
            self.backend, self.reason = "clone_fallback", "ready"
            local_emitted = False
            for chunk in clone.stream(text, rate=rate, language=language, cancel=canceled):
                check()
                local_emitted = True
                yield chunk
            check()
            if not local_emitted:
                raise GeminiUnavailable("local_unavailable")
            self.last_usage = None  # Local audio has no cloud token usage.
        except GeminiCancelled:
            self.cancel()
            raise
        except Exception:
            if canceled():
                self.cancel()
                raise GeminiCancelled("canceled") from None
            if not emitted:
                self.backend = "gemini_unavailable"
            self.reason = "local_unavailable" if self.backend != "gemini" else "provider_unavailable"
            raise

    def cancel(self):
        with self._lock:
            self._generation += 1
            clone, self._clone = self._clone, None
            gemini = self._gemini
        cancel = getattr(gemini, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass
        self._dispose(clone)

    def close(self):
        self._closed.set()
        self.cancel()
        with self._lock:
            gemini, self._gemini = self._gemini, None
        self._dispose(gemini)
