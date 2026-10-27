"""Optional local-only text adapter. It has no tools or execution callback.

Ollama must already be running and a local model installed. This module never
downloads a model, starts a shell, reads notes, or sends conversation history.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request


ENDPOINT = "http://127.0.0.1:11434/api/chat"
MAX_RESPONSE_BYTES = 65536
SYSTEM = (
    "Ты Джарвис, спокойный личный помощник. Отвечай по-русски коротко и по делу, "
    "на английский запрос можно ответить по-английски. Помогай разобраться в мысли, "
    "выбрать один следующий шаг или составить реалистичный план. "
    "У тебя нет инструментов, доступа к компьютеру, файлам или календарю. "
    "Ты не знаешь личных фактов, если пользователь не написал их в этом запросе. "
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


class LocalChat:
    def __init__(self, model="qwen3:4b", *, opener=None, timeout=45):
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}", model) or "cloud" in model.casefold() or "://" in model:
            raise ValueError("A local model name is required")
        self.model = model
        self.timeout = min(90, max(1, timeout))
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def answer(self, text: str) -> str:
        if not isinstance(text, str) or not text.strip() or len(text) > 8000 or "\x00" in text:
            raise ValueError("Local question must contain 1..8000 characters")
        payload = {
            "model": self.model, "stream": False, "think": False, "keep_alive": "5m",
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
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
            if message.get("tool_calls"):
                raise LocalChatUnavailable("Tool responses are not accepted")
            answer = message.get("content")
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 12000 or "\x00" in answer:
                raise LocalChatUnavailable("Local response had no usable text")
            return answer.strip()
        except (urllib.error.URLError, OSError, ValueError, TypeError) as error:
            # No response body, request content or secret-bearing local errors.
            raise LocalChatUnavailable("Local model unavailable") from error
