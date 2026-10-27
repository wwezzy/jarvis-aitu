"""Optional local-only text adapter. It has no tools or execution callback.

Ollama must already be running and a local model installed. This module never
downloads a model, starts a shell or reads notes. Explicitly enabled dialogue
context contains only bounded completed local-model exchanges.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


ENDPOINT = "http://127.0.0.1:11434/api/chat"
MAX_RESPONSE_BYTES = 65536
MAX_STREAM_BYTES = 262144
MAX_STREAM_LINE_BYTES = 65536
MAX_STREAM_EVENTS = 2048
MAX_ANSWER_CHARS = 12000
KEEP_ALIVE = "30m"
MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_CHARS = 4000
SYSTEM = (
    "Ты Джарвис, спокойный личный помощник. Говори естественно, ясно и по делу, "
    "по умолчанию 2–4 короткими предложениями, без канцелярита и напоминаний, что ты ИИ. "
    "Если просят подробности, объясни подробнее. Отвечай по-русски, "
    "на английский запрос можно ответить по-английски. Помогай разобраться в мысли, "
    "выбрать один следующий шаг или составить реалистичный план. "
    "У тебя нет инструментов, доступа к компьютеру, файлам или календарю. "
    "Тебе передан текущий вопрос и, только если пользователь включил контекст, "
    "несколько предыдущих реплик локального диалога. Используй факты только из этих сообщений. "
    "Другие заметки, память, календарь и файлы тебе не переданы. "
    "Не утверждай, что выполнил действие, сохранил информацию или изменил настройки. "
    "Команды компьютера выполняет отдельный детерминированный обработчик приложения. "
    "Если просят такое действие, предложи ввести точную команду в приложении. "
    "Не изображай недоступные тебе измерения и не выдумывай результат."
)


class LocalChatUnavailable(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Even localhost redirects must not send personal text off this origin.
        return None


def bounded_history(history=None) -> list[dict]:
    """Validate completed user/assistant pairs and retain bounded recent ones.

    No caller-supplied system/tool role or metadata is accepted. Cropping is
    only context projection; the original local records are never modified.
    """
    if history is None:
        return []
    if not isinstance(history, (list, tuple)) or len(history) > 250 or len(history) % 2:
        raise ValueError("History must contain completed local dialogue pairs")
    for index, message in enumerate(history):
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ValueError("History contains unsupported metadata")
        if message["role"] != ("user" if index % 2 == 0 else "assistant"):
            raise ValueError("History accepts only completed user/assistant pairs")
        content = message["content"]
        if not isinstance(content, str) or not content.strip() or "\x00" in content:
            raise ValueError("History must contain plain nonempty text")
    selected = []
    remaining = MAX_HISTORY_CHARS
    pairs = [history[index:index + 2] for index in range(0, len(history), 2)]
    for user, assistant in reversed(pairs[-(MAX_HISTORY_MESSAGES // 2):]):
        if remaining < 2:
            break
        question = user["content"].strip()[:min(2000, remaining // 2)]
        answer = assistant["content"].strip()[:min(2000, remaining - len(question))]
        selected.append([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
        remaining -= len(question) + len(answer)
    return [message for pair in reversed(selected) for message in pair]


def _final_answer(content: str) -> str:
    """Accept only the final segment of legacy Qwen reasoning wrappers.

    Some local runtimes put reasoning inside ``content`` (including versions
    that omit the opening tag), instead of a separate ``thinking`` field.
    An unfinished wrapper is not a usable final response.
    """
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    if "<think>" in content:
        raise LocalChatUnavailable("Local model did not provide a final answer")
    content = content.strip()
    if not content:
        raise LocalChatUnavailable("Local model did not provide a final answer")
    return content


class LocalChat:
    def __init__(self, model="qwen3:4b-instruct-2507-q4_K_M", *, opener=None, timeout=45):
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}", model) or "cloud" in model.casefold() or "://" in model:
            raise ValueError("A local model name is required")
        self.model = model
        self.timeout = min(90, max(1, timeout))
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @staticmethod
    def _remaining(response, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LocalChatUnavailable("Local response exceeded its time limit")
        # CPython urllib's HTTPResponse wraps SocketIO in a BufferedReader.
        # Tighten the actual socket timeout for each bounded read, so a slow
        # stream cannot reset the whole-response budget on every new line.
        raw = getattr(getattr(response, "fp", None), "raw", None)
        transport = getattr(raw, "_sock", None)
        if transport is not None:
            transport.settimeout(remaining)
        return remaining

    @staticmethod
    def _request(payload):
        return urllib.request.Request(ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/x-ndjson, application/json"}, method="POST")

    @staticmethod
    def _response_origin(response):
        if response.status != 200 or response.geturl() != ENDPOINT:
            raise LocalChatUnavailable("Unexpected local service response")

    def _complete_json(self, response, deadline):
        self._remaining(response, deadline)
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        self._remaining(response, deadline)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LocalChatUnavailable("Local response exceeded the limit")
        data = json.loads(raw)
        if not isinstance(data, dict) or "error" in data:
            raise LocalChatUnavailable("Local response was not a valid object")
        return data

    @staticmethod
    def _message(data, *, streaming=False):
        message = data.get("message")
        if not isinstance(message, dict):
            raise LocalChatUnavailable("Local response was incomplete")
        if message.get("role", "assistant") != "assistant":
            raise LocalChatUnavailable("Unexpected local response role")
        if message.get("tool_calls") not in (None, []):
            raise LocalChatUnavailable("Tool responses are not accepted")
        answer = message.get("content")
        if (not isinstance(answer, str) or "\x00" in answer
                or len(answer) > MAX_ANSWER_CHARS or (not streaming and not answer.strip())):
            raise LocalChatUnavailable("Local response had no usable text")
        return answer

    def _stream(self, response, deadline, on_chunk):
        """Yield provisional display deltas; return only a validated final.

        The receiver must discard preview on failure and replace it with the
        returned final. Streaming text is never a completion/OS grant and must
        not be persisted or spoken before this method returns successfully.
        """
        content, preview = "", ""
        total, finished = 0, False
        # The older Qwen3 thinking variant previously emitted unwrapped drafts
        # even with think=False. It retains final-only previews; the installed
        # non-thinking instruct model can stream normal content immediately.
        wrappers = self.model.casefold().startswith("qwen3:") and "instruct" not in self.model.casefold()

        def emit(delta):
            if not delta:
                return
            try:
                if on_chunk(delta) is False:
                    raise LocalChatUnavailable("Local response cancelled")
            except LocalChatUnavailable:
                raise
            except Exception as error:
                raise LocalChatUnavailable("Local preview receiver unavailable") from error

        events = 0

        def consume(line):
            nonlocal events, content, preview, wrappers, finished
            events += 1
            if events > MAX_STREAM_EVENTS:
                raise LocalChatUnavailable("Local stream exceeded the event limit")
            if len(line) > MAX_STREAM_LINE_BYTES:
                raise LocalChatUnavailable("Local stream exceeded the byte limit")
            if not line.strip():
                return
            if finished:
                raise LocalChatUnavailable("Unexpected data after the final response")
            data = json.loads(line)
            if not isinstance(data, dict) or "error" in data or type(data.get("done")) is not bool:
                raise LocalChatUnavailable("Invalid local stream event")
            chunk = self._message(data, streaming=True)
            if len(content) + len(chunk) > MAX_ANSWER_CHARS:
                raise LocalChatUnavailable("Local stream exceeded the content limit")
            reason = data.get("done_reason")
            if (data["done"] and reason not in {None, "stop"}) or (not data["done"] and reason is not None):
                raise LocalChatUnavailable("Local model did not complete its answer")
            content += chunk
            finished = data["done"]
            if finished:
                return
            # Ignore the separate thinking field completely. Explicit legacy
            # reasoning wrappers suspend previews until final validation.
            wrappers = wrappers or "<think>" in content or "</think>" in content
            if wrappers:
                return
            candidate = content.lstrip()
            # Hold partial tag prefixes crossing NDJSON event boundaries.
            held = max((size for marker in ("<think>", "</think>")
                        for size in range(1, len(marker)) if candidate.endswith(marker[:size])), default=0)
            safe = candidate[:-held] if held else candidate
            if safe.startswith(preview):
                emit(safe[len(preview):])
                preview = safe
        pending = b""
        while True:
            self._remaining(response, deadline)
            # HTTPResponse.read1 performs at most one buffered/raw body read.
            # Unlike readline it returns on a dribbling partial NDJSON line,
            # allowing the whole deadline to be rechecked before another read.
            block = response.read1(min(8192, MAX_STREAM_BYTES - total + 1))
            self._remaining(response, deadline)
            if not block:
                if pending:
                    consume(pending)
                break
            if not isinstance(block, bytes):
                raise LocalChatUnavailable("Invalid local stream transport")
            total += len(block)
            if total > MAX_STREAM_BYTES:
                raise LocalChatUnavailable("Local stream exceeded the byte limit")
            pending += block
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                consume(line)
            if len(pending) > MAX_STREAM_LINE_BYTES:
                raise LocalChatUnavailable("Local stream exceeded the byte limit")
        if not finished:
            raise LocalChatUnavailable("Local stream ended without a final response")
        final = _final_answer(content)
        if final.startswith(preview):
            emit(final[len(preview):])
        elif not preview:
            emit(final)
        self._remaining(response, deadline)
        return final

    def prewarm(self) -> bool:
        """Explicit model-only preload, with no prompt/history or service start.

        Ollama documents empty /api/chat requests for preload and keep_alive
        retention: https://docs.ollama.com/faq#how-can-i-preload-a-model-into-ollama-to-get-faster-response-times
        """
        payload = {"model": self.model, "messages": [], "stream": False, "think": False,
                   "keep_alive": KEEP_ALIVE, "options": {"num_ctx": 4096}}
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener.open(self._request(payload), timeout=self.timeout) as response:
                self._response_origin(response)
                data = self._complete_json(response, deadline)
            message = data.get("message", {})
            return (data.get("done") is True and data.get("done_reason") in {None, "load", "stop"}
                    and isinstance(message, dict) and message.get("role", "assistant") == "assistant"
                    and not message.get("tool_calls")
                    and not message.get("content") and not message.get("thinking"))
        except (LocalChatUnavailable, urllib.error.URLError, OSError, ValueError, TypeError):
            return False

    def answer(self, text: str, history=None, on_chunk=None) -> str:
        if not isinstance(text, str) or not text.strip() or len(text) > 8000 or "\x00" in text:
            raise ValueError("Local question must contain 1..8000 characters")
        if on_chunk is not None and not callable(on_chunk):
            raise ValueError("on_chunk must be a callable")
        payload = {
            "model": self.model, "stream": on_chunk is not None, "think": False, "keep_alive": KEEP_ALIVE,
            "messages": [{"role": "system", "content": SYSTEM}, *bounded_history(history), {"role": "user", "content": text}],
            "options": {"num_predict": 512, "num_ctx": 4096, "temperature": 0.4},
        }
        request = self._request(payload)
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                self._response_origin(response)
                if on_chunk is not None:
                    return self._stream(response, deadline, on_chunk)
                data = self._complete_json(response, deadline)
            if data.get("done") is not True:
                raise LocalChatUnavailable("Local response was incomplete")
            if data.get("done_reason") not in {None, "stop"}:
                raise LocalChatUnavailable("Local model did not complete its answer")
            return _final_answer(self._message(data))
        except (urllib.error.URLError, OSError, ValueError, TypeError) as error:
            # No response body, request content or secret-bearing local errors.
            raise LocalChatUnavailable("Local model unavailable") from error
