"""Deterministic offline personal assistant and explicit action policy.

Only original typed text or local speech may enter handle(). Model, imported
notes and synchronized content are data and cannot authorize actions. Risky
actions additionally require a one-use local button or typed confirmation.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from desktop.actions import ALLOWED_ACTIONS, RISKY_ACTIONS, execute_windows_action
from desktop.store import LocalStore


@dataclass(frozen=True)
class Reply:
    text: str
    action: str | None = None
    confirmation_id: str | None = None
    speak: bool = True


def normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text.casefold().replace("ё", "е").strip()).rstrip(".!?")
    text = re.sub(r"^(?:джарвис|джарсис|джарвиз|jarvis)[,\s]+", "", text)
    text = re.sub(r"^пожалуйста[,\s]+|[,\s]+пожалуйста$", "", text)
    return text.strip()


_ALIASES = {
    "calculator": ("открой калькулятор", "запусти калькулятор", "калькулятор", "open calculator"),
    "notepad": ("открой блокнот", "запусти блокнот", "open notepad"),
    "explorer": ("открой проводник", "открой файлы", "open explorer", "open files"),
    "youtube": ("открой ютуб", "открой youtube", "открой ютьюб", "open youtube"),
    "browser": ("открой браузер", "запусти браузер", "open browser"),
    "volume_up": ("громче", "сделай громче", "увеличь громкость", "volume up", "louder"),
    "volume_down": ("тише", "сделай тише", "уменьши громкость", "volume down", "quieter"),
    "volume_mute": ("без звука", "выключи звук", "отключи звук", "mute"),
    "volume_unmute": ("включи звук", "unmute"),
    "minimize": ("сверни", "сверни окно", "сверни видео", "minimize", "minimize window"),
    "minimize_all": ("сверни все", "сверни все окна", "minimize all"),
    "lock": ("заблокируй компьютер", "заблокируй ноутбук", "заблокируй ноут", "lock computer", "lock pc", "/pclock"),
    "shutdown": ("выключи", "выключить", "выключи компьютер", "выключи ноутбук", "выключи ноут", "выключи мой ноутбук", "shutdown", "shut down computer", "/pcshutdown"),
    "restart": ("перезагрузи компьютер", "перезагрузи ноутбук", "перезагрузи ноут", "перезагрузка", "restart computer", "restart pc", "/pcrestart"),
    "hibernate": ("гибернация", "отправь компьютер в гибернацию", "hibernate", "/pchibernate"),
    "cancel_shutdown": ("отмени выключение", "отмени перезагрузку", "cancel shutdown", "/pccancel"),
}
ALIASES = {alias: action for action, aliases in _ALIASES.items() for alias in aliases}
ACTION_LABELS = {"lock": "блокировку компьютера", "shutdown": "выключение компьютера",
                 "restart": "перезагрузку компьютера", "hibernate": "гибернацию компьютера"}
HELP = ("Могу открыть YouTube, браузер, блокнот, проводник и калькулятор; изменить громкость, свернуть окна. "
        "Попробуй: «добавь задачу закончить проект», «задачи», «готово 1», «следующий шаг», "
        "«запомни важную мысль», «найди проект», «напомни через 10 минут сделать перерыв», "
        "«фокус 25 минут», «напоминания» или «отмени таймер 1». Управление питанием — только с отдельным подтверждением. "
        "Задачи и заметки сохраняются на этом компьютере; Render и ИИ для этих команд не нужны.")

_UNITS = {
    "секунда": 1, "секунду": 1, "секунды": 1, "секунд": 1, "сек": 1, "second": 1, "seconds": 1,
    "минута": 60, "минуту": 60, "минуты": 60, "минут": 60, "мин": 60, "minute": 60, "minutes": 60,
    "час": 3600, "часа": 3600, "часов": 3600, "hour": 3600, "hours": 3600,
}
_ONES = {"один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
         "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
_SMALL = {**_ONES, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
          "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
          "восемнадцать": 18, "девятнадцать": 19}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
         "семьдесят": 70, "восемьдесят": 80, "девяносто": 90}
_HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
             "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900}
_DURATION = r"(.{1,64}?)\s+(" + "|".join(_UNITS) + r")"


def _duration(count: str, unit: str) -> int:
    count, unit = count.casefold(), unit.casefold()
    if re.fullmatch(r"[0-9]{1,7}", count):
        number = int(count)
    else:
        words = count.split()
        number = _HUNDREDS.get(words[0], 0) if words else 0
        if number:
            words = words[1:]
        if len(words) == 1 and words[0] in {**_SMALL, **_TENS}:
            number += {**_SMALL, **_TENS}[words[0]]
        elif len(words) == 2 and words[0] in _TENS and words[1] in _ONES:
            number += _TENS[words[0]] + _ONES[words[1]]
        elif words or not number:
            raise ValueError("Use one positive whole number")
    seconds = number * _UNITS[unit]
    if not 1 <= seconds <= 7 * 86400:
        raise ValueError("Duration outside the supported range")
    return seconds


class DesktopCore:
    def __init__(self, state_dir: Path, executor=None):
        self.store = LocalStore(state_dir)
        self.executor = executor if executor is not None else execute_windows_action

    def snapshot(self) -> dict:
        return self.store.snapshot()

    def close(self):
        self.store.close()

    def due_reminders(self, now=None) -> list[Reply]:
        """UI polls while running, then displays/speaks literal returned text.

        Claims are durable before UI delivery. Catch-up is limited to 24 hours;
        a reminder is never fed back to handle() or treated as action authority.
        """
        replies = []
        for reminder in self.store.claim_due_reminders(now=now):
            if reminder["kind"] == "focus":
                text = f"Фокус #{reminder['id']} завершён. {reminder['text']} Что удалось сделать? Таймер не означает выполнения задачи — отметь результат сам."
            elif reminder["kind"] == "timer":
                text = f"Таймер #{reminder['id']}: время вышло. {reminder['text']}"
            else:
                text = f"Напоминание #{reminder['id']}: {reminder['text']}"
            replies.append(self._answer(Reply(text)))
        return replies

    def _answer(self, reply: Reply) -> Reply:
        self.store.record("assistant", reply.text)
        return reply

    def _execute(self, action: str) -> Reply:
        if action not in ALLOWED_ACTIONS:
            return self._answer(Reply("Эта команда не входит в разрешенный список."))
        try:
            result = self.executor(action)
        except Exception as error:
            # Fixed error class, never stdout/stderr/paths or personal content.
            return self._answer(Reply(f"Не удалось выполнить действие ({type(error).__name__}). Успех не подтвержден.", action=action))
        if not isinstance(result, str) or not result.strip():
            result = "Обработчик завершился без подтверждения результата. Проверь действие на компьютере."
        return self._answer(Reply(result, action=action))

    def handle(self, text: str, source: str = "typed") -> Reply:
        if source not in {"typed", "voice"}:
            return Reply("Импортированный текст и ответы ИИ не могут запускать команды компьютера.")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000 or "\x00" in text:
            return Reply("Нужна непустая команда длиной до 20 000 символов.")
        self.store.record("user", text.strip())
        value = normalize(text)
        original = re.sub(r"^(?:джарвис|джарсис|джарвиз|jarvis)[,\s]+", "", text.strip(), flags=re.IGNORECASE)
        original = re.sub(r"^пожалуйста[,\s]+", "", original, flags=re.IGNORECASE)
        confirmed = re.fullmatch(r"(?:подтверждаю|подтверди|confirm|/confirm) ([a-f0-9]{32})", value)
        if confirmed:
            return self.confirm(confirmed[1], source=source)
        canceled = re.fullmatch(r"(?:отмена|отмени|cancel|/cancel) ([a-f0-9]{32})", value)
        if canceled:
            return self.cancel(canceled[1])
        action = ALIASES.get(value)
        if action in RISKY_ACTIONS:
            token = secrets.token_hex(16)
            self.store.create_confirmation(token, action, time.time())
            return self._answer(Reply(
                "Подтверди " + ACTION_LABELS[action] + " кнопкой в приложении в течение 60 секунд. "
                + ("Сохрани открытые документы. После выполнения остается 30 секунд на отмену." if action in {"shutdown", "restart"} else "Несохраненная работа может быть прервана."),
                action=action, confirmation_id=token))
        if action:
            return self._execute(action)
        if value in {"джарвис", "jarvis", "да", "привет", "как дела", "ты здесь", "how are you", "hello", "are you there"}:
            return self._answer(Reply("На связи, сэр. Скажи команду или выбери следующий шаг."))
        if value in {"помощь", "команды", "что ты умеешь", "help", "/help"}:
            return self._answer(Reply(HELP))
        if value in {"статус", "статус компьютера", "status", "/pcstatus"}:
            return self._answer(Reply("Локальный помощник работает. Задачи и заметки доступны без сети. "
                                      "Доступность микрофона и голоса показана в настройках приложения."))
        reminder = re.fullmatch(r"напомни(?: мне)? через " + _DURATION + r"\s+(.+)", original, re.DOTALL | re.IGNORECASE)
        timer = re.fullmatch(r"(?:(?:поставь|запусти)\s+)?(таймер|фокус|timer|focus)(?:\s+на)?\s+" + _DURATION + r"[.!?]?(?:\s+(.+))?", original, re.DOTALL | re.IGNORECASE)
        if reminder or timer:
            try:
                if reminder:
                    seconds = _duration(reminder[1], reminder[2])
                    kind, content = "reminder", reminder[3]
                else:
                    seconds = _duration(timer[2], timer[3])
                    kind = "focus" if timer[1].casefold() in {"фокус", "focus"} else "timer"
                    content = timer[4] or ("Один выбранный шаг." if kind == "focus" else "Сделай паузу.")
                now = time.time()
                reminder_id = self.store.create_reminder(content, now + seconds, kind, now=now)
            except ValueError:
                return self._answer(Reply("Нужен целый интервал от 1 секунды до 7 дней и текст до 1000 символов. Одновременно можно сохранить до 20 напоминаний."))
            return self._answer(Reply(f"Сохранил {'фокус' if kind == 'focus' else 'таймер' if kind == 'timer' else 'напоминание'} #{reminder_id}, через {seconds} сек. Сработает, пока Jarvis запущен; после паузы догонит только последние 24 часа."))
        if value in {"напоминания", "таймеры", "reminders", "timers"}:
            pending = self.snapshot()["reminders"]
            if not pending:
                return self._answer(Reply("Активных напоминаний и таймеров нет."))
            now = time.time()
            lines = [f"#{item['id']} · через {max(0, round(item['due_at'] - now))} сек. · {item['text']}" for item in pending]
            return self._answer(Reply("\n".join(lines)))
        cancel_timer = re.fullmatch(r"(?:отмени таймер|отмени напоминание|отмени фокус|cancel timer)\s+#?([0-9]{1,10})", value)
        if cancel_timer:
            changed = self.store.cancel_reminder(int(cancel_timer[1]))
            return self._answer(Reply("Отменил напоминание до срабатывания." if changed else "Такого активного напоминания нет: оно уже сработало, отменено или не найдено."))
        if value.startswith(("напомни через", "напомни мне через", "таймер ", "фокус ", "timer ", "focus ",
                             "поставь таймер", "запусти таймер", "запусти фокус", "поставь фокус")):
            # Malformed timer syntax never falls through to an optional model.
            return self._answer(Reply("Укажи целый интервал: «напомни через 10 минут текст», «таймер 25 минут» или «фокус 25 минут». Поддерживаются секунды, минуты и часы, от 1 секунды до 7 дней."))
        task = re.match(r"^(?:добавь задачу|добавить задачу|задача|add task|task)\s+(.+)$", text.strip(), re.IGNORECASE | re.DOTALL)
        note = re.match(r"^(?:запомни|заметка|сохрани заметку|capture|note|remember)\s+(.+)$", text.strip(), re.IGNORECASE | re.DOTALL)
        # Prefix stripping preserves source content/case instead of storing the
        # normalized form. A named request works like the same direct request.
        if original != text.strip():
            task = re.match(r"^(?:добавь задачу|добавить задачу|задача|add task|task)\s+(.+)$", original, re.IGNORECASE | re.DOTALL)
            note = re.match(r"^(?:запомни|заметка|сохрани заметку|capture|note|remember)\s+(.+)$", original, re.IGNORECASE | re.DOTALL)
        try:
            if task:
                task_id = self.store.add_task(task[1])
                return self._answer(Reply(f"Добавил задачу #{task_id}: {task[1].strip()}"))
            if note:
                note_id = self.store.capture_note(note[1])
                return self._answer(Reply(f"Сохранил заметку #{note_id} на этом компьютере."))
        except ValueError:
            return self._answer(Reply("Задача должна содержать от 1 до 1000 символов, заметка — от 1 до 20 000."))
        done = re.fullmatch(r"(?:готово|выполнено|заверши задачу|complete task|done)\s+#?(\d{1,10})", value)
        if done:
            result = self.store.complete_task(int(done[1]))
            return self._answer(Reply({"missing": "Такой активной задачи нет.", "already_done": "Эта задача уже выполнена.",
                                       "completed": "Отметил задачу выполненной."}[result]))
        archived = re.fullmatch(r"(?:удали задачу|архивируй задачу|archive task)\s+#?(\d{1,10})", value)
        if archived:
            changed = self.store.archive_task(int(archived[1]))
            return self._answer(Reply("Убрал задачу из списка. Запись сохранена в локальном архиве." if changed else "Такой активной задачи нет."))
        if value in {"задачи", "мои задачи", "список задач", "tasks", "/tasks"}:
            tasks = [task for task in self.snapshot()["tasks"] if not task["done"]]
            text = "\n".join(f"#{task['id']} · {task['title']}" for task in tasks[:20]) if tasks else "Активных задач нет. Добавь одну конкретную задачу."
            return self._answer(Reply(text))
        if value in {"следующий шаг", "что дальше", "что мне делать", "мне скучно", "next", "/next"}:
            tasks = [task for task in self.snapshot()["tasks"] if not task["done"]]
            text = (f"Следующий шаг: {tasks[0]['title']}. Выдели 10 минут на начало. "
                    "После работы отметь результат сам — я не засчитываю время как выполнение.") if tasks else "Выбери одно небольшое дело и добавь его в задачи. Начни с 10 минут; отдых тоже можно запланировать."
            return self._answer(Reply(text))
        search = re.fullmatch(r"(?:найди заметку|найди|find|search)\s+(.+)", value)
        if search:
            if len(search[1]) > 500:
                return self._answer(Reply("Для поиска достаточно запроса до 500 символов."))
            notes = self.store.search_notes(search[1])
            text = "\n\n".join(f"#{note['id']} · {note['text'][:1200]}" for note in notes[:10]) if notes else "Подходящих заметок нет."
            return self._answer(Reply(text))
        if value in {"заметки", "мои заметки", "notes", "/notes"}:
            notes = self.snapshot()["notes"]
            text = "\n\n".join(f"#{note['id']} · {note['text'][:1200]}" for note in notes[:10]) if notes else "Заметок пока нет. Скажи «запомни» и свою мысль."
            return self._answer(Reply(text))
        preferences = self.store.preferences()
        if preferences["local_chat_enabled"]:
            from desktop.chat import LocalChat, LocalChatUnavailable
            try:
                # A model's result is display-only. Never call handle()/execute
                # on it, and never send stored notes or chat history implicitly.
                answer = LocalChat(preferences["local_chat_model"]).answer(text)
            except ValueError:
                return self._answer(Reply("Для локальной модели сократи вопрос до 8000 символов и проверь имя модели в настройках."))
            except LocalChatUnavailable:
                return self._answer(Reply("Локальная модель сейчас недоступна. Проверь, что Ollama запущена и выбранная локальная модель установлена. Команды, задачи и заметки продолжают работать."))
            return self._answer(Reply("Локальная модель: " + answer))
        return self._answer(Reply("Не распознал точную команду. " + HELP))

    def confirm(self, token: str, source: str = "button") -> Reply:
        if source not in {"button", "typed"}:
            return self._answer(Reply("Для этого действия требуется кнопка подтверждения или исходная текстовая команда. Голос не подтверждает управление питанием."))
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            return self._answer(Reply("Неверное подтверждение. Запроси действие заново."))
        action = self.store.claim_confirmation(token, time.time())
        if action not in RISKY_ACTIONS:
            return self._answer(Reply("Подтверждение истекло, отменено или уже использовано. Запроси действие заново."))
        return self._execute(action)

    def cancel(self, token: str) -> Reply:
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            return self._answer(Reply("Неверный код отмены."))
        if self.store.cancel_confirmation(token):
            return self._answer(Reply("Запрос отменен до выполнения."))
        return self._answer(Reply("Этот запрос уже использован или отменен. Для запланированного выключения: «отмени выключение»."))
