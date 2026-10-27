"""Optional local-only text adapter. It has no tools or execution callback.

Ollama must already be running and a local model installed. This module never
downloads a model, starts a shell or reads notes. Explicitly enabled dialogue
context contains only bounded completed local-model exchanges.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request


ENDPOINT = "http://127.0.0.1:11434/api/chat"
MAX_RESPONSE_BYTES = 65536
MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_CHARS = 4000
SYSTEM = (
    "Ты Джарвис, спокойный личный помощник. Отвечай по-русски коротко и по делу, "
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

    def answer(self, text: str, history=None) -> str:
        if not isinstance(text, str) or not text.strip() or len(text) > 8000 or "\x00" in text:
            raise ValueError("Local question must contain 1..8000 characters")
        payload = {
            "model": self.model, "stream": False, "think": False, "keep_alive": "5m",
            "messages": [{"role": "system", "content": SYSTEM}, *bounded_history(history), {"role": "user", "content": text}],
            "options": {"num_predict": 512, "num_ctx": 4096, "temperature": 0.4},
        }
        request = urllib.request.Request(ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200 or response.geturl() != ENDPOINT:
                    raise LocalChatUnavailable("Unexpected local service response")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise LocalChatUnavailable("Local response exceeded the limit")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise LocalChatUnavailable("Local response was not an object")
            message = data.get("message")
            if data.get("done") is not True or not isinstance(message, dict):
                raise LocalChatUnavailable("Local response was incomplete")
            if data.get("done_reason") not in {None, "stop"}:
                raise LocalChatUnavailable("Local model did not complete its answer")
            if message.get("tool_calls"):
                raise LocalChatUnavailable("Tool responses are not accepted")
            answer = message.get("content")
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 12000 or "\x00" in answer:
                raise LocalChatUnavailable("Local response had no usable text")
            return _final_answer(answer)
        except (urllib.error.URLError, OSError, ValueError, TypeError) as error:
            # No response body, request content or secret-bearing local errors.
            raise LocalChatUnavailable("Local model unavailable") from error
