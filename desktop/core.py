"""Deterministic offline personal assistant and explicit action policy.

Only original typed text or local speech may enter handle(). Model, imported
notes and synchronized content are data and cannot authorize actions. Risky
actions additionally require a one-use local button or typed confirmation.
"""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from desktop.actions import ALLOWED_ACTIONS, RISKY_ACTIONS, execute_windows_action
from desktop.store import LocalStore


@dataclass(frozen=True)
class Reply:
    text: str
    action: str | None = None
    confirmation_id: str | None = None
    speak: bool = True


MAX_REPLY_CHARS = 22000
NOTE_PREVIEW_CHARS = 1200
PERSONAL_COMMANDS = {
    "расписание": "schedule", "мое расписание": "schedule", "schedule": "schedule", "my schedule": "schedule",
    "что у меня сегодня": "today", "сегодня": "today", "today": "today", "my day": "today",
    "дедлайны": "assignments", "мои дедлайны": "assignments", "deadlines": "assignments",
    "моя память": "memory", "что ты обо мне помнишь": "memory", "what do you remember about me": "memory", "my memory": "memory",
}
_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _note_previews(notes: list[dict]) -> str:
    """Bound display only; source notes in SQLite remain complete and literal."""
    lines = ["Предпросмотр заметок. Полный текст — в разделе «Заметки»."]
    for note in notes[:10]:
        content = note["text"]
        preview = content[:NOTE_PREVIEW_CHARS]
        if len(content) > NOTE_PREVIEW_CHARS:
            preview += "… [краткий фрагмент]"
        lines.append(f"#{note['id']} · {preview}")
    if len(notes) > 10:
        lines.append("Показаны первые 10 результатов; уточни запрос для поиска других заметок.")
    return "\n\n".join(lines)


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
    "music_open": ("открой яндекс музыку", "открой яндекс музыку в браузере", "open yandex music"),
    "music_play": ("включи музыку", "включить музыку", "возобнови музыку", "play music", "resume music"),
    "music_morning": ("включи утреннюю музыку", "утренняя музыка", "play morning music"),
    "volume_up": ("громче", "сделай громче", "увеличь громкость", "volume up", "louder"),
    "volume_down": ("тише", "сделай тише", "уменьши громкость", "volume down", "quieter"),
    "volume_mute": ("без звука", "выключи звук", "отключи звук", "mute"),
    "volume_unmute": ("включи звук", "unmute"),
    "minimize": ("сверни", "сверни окно", "сверни видео", "minimize", "minimize window"),
    "minimize_all": ("сверни все", "сверни все окна", "minimize all"),
    "maximize": ("разверни окно", "разверни активное окно", "maximize window", "maximize current window"),
    "maximize_youtube": ("разверни ютуб", "разверни youtube", "разверни ютьюб", "maximize youtube"),
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
        "«Открой Яндекс Музыку», «включи музыку» и «включи утреннюю музыку» различают открытие плеера и подтверждённое воспроизведение. "
        "«Разверни окно» разворачивает активное окно, «разверни ютуб» — одно найденное окно браузера с YouTube в заголовке. "
        "Попробуй: «добавь задачу закончить проект», «задачи», «готово 1», «следующий шаг», "
        "«запомни важную мысль», «найди проект», «напомни через 10 минут сделать перерыв», "
        "«фокус 25 минут», «напоминания», «отмени таймер 1», «расписание», «дедлайны» или «моя память». Управление питанием — только с отдельным подтверждением. "
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
    def __init__(self, state_dir: Path, executor=None, *, personal_path: Path | None = None):
        self.store = LocalStore(state_dir)
        self.executor = executor if executor is not None else execute_windows_action
        # A core used in tests/smoke mode never discovers global personal files.
        # Only the actual entry point may opt in with an identified local copy.
        self.personal_path = Path(personal_path) if personal_path is not None else None

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
        if len(reply.text) > MAX_REPLY_CHARS:
            label = "\n\n[Краткий фрагмент ответа: достигнут лимит чата. Исходные заметки сохранены полностью в разделе «Заметки».]"
            reply = replace(reply, text=reply.text[:MAX_REPLY_CHARS - len(label)] + label)
        self.store.record("assistant", reply.text)
        return reply

    def _personal_snapshot(self):
        if self.personal_path is None:
            return None
        from desktop.personal import load_personal_snapshot
        return load_personal_snapshot(self.personal_path)

    @staticmethod
    def _personal_label(snapshot):
        try:
            date = datetime.strptime(snapshot["snapshot_date"], "%Y-%m-%d").strftime("%d.%m.%Y")
        except (KeyError, ValueError, TypeError):
            return "Историческая копия; дата требует проверки. Изменения позже не учтены."
        return f"Копия от {date}; изменения позже не учтены."

    def _personal_reply(self, kind: str) -> Reply:
        from desktop.personal import display_due, safe_text
        snapshot = self._personal_snapshot()
        if snapshot is None:
            return Reply("Локальная копия личных данных не подключена. Созданные здесь задачи и заметки доступны отдельно; подключение копии показано в разделе «Мои данные».")
        status = snapshot.get("status")
        if status == "selection_required" or status == "identity_missing":
            return Reply("Не могу безопасно определить владельца личной копии. Данные не показаны; проверь раздел «Мои данные».")
        if status != "ready":
            return Reply("Локальная копия личных данных не найдена или недоступна. Проверь раздел «Мои данные». Созданные здесь задачи и заметки продолжают работать.")
        label = self._personal_label(snapshot)
        section = "schedule" if kind == "today" else kind
        if section in snapshot.get("unavailable", []):
            return Reply(label + "\nЭтот раздел в копии недоступен; данные не подставлены из других источников.")
        items = snapshot.get(section, [])
        lines = [label]
        if kind in {"schedule", "today"}:
            items = [item for item in items if type(item.get("weekday")) is int and 0 <= item["weekday"] <= 6]
            if kind == "today":
                today = datetime.fromtimestamp(time.time(), timezone(timedelta(hours=5)))
                items = [item for item in items if item["weekday"] == today.weekday()]
                lines.append(f"Повторяющиеся записи копии на {today:%d.%m.%Y} (UTC+5):")
            else:
                lines.append("Повторяющееся расписание из копии:")
            for item in items[:8]:
                start, end = str(item.get("start", "")), str(item.get("end", ""))
                valid = all(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) for value in (start, end))
                hours = f"{start}–{end}" if valid else "время требует проверки"
                lines.append(f"{_WEEKDAYS[item['weekday']]} · {hours} · {safe_text(item.get('title'), 250)}")
            if not items:
                lines.append("Для этого запроса записей в копии нет; это не подтверждает отсутствие занятий сейчас.")
        elif kind == "assignments":
            lines.append("Исторические незавершённые задания; проверь актуальность сроков и статусов:")
            for item in items[:8]:
                title = safe_text(item.get("title"), 300)
                course = safe_text(item.get("course"), 80)
                lines.append(f"{title}" + (f" · {course}" if course else "") + f" · {display_due(item.get('due_at'))}")
            if not items:
                lines.append("В копии таких заданий нет; новые дедлайны после её даты неизвестны.")
        else:
            if snapshot.get("name"):
                lines.append("Имя в копии: " + safe_text(snapshot["name"], 100))
            lines.append("Сохранённые сведения из копии:")
            for item in items[:8]:
                lines.append(f"{safe_text(item.get('key'), 120)}: {safe_text(item.get('value'), 350)}")
            if not items:
                lines.append("Сохранённых сведений в этом разделе копии нет.")
        if len(items) > 8:
            lines.append("Показаны первые 8 записей. Остальные — в разделе «Мои данные».")
        return Reply("\n".join(lines))

    def _historical_next_step(self):
        from desktop.personal import safe_text
        snapshot = self._personal_snapshot()
        if not snapshot or snapshot.get("status") != "ready":
            return None
        pending = [*snapshot.get("tasks", []), *snapshot.get("assignments", [])]
        if not pending:
            return None
        lines = [self._personal_label(snapshot),
                 "В старой копии есть незавершённые записи. Сначала проверь, какие ещё актуальны; я не считаю их текущими задачами автоматически:"]
        lines.extend("· " + safe_text(item.get("title"), 250) for item in pending[:3])
        lines.append("Выбери одну актуальную и добавь её в новый локальный список. Остальные старые записи — в разделе «Мои данные».")
        return "\n".join(lines)

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

    def handle(self, text: str, source: str = "typed", *, on_chunk=None) -> Reply:
        if source not in {"typed", "voice"}:
            return Reply("Импортированный текст и ответы ИИ не могут запускать команды компьютера.")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000 or "\x00" in text:
            return Reply("Нужна непустая команда длиной до 20 000 символов.")
        request_id = self.store.record("user", text.strip())
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
        if value in PERSONAL_COMMANDS:
            return self._answer(self._personal_reply(PERSONAL_COMMANDS[value]))
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
            text = "\n".join(f"#{task['id']} · {task['title']}" for task in tasks[:20]) if tasks else "В локальном списке нет активных задач. Добавь одну конкретную задачу."
            return self._answer(Reply(text))
        if value in {"следующий шаг", "что дальше", "что мне делать", "мне скучно", "next", "/next"}:
            tasks = [task for task in self.snapshot()["tasks"] if not task["done"]]
            if tasks:
                text = (f"Следующий шаг: {tasks[0]['title']}. Выдели 10 минут на начало. "
                        "После работы отметь результат сам — я не засчитываю время как выполнение.")
            else:
                text = self._historical_next_step() or "Выбери одно небольшое дело и добавь его в задачи. Начни с 10 минут; отдых тоже можно запланировать."
            return self._answer(Reply(text))
        search = re.fullmatch(r"(?:найди заметку|найди|find|search)\s+(.+)", value)
        if search:
            if len(search[1]) > 500:
                return self._answer(Reply("Для поиска достаточно запроса до 500 символов."))
            notes = self.store.search_notes(search[1])
            text = _note_previews(notes) if notes else "Подходящих заметок нет."
            return self._answer(Reply(text))
        if value in {"заметки", "мои заметки", "notes", "/notes"}:
            notes = self.snapshot()["notes"]
            text = _note_previews(notes) if notes else "Заметок пока нет. Скажи «запомни» и свою мысль."
            return self._answer(Reply(text))
        preferences = self.store.preferences()
        if preferences["local_chat_enabled"]:
            from desktop.chat import LocalChat, LocalChatUnavailable
            try:
                # A model's result stays display-only. Optional context is
                # exclusively earlier completed model-chat exchanges, never
                # note/task/PC-command/personal-data records or the current row.
                client = LocalChat(preferences["local_chat_model"])
                options = {"on_chunk": on_chunk} if on_chunk is not None else {}
                if preferences["local_chat_history"]:
                    history = self.store.model_chat_history(before_id=request_id)
                    answer = client.answer(text, history=history, **options)
                else:
                    answer = client.answer(text, **options)
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
