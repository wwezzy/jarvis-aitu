"""Deterministic offline personal assistant and explicit action policy.

Only original typed text or local speech may enter handle(). Model, imported
notes and synchronized content are data and cannot authorize actions. Risky
actions additionally require a one-use local button or typed confirmation.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from desktop.actions import ALLOWED_ACTIONS, RISKY_ACTIONS, execute_windows_action
from desktop.organizer import LocalOrganizer
from desktop.store import LocalStore


@dataclass(frozen=True)
class Reply:
    text: str
    action: str | None = None
    confirmation_id: str | None = None
    speak: bool = True
    organizer_event: dict | None = None
    media_cue: str | None = None
    followup_id: str | None = None
    speech_text: str | None = None


MAX_REPLY_CHARS = 22000
NOTE_PREVIEW_CHARS = 1200
PERSONAL_COMMANDS = {
    "старое расписание": "schedule", "расписание из копии": "schedule", "archived schedule": "schedule",
    "старое расписание на сегодня": "today", "сегодня из копии": "today", "archived schedule today": "today",
    "дедлайны": "assignments", "мои дедлайны": "assignments", "deadlines": "assignments",
    "моя память": "memory", "что ты обо мне помнишь": "memory", "what do you remember about me": "memory", "my memory": "memory",
}
_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_PERSONAL_TOPIC = re.compile(
    r"\b(?:мой|моего|моей|моему|моем|моя|мое|мои|моих|мне|меня|сегодня|завтра|"
    r"план|плана|планы|планов|плане|плану|цель|цели|целям|зал|зале|"
    r"тренировк\w*|дисциплин\w*|скучно|my|me|myself|today|tomorrow|goal|goals|plan|plans|planning)\b", re.I)


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


def _needs_plan_context(text, history=None):
    value = normalize(text)
    if _PERSONAL_TOPIC.search(value):
        return True
    if value not in {"почему", "почему так", "а почему", "почему именно так", "как это",
                     "с чего начать", "что дальше", "why", "why so", "what next", "how so"}:
        return False
    questions = [message["content"] for message in (history or []) if message["role"] == "user"]
    return bool(questions and _PERSONAL_TOPIC.search(normalize(questions[-1])))


_ALIASES = {
    "calculator": ("открой калькулятор", "запусти калькулятор", "калькулятор", "open calculator"),
    "notepad": ("открой блокнот", "запусти блокнот", "open notepad"),
    "explorer": ("открой проводник", "открой файлы", "open explorer", "open files"),
    "youtube": ("открой ютуб", "открой youtube", "открой ютьюб", "open youtube"),
    "browser": ("открой браузер", "запусти браузер", "open browser"),
    "chrome": ("открой гугл хром", "открой google chrome", "открой хром", "open chrome", "open google chrome"),
    "music_open": ("открой яндекс музыку", "открой яндекс музыку в браузере", "open yandex music"),
    "music_play": ("включи музыку", "включить музыку", "возобнови музыку", "включи песню", "поставь песню",
                   "включи трек", "поставь музыку", "play a song", "play music", "resume music"),
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
HELP = ("Могу открыть YouTube, браузер по умолчанию, Google Chrome, блокнот, проводник и калькулятор; изменить громкость, свернуть окна. "
        "Открываю добавленные тобой приложения и проекты в выбранном редакторе: «открой проект Jarvis», «открой приложение Редактор». Их имена и доступ настраиваются в разделе «Управление». "
        "«Открой Яндекс Музыку», «включи музыку» и «включи утреннюю музыку» различают открытие плеера и подтверждённое воспроизведение. "
        "«Разверни окно» разворачивает активное окно, «разверни ютуб» — одно найденное окно браузера с YouTube в заголовке. "
        "Попробуй: «добавь задачу закончить проект», «задачи», «готово 1», «следующий шаг», "
        "«запомни важную мысль», «найди проект», «напомни через 10 минут сделать перерыв», "
        "«план на завтра», «добавь цель на завтра закончить проект», «я хожу в зал во вт чт вс», "
        "«фокус 25 минут», «напоминания», «отмени таймер 1», «расписание», «дедлайны» или «моя память». Управление питанием — только с отдельным подтверждением. "
        "Задачи и заметки сохраняются на этом компьютере; Render и ИИ для этих команд не нужны.")
HELP_SPOKEN = ("К вашим услугам, сэр. Открываю приложения и проекты, управляю окнами и музыкой. "
               "Сохраняю задачи и заметки, помогаю с планом и напоминаниями. "
               "Например: открой проект Джарвис, запомни мою мысль или покажи план на завтра. Подробности — на экране.")

_APP_NAMES = {
    "браузер": "browser", "мой браузер": "browser", "browser": "browser", "the browser": "browser", "my browser": "browser",
    "google chrome": "chrome", "googlechrome": "chrome", "chrome": "chrome", "the chrome browser": "chrome", "гугл хром": "chrome", "гуглхром": "chrome", "хром": "chrome",
    "калькулятор": "calculator", "calculator": "calculator", "the calculator": "calculator",
    "блокнот": "notepad", "notepad": "notepad",
    "проводник": "explorer", "файлы": "explorer", "explorer": "explorer", "file explorer": "explorer", "files": "explorer",
    "ютуб": "youtube", "ютьюб": "youtube", "youtube": "youtube",
    "яндекс музыка": "music_open", "яндекс музыку": "music_open", "яндекс музыки": "music_open",
    "yandex music": "music_open", "yandexmusic": "music_open", "музыку": "music_open", "music": "music_open",
}
# Finite verbs/targets, never a fuzzy match or an extracted subcommand. Named
# Yandex requests open that player; generic resume may use another music app.
_NATURAL_ACTIONS = {verb + " " + target: action for target, action in _APP_NAMES.items()
                    for verb in ("открой", "открыть", "запусти", "запустить", "включи", "включить", "open", "launch", "start")}
_NATURAL_ACTIONS.update(ALIASES)
for _imperative, _infinitive in (("открой", "открыть"), ("запусти", "запустить"), ("включи", "включить"),
                               ("выключи", "выключить"), ("перезагрузи", "перезагрузить"),
                               ("заблокируй", "заблокировать"), ("сверни", "свернуть"),
                               ("разверни", "развернуть"), ("увеличь", "увеличить"),
                               ("уменьши", "уменьшить"), ("отключи", "отключить"), ("отмени", "отменить")):
    for _phrase, _action in ALIASES.items():
        if _phrase.startswith(_imperative + " "):
            _NATURAL_ACTIONS[_infinitive + _phrase[len(_imperative):]] = _action
_NATURAL_ACTIONS.update({
    "play yandex music": "music_open", "возобнови яндекс музыку": "music_open",
    "возобновить музыку": "music_play", "включить утреннюю музыку": "music_morning",
    "сделай потише": "volume_down", "потише": "volume_down", "убавь громкость": "volume_down",
    "сделай погромче": "volume_up", "погромче": "volume_up", "прибавь громкость": "volume_up",
    "minimize the window": "minimize", "minimize all windows": "minimize_all",
    "maximize the window": "maximize", "mute the sound": "volume_mute", "unmute the sound": "volume_unmute",
    "turn up the volume": "volume_up", "turn the volume up": "volume_up",
    "turn down the volume": "volume_down", "turn the volume down": "volume_down",
    "turn the sound off": "volume_mute", "turn the sound on": "volume_unmute",
})
for _verb, _action in {"shut down": "shutdown", "turn off": "shutdown", "shutdown": "shutdown",
                       "restart": "restart", "lock": "lock", "hibernate": "hibernate"}.items():
    for _target in ("computer", "the computer", "my computer", "pc", "my pc", "laptop", "the laptop", "my laptop"):
        _NATURAL_ACTIONS[_verb + " " + _target] = _action
_CAPABILITIES = {"помощь", "команды", "список команд", "что ты умеешь", "что ты можешь", "что ты можешь делать",
    "что ты умеешь делать", "какие у тебя возможности", "какие у тебя функции", "какие команды ты знаешь",
    "как ты можешь помочь", "чем можешь помочь", "чем ты можешь помочь", "help", "/help",
    "what can you do", "what do you do", "what can you open", "what are your capabilities", "what commands do you know",
    "tell me what you can do", "show commands"}
_POLITE_REQUEST = re.compile(r"^(?:можешь(?: ты)?|будь добр|будь добра|прошу тебя|can you|could you|would you|will you)[\s,]+")
_ACTION_WORDS = re.compile(r"\b(?:откр\w*|запус\w*|включ\w*|выключ\w*|закр\w*|удал\w*|выполн\w*|перезагруз\w*|заблокир\w*|свер\w*|развер\w*|громк\w*|громче|тише|open|launch|start|play|resume|close|delete|run|shutdown|restart|lock|hibernate|mute|unmute|minimize|maximize)\b")
# These whole phrases select only a recorded personal greeting. The final
# homophone is a bounded ASR variant, never an inferred family/OS intent.
_WELCOME_HOME = re.compile(r"(?:(?:wake up )?daddy['’]?s home|(?:дадис|дэддис) хоум|wake up that is home)")


def _intent_phrase(text):
    """Only formatting/address frames; quotes and negation remain intact."""
    value = re.sub(r"\s+", " ", text.casefold().replace("ё", "е").strip()).rstrip(".!?").strip()
    value = re.sub(r"(?<!\w)(?:пожалуйста|please)(?!\w)", " ", value)
    value = re.sub(r"[\s,]+", " ", value).strip()
    value = re.sub(r"^(?:джарвис|джарсис|джарвиз|jarvis)[\s,:.!?—-]+", "", value)
    value = re.sub(r"[\s,]+(?:джарвис|джарсис|джарвиз|jarvis|сэр|sir)$", "", value)
    value = re.sub(r"\b(яндекс|yandex)[.·-]+\s*(?=музык[ауи]\b|music\b)", r"\1 ", value)
    return re.sub(r"[\s,]+", " ", value).strip()


def _desktop_action(phrase):
    if any(mark in phrase for mark in ('"', "'", "`", "«", "»", "“", "”", "‘", "’")):
        return None
    body = _POLITE_REQUEST.sub("", phrase, count=1)
    action = _NATURAL_ACTIONS.get(body)
    # A polite modal without a power target is a capability/clarification
    # question; do not infer a destructive target from it.
    if body != phrase and action in RISKY_ACTIONS and body in {"выключи", "выключить", "shutdown", "hibernate", "гибернация", "перезагрузка"}:
        return None
    return action


def _catalog_request_frame(text):
    """Remove only outer request frames; names remain literal capabilities.

    Jarvis can itself be a project name, and Please Reply is a valid name.
    Never erase words inside a name or treat its last word as an address.
    """
    value = re.sub(r"\s+", " ", text.casefold().replace("ё", "е").strip()).rstrip(".!?").strip()
    for _ in range(2):
        value = re.sub(r"^(?:джарвис|джарсис|джарвиз|jarvis)[\s,:.!?—-]+", "", value)
        value = re.sub(r"^(?:пожалуйста|please)[\s,]+", "", value)
        value = _POLITE_REQUEST.sub("", value, count=1).lstrip(" ,")
    for _ in range(2):
        value = re.sub(r",\s*(?:пожалуйста|please|джарвис|jarvis|сэр|sir)$", "", value).rstrip()
    return value


def _desktop_information(phrase):
    body = _POLITE_REQUEST.sub("", phrase, count=1)
    # Capability requests are read-only. Tolerate a trailing ASR fragment here
    # without ever extracting an executable action from a longer utterance.
    if re.match(r"^(?:что ты (?:умеешь|можешь)|расскажи что ты (?:умеешь|можешь))\b", body):
        return HELP
    if body in _CAPABILITIES or re.fullmatch(
            r"(?:расскажи|рассказать|скажи|сказать|покажи|показать)(?: мне)? (?:что (?:ты )?(?:можешь|умеешь)(?: делать)?|о своих возможностях|про свои возможности)", body):
        return HELP
    if re.fullmatch(r"(?:ты (?:можешь|умеешь)|(?:можешь|умеешь) ли ты|are you able to|do you support|can you actually) .+", phrase):
        return HELP
    if (re.match(r"^(?:чем отличается|в чем разница|what is the difference|what's the difference)\b", phrase)
            and re.search(r"музык|music", phrase)):
        return ("Открыть Яндекс Музыку — запустить плеер. «Включи музыку» — попробовать продолжить музыку в доступном плеере. "
                "О воспроизведении сообщаю только после подтверждения; первый вход и выбор подборки могут потребовать твоего действия.")
    return None


def _dialogue_history(history):
    """Drop obsolete model denials of operational requests from AI context.

    Classification is pure: do not call handle/organizer or mutate the complete
    SQLite history. Only ordinary completed model conversations remain context.
    """
    selected = []
    for index in range(0, len(history) - 1, 2):
        question, answer = history[index:index + 2]
        phrase = _intent_phrase(question["content"])
        if (_desktop_information(phrase) is not None or _desktop_action(phrase) is not None
                or _desktop_clarification(phrase) is not None):
            continue
        selected.extend((dict(question), dict(answer)))
    return selected


def _desktop_clarification(phrase):
    body = _POLITE_REQUEST.sub("", phrase, count=1)
    commandlike = bool(re.match(r"^(?:откр\w*|запус\w*|включ\w*|выключ\w*|закр\w*|удал\w*|выполн\w*|перезагруз\w*|заблокир\w*|свер\w*|развер\w*|open|launch|start|play|resume|close|delete|run|shutdown|restart|lock|hibernate|mute|unmute|minimize|maximize|/pc\w*)\b", body))
    commandlike = commandlike or bool(re.match(r"^(?:сделай|увелич\w*|уменьш\w*|прибав\w*|убав\w*|громче|тише|volume|turn)\b", body)
                                      and re.search(r"громк|громче|тише|звук|volume|sound|computer|laptop|\bpc\b", body))
    nonrequest = bool(re.match(r"^(?:не\b|не надо\b|not\b|don't\b|do not\b|never\b|скажи\b|процитируй\b|цитата\b|в тексте\b|в документе\b|(?:он|она|я|ты) сказал\w*\b|мне сказали\b|the document\b|he said\b|she said\b|say\b|quote\b)", body)
                      or any(mark in body for mark in ('"', "'", "`", "«", "»", "“", "”", "‘", "’")))
    if nonrequest and _ACTION_WORDS.search(body):
        return "В этой формулировке есть отрицание, цитата или описание команды. Для выполнения пришли отдельную прямую просьбу, например «открой браузер»."
    if re.match(r"^(?:сначала|потом|затем|first|firstly|then)\b", body) and _ACTION_WORDS.search(body):
        return "Пришли отдельную прямую команду для одного действия. Составные просьбы не выполняю автоматически."
    if commandlike or body in _APP_NAMES:
        if re.search(r"музык|music|yandex|яндекс", body):
            return "Уточни одно действие: «открой Яндекс Музыку» — открыть плеер; «включи музыку» — попробовать продолжить музыку."
        if re.search(r"звук|громк|volume|sound", body):
            return "Уточни управление звуком: «громче», «тише», «без звука» или «включи звук»."
        if re.match(r"^(?:выключ\w*|перезагруз\w*|заблокир\w*|shut down|turn off|shutdown|restart|lock|hibernate|/pc\w*)\b", body):
            return "Уточни действие с ноутбуком: например «выключи ноутбук» или «перезагрузи ноутбук». Для выполнения потребуется отдельное подтверждение в приложении."
        return ("Укажи одно действие и приложение: «открой браузер», «открой Google Chrome» или «открой Яндекс Музыку». "
                "Чтобы узнать доступные команды, спроси «что ты умеешь?».")
    if re.match(r"^(?:что ты (?:умеешь|можешь)|расскажи что ты (?:умеешь|можешь)|what can you)\b", body):
        return "Уточни вопрос о возможностях: например «что ты умеешь?» или «какие команды ты знаешь?»."
    return None

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
    def __init__(self, state_dir: Path, executor=None, *, personal_path: Path | None = None, computer_controls=None):
        self.state_dir = Path(state_dir).absolute()
        self._closed = threading.Event()
        self._request_context = threading.local()
        self._morning_lock, self._morning_pending = threading.RLock(), {}
        self.store = LocalStore(state_dir)
        self.organizer = LocalOrganizer(self.store)
        from desktop.observations import ObservationMonitor
        self.observations = ObservationMonitor(self.store)
        from desktop.usage import UsageLedger
        self.usage = UsageLedger(self.store)
        from desktop.owner_auth import OwnerSession
        from desktop.project_catalog import ProjectCatalog
        from desktop.app_catalog import AppCatalog
        self.owner = OwnerSession(enabled=self.store.preferences()["voice_owner_required"])
        self.projects = ProjectCatalog(state_dir)
        self.apps = AppCatalog(state_dir)
        if computer_controls is None and executor is None:
            from desktop.computer_controls import ComputerControls
            computer_controls = ComputerControls(self.apps)
        self.computer = computer_controls
        from desktop.obsidian import ObsidianVault
        self.obsidian = ObsidianVault(state_dir)
        self._native_executor = executor is None
        self.executor = executor if executor is not None else execute_windows_action
        # A core used in tests/smoke mode never discovers global personal files.
        # Only the actual entry point may opt in with an identified local copy.
        self.personal_path = Path(personal_path) if personal_path is not None else None

    def snapshot(self) -> dict:
        value = self.store.snapshot()
        value["observations"] = self.observations.summary(enabled=value["preferences"]["activity_monitor_enabled"])
        return value

    def observation_summary(self):
        return self.observations.summary(enabled=self.store.preferences()["activity_monitor_enabled"])

    def usage_snapshot(self):
        return self.usage.snapshot()

    def _record_chat_usage(self, model, client, *, success):
        try:
            self.usage.record(model, getattr(client, "last_usage", None), success=success)
        except Exception:
            pass  # Optional accounting must not discard an otherwise usable reply.

    def poll_observations(self):
        try:
            return self.observations.tick(enabled=self.store.preferences()["activity_monitor_enabled"])
        except Exception:
            # Optional observations must never prevent deterministic commands.
            self.observations.reset()
            return False

    def reset_observations(self):
        self.observations.reset()

    def clear_observations(self):
        self.observations.clear()

    def _observation_reply(self):
        data = self.observation_summary()
        if not data["observed_seconds"]:
            return Reply("Сохранённых интервалов активности за сегодня пока нет. Наблюдение "
                         + ("разрешено" if data["enabled"] else "выключено")
                         + ". По открытому приложению нельзя надёжно подтвердить учёбу или тренировку.")
        labels = {"work": "рабочие программы", "gaming": "игровые программы", "media": "медиаплееры", "idle": "бездействие", "unknown": "неизвестно"}
        totals = "; ".join(f"{labels[key]}: {round(seconds / 60, 1)} мин" for key, seconds in data["seconds"].items())
        return Reply(f"Наблюдения {data['date']} (UTC+5), только за записанные интервалы: {totals}. "
                     "Это приблизительные категории, а не отчёт о выполнении. Браузер остаётся неизвестным; рабочая программа не доказывает учёбу, компьютер не подтверждает зал. "
                     + ("Сбор включён." if data["enabled"] else "Сбор приостановлен."))

    def _current_plan_context(self, preferences):
        """Small explicit model projection; never notes, files or PC authority."""
        snapshot = self.organizer.snapshot()
        result = {}
        for name in ("today", "tomorrow"):
            day = snapshot[name]
            result[name] = {"date": day["date"], "items": [
                {"title": item["title"][:80], "time": item["time"], "status": item["status"],
                 "provenance": item["provenance"] or "unknown"}
                for item in day["items"][:6]]}
        gym = self.organizer.gym_settings()
        result["gym_days"] = gym["days"] if gym.get("enabled") else []
        result["observations"] = None
        if preferences["activity_monitor_enabled"]:
            hints = self.observation_summary()
            result["observations"] = {
                "work_hint_minutes": int(hints["seconds"]["work"] // 60),
                "gaming_minutes": int(hints["seconds"]["gaming"] // 60),
                "observed_minutes": int(hints["observed_seconds"] // 60)}
        return result

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self.cancel_morning()
        if self.computer is not None:
            self.computer.close()
        self.obsidian.close()
        self.owner.revoke()
        self.observations.reset()
        self.store.close()

    def _catalog_authorized(self):
        try:
            return (not self._request_cancelled() and not getattr(self._request_context, "chat_only", False)
                    and (not self.owner.enabled or self.owner.allowed() is True))
        except Exception:
            return False

    def open_catalog_item(self, kind, record_id, *, source="button"):
        """Trusted local selection uses an ID, never a generated chat command."""
        if (source != "button" or not isinstance(kind, str) or kind not in {"project", "app"}
                or type(record_id) is not int or record_id < 1):
            return Reply("Выбери проект или приложение в разделе «Управление».")
        if not self._catalog_authorized():
            return Reply("Сначала подтверди владельца кнопкой Windows Hello в разделе «Управление».")
        opener = self.projects.open_project_id if kind == "project" else self.apps.open_app_id
        result = opener(record_id, authorization_guard=self._catalog_authorized)
        return self._answer(Reply(result["message"]))

    def set_owner_required(self, enabled):
        if type(enabled) is not bool:
            return False
        if self.owner.enabled and not enabled and not self.owner.allowed():
            return False
        self.store.set_preferences({"voice_owner_required": enabled, **({"gestures_enabled": False} if not enabled else {})})
        self.owner.set_enabled(enabled)
        return True

    def approve_remote(self, enabled):
        if self._closed.is_set() or type(enabled) is not bool or enabled and not (self.owner.enabled and self.owner.allowed()):
            return False
        self.store.set_preferences({"remote_control_approved": enabled})
        return True

    def request_control(self, action, *, source="remote", token=None, cancel=False):
        """Trusted paired-phone adapter; no text or model action extraction."""
        if self._closed.is_set() or not isinstance(action, str) or action not in ALLOWED_ACTIONS or type(cancel) is not bool:
            return Reply("Неподдерживаемое действие.", speak=False)
        if source == "gesture":
            return Reply("Управление жестами удалено.", speak=False)
        preferences = self.store.preferences()
        if source == "remote":
            # The HTTP adapter separately verifies both owner-device secrets.
            if not preferences["remote_control_approved"]:
                return Reply("Удалённое управление не разрешено владельцем.", speak=False)
            if token is not None:
                if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
                    return Reply("Неверное подтверждение.", speak=False)
                claimed = self.store.claim_confirmation(token, time.time())
                if claimed != action or claimed not in RISKY_ACTIONS:
                    return Reply("Подтверждение не соответствует действию, истекло или уже использовано.", speak=False)
                if cancel:
                    return self._answer(Reply("Удалённый запрос отменён до выполнения.", speak=False))
                return self._execute(claimed)
        else:
            return Reply("Источник управления не разрешён.", speak=False)
        if cancel:
            return Reply("Нет подтверждения для отмены.", speak=False)
        return self._request_action(action)

    def _request_action(self, action):
        if action in RISKY_ACTIONS:
            token = secrets.token_hex(16)
            self.store.create_confirmation(token, action, time.time())
            return self._answer(Reply(
                "Подтверди " + ACTION_LABELS[action] + " кнопкой в приложении в течение 60 секунд. "
                + ("Сохрани открытые документы. После выполнения остается 30 секунд на отмену." if action in {"shutdown", "restart"} else "Несохраненная работа может быть прервана."),
                action=action, confirmation_id=token))
        return self._execute(action)

    def due_events(self, now=None, trigger="tick") -> list[Reply]:
        """Local plan notices are literal data, never passed to handle/model.

        Queued delivery must recheck event_is_current immediately before each
        notification/speech operation; current rules may have changed meanwhile.
        """
        return [self._answer(Reply(event["text"], organizer_event=event))
                for event in self.organizer.due_events(now=now, trigger=trigger)]

    def event_is_current(self, reply, *, now=None):
        event = reply.organizer_event if isinstance(reply, Reply) else reply
        return self.organizer.event_is_current(event, now=now)

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
        if self._request_cancelled():
            return Reply("Запрос отменён.", speak=False)
        if len(reply.text) > MAX_REPLY_CHARS:
            label = "\n\n[Краткий фрагмент ответа: достигнут лимит чата. Исходные заметки сохранены полностью в разделе «Заметки».]"
            reply = replace(reply, text=reply.text[:MAX_REPLY_CHARS - len(label)] + label)
        if not self._closed.is_set():
            self.store.record("assistant", reply.text)
        return reply

    def _request_cancelled(self):
        event = getattr(self._request_context, "cancel_event", None)
        return self._closed.is_set() or event is not None and event.is_set()

    def request_kind(self, text, source="typed"):
        """Pure admission classification; it never dispatches an action.

        Only positively free-form requests enter the separate chat lane. All
        action-looking input remains on the serialized command lane, including
        invalid/quoted/negated requests that require a clarification.
        """
        if (not isinstance(source, str) or source not in {"typed", "voice"}
                or not isinstance(text, str) or not text.strip() or len(text) > 20000 or "\x00" in text):
            return "command"
        value, intent = normalize(text), _intent_phrase(text)
        if value in {"стоп", "замолчи", "хватит", "отмена", "stop", "cancel", "отмени ответ"} or intent in {"стоп", "замолчи", "хватит", "отмена", "stop", "cancel", "отмени ответ"}:
            return "stop"
        known = {"доброе утро", "good morning", "помощь", "команды", "что ты умеешь", "help", "/help",
                 "джарвис", "jarvis", "да", "привет", "как дела", "как твои дела", "как у тебя дела",
                 "ты здесь", "ты тут", "how are you", "hello", "are you there", "статус", "статус компьютера", "status", "/pcstatus",
                 "ты меня слышишь", "слышишь меня", "меня слышно", "can you hear me", "do you hear me",
                 "проекты", "мои проекты", "projects", "приложения", "мои приложения", "apps",
                 "моя активность", "активность", "наблюдения", "что видно по компьютеру", "учусь ли я", "играю ли я",
                 "задачи", "мои задачи", "список задач", "tasks", "/tasks", "заметки", "мои заметки", "notes", "/notes",
                 "следующий шаг", "что дальше", "что мне делать", "мне скучно", "next", "/next",
                 "мой план", "план", "планы", "план на сегодня", "план сегодня", "что у меня сегодня", "сегодня",
                 "план на завтра", "план завтра", "что у меня завтра", "завтра", "напоминания", "таймеры", "reminders", "timers"}
        known.update(PERSONAL_COMMANDS)
        from desktop.computer_controls import parse_command
        if parse_command(text) is not None:
            return "command"
        known.update({"пауза планировщика", "возобнови планировщик", "тихий режим", "отключи тихий режим",
                      "выключи утренний план", "включи утренний план", "выключи вечерний план", "включи вечерний план",
                      "obsidian", "обсидиан", "заметки обсидиан", "заметки obsidian"})
        if value in known or intent in known or _WELCOME_HOME.fullmatch(intent) or _desktop_information(intent) is not None or _desktop_action(intent) is not None:
            return "command"
        if (_ACTION_WORDS.search(intent) or value.startswith("/") or re.match(
            r"^(?:напомни|таймер|фокус|timer|focus|подтверждаю|подтверди|confirm|отмени|отмена|cancel|"
            r"добавь|добавить|запомни|запиши|сохрани|заметка|note|add task|task|capture|remember|готово|выполнено|done|complete task|"
            r"архивируй|archive task|заверши задачу|удали|найди|find|search|запланируй|перенеси|отметь|пропусти|убери|поставь|начни|останови|"
            r"план(?: на)? \d|цель\s+(?:на|#?\d)|(?:я )?(?:качаюсь|хожу в зал|тренируюсь|сходил в зал|потренировался|пропустил зал|учился)\b)", value)):
            return "command"
        return "chat"

    def cancel_morning(self, token=None):
        with self._morning_lock:
            if token is None:
                self._morning_pending.clear()
            elif isinstance(token, str):
                self._morning_pending.pop(token, None)

    def complete_morning(self, token, *, cancel_event=None):
        """One local original request permits music only after its full cue.

        This opaque in-memory token is never generated by a model or persisted
        in chat. A new request, cancellation, expiry, close or revoked owner
        invalidates the continuation. Playback callbacks themselves execute no
        PC commands; the UI submits this method on its serialized action lane.
        """
        if (not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token)
                or cancel_event is not None and not isinstance(cancel_event, type(threading.Event()))):
            return Reply("Утренний сценарий уже не действует.", speak=False)
        with self._morning_lock:
            expires = self._morning_pending.pop(token, None)
        if expires is None or time.monotonic() >= expires or not self._catalog_authorized():
            return Reply("Утренний сценарий отменён или устарел.", speak=False)
        previous = getattr(self._request_context, "cancel_event", None)
        self._request_context.cancel_event = cancel_event
        try:
            result = self._execute("music_morning", authorization_guard=self._catalog_authorized)
            return replace(result, speak=False)
        finally:
            self._request_context.cancel_event = previous

    def _vault_notes_reply(self, query=None):
        """Bound literal local previews; note contents never become authority."""
        status = self.obsidian.status()
        if status.get("configured") is not True:
            return status["message"]
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 500):
            return "Для поиска достаточно запроса до 500 символов."
        notes = self.obsidian.list_notes(limit=11) if query is None else self.obsidian.search(query, limit=11)
        if not notes:
            return "В хранилище Obsidian подходящих заметок нет." if query else "В хранилище Obsidian пока нет заметок."
        lines = ["Заметки Obsidian. Полный текст — в разделе «Заметки»."]
        for item in notes[:10]:
            lines.append(item["title"][:100])
            if query is not None and item.get("preview"):
                preview = item["preview"][:NOTE_PREVIEW_CHARS]
                if item.get("truncated") or len(item["preview"]) > NOTE_PREVIEW_CHARS:
                    preview += "… [краткий фрагмент]"
                lines.append(preview)
        if len(notes) > 10:
            lines.append("Показаны первые 10 результатов; уточни поиск в разделе «Заметки».")
        return "\n\n".join(lines)

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

    def _execute(self, action: str, *, authorization_guard=None) -> Reply:
        if self._request_cancelled() or getattr(self._request_context, "chat_only", False):
            return Reply("Jarvis завершает работу. Повтори команду после запуска.", speak=False)
        if action not in ALLOWED_ACTIONS:
            return self._answer(Reply("Эта команда не входит в разрешенный список."))
        try:
            if authorization_guard is not None and authorization_guard() is not True:
                return Reply("Доступ отозван. Действие отменено.", speak=False)
            if self._native_executor:
                canceled = getattr(self._request_context, "cancel_event", None)
                result = self.executor(action, authorization_guard=lambda:
                    not self._closed.is_set() and (canceled is None or not canceled.is_set())
                    and (authorization_guard is None or authorization_guard() is True))
            else:
                result = self.executor(action)
        except Exception as error:
            # Fixed error class, never stdout/stderr/paths or personal content.
            return self._answer(Reply(f"Не удалось выполнить действие ({type(error).__name__}). Успех не подтвержден.", action=action))
        if not isinstance(result, str) or not result.strip():
            result = "Обработчик завершился без подтверждения результата. Проверь действие на компьютере."
        return self._answer(Reply(result, action=action))

    def handle(self, text: str, source: str = "typed", *, on_chunk=None, cancel_event=None) -> Reply:
        if cancel_event is not None and not isinstance(cancel_event, type(threading.Event())):
            raise ValueError("A cancellation event is required")
        previous = getattr(self._request_context, "cancel_event", None)
        previous_chat = getattr(self._request_context, "chat_only", False)
        self._request_context.cancel_event = cancel_event
        self._request_context.chat_only = self.request_kind(text, source) == "chat"
        try:
            if self._request_cancelled():
                return Reply("Запрос отменён.", speak=False)
            self.cancel_morning()
            return self._handle_request(text, source=source, on_chunk=on_chunk)
        finally:
            self._request_context.cancel_event = previous
            self._request_context.chat_only = previous_chat

    def _handle_request(self, text: str, source: str = "typed", *, on_chunk=None) -> Reply:
        if self._closed.is_set():
            return Reply("Jarvis завершает работу. Повтори команду после запуска.", speak=False)
        if not isinstance(source, str) or source not in {"typed", "voice"}:
            return Reply("Импортированный текст и ответы ИИ не могут запускать команды компьютера.")
        if self.owner.enabled and not self.owner.allowed():
            return Reply("Сначала подтверди владельца кнопкой Windows Hello в разделе «Управление».")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000 or "\x00" in text:
            return Reply("Нужна непустая команда длиной до 20 000 символов.")
        request_id = self.store.record("user", text.strip())
        value = normalize(text)
        if value in {"моя активность", "активность", "наблюдения", "что видно по компьютеру", "учусь ли я", "играю ли я"}:
            return self._answer(self._observation_reply())
        original = re.sub(r"^(?:джарвис|джарсис|джарвиз|jarvis)[,\s]+", "", text.strip(), flags=re.IGNORECASE)
        original = re.sub(r"^пожалуйста[,\s]+", "", original, flags=re.IGNORECASE)
        confirmed = re.fullmatch(r"(?:подтверждаю|подтверди|confirm|/confirm) ([a-f0-9]{32})", value)
        if confirmed:
            return self.confirm(confirmed[1], source=source)
        canceled = re.fullmatch(r"(?:отмена|отмени|cancel|/cancel) ([a-f0-9]{32})", value)
        if canceled:
            return self.cancel(canceled[1])
        intent_phrase = _intent_phrase(text)
        if self.request_kind(text, source) == "stop":
            return self._answer(Reply("Остановлено.", speak=False))
        if intent_phrase in {"доброе утро", "good morning"}:
            token = secrets.token_hex(16)
            with self._morning_lock:
                self._morning_pending[token] = time.monotonic() + 60
            return self._answer(Reply("Доброе утро, сэр.", media_cue="welcome_home", followup_id=token))
        # This is an explicit playback cue, never a system-readiness claim or
        # an inferred PC action. The user's recording stays outside the repo.
        if _WELCOME_HOME.fullmatch(intent_phrase):
            return self._answer(Reply("Добро пожаловать домой, сэр.", media_cue="welcome_home"))
        if intent_phrase in {"заметки обсидиан", "заметки obsidian"}:
            return self._answer(Reply(self._vault_notes_reply()))
        vault_open = re.fullmatch(r"(?:(?:открой|покажи|open)\s+)?(?:обсидиан|obsidian)(?:\s+заметки)?", intent_phrase)
        if vault_open:
            result = self.obsidian.open_vault(authorization_guard=self._catalog_authorized)
            return self._answer(Reply(result["message"]))
        vault_note = re.fullmatch(r"(?:запиши|сохрани|создай|save)(?:\s+заметку)?\s+(?:в\s+)?(?:обсидиан|obsidian)\s+(.+)", original, re.I | re.S)
        if vault_note:
            try:
                result = self.obsidian.create_note("Быстрая заметка", vault_note[1], authorization_guard=self._catalog_authorized)
                return self._answer(Reply(result["message"]))
            except (ValueError, OSError):
                return self._answer(Reply("Не удалось сохранить заметку в Obsidian. Проверь выбранное хранилище и доступ владельца."))
        vault_search = re.fullmatch(r"найди (?:в\s+)?(?:обсидиан|obsidian)\s+(.+)", original, re.I | re.S)
        if vault_search:
            try:
                return self._answer(Reply(self._vault_notes_reply(vault_search[1])))
            except (ValueError, OSError):
                return self._answer(Reply("Поиск в Obsidian недоступен. Проверь выбранное хранилище."))
        information = _desktop_information(intent_phrase)
        if information is not None:
            return self._answer(Reply(information, speech_text=HELP_SPOKEN if information == HELP else None))
        if intent_phrase in {"проекты", "мои проекты", "projects"}:
            items = self.projects.projects()
            return self._answer(Reply("\n".join(item["name"] for item in items[:30]) if items else "Добавь проект или разрешённую папку в настройках Jarvis."))
        if intent_phrase in {"приложения", "мои приложения", "apps"}:
            items = self.apps.apps()
            return self._answer(Reply("\n".join(item["name"] for item in items[:30]) if items else "Добавь свои приложения в настройках. Браузер, Chrome, Яндекс Музыка и системные утилиты уже поддерживаются."))
        from desktop.computer_controls import parse_command
        window_command = parse_command(text)
        if window_command is not None and self.computer is not None:
            result = self.computer.request(*window_command, authorization_guard=self._catalog_authorized)
            return self._answer(Reply(result.text, confirmation_id=result.confirmation_id))
        catalog_frame = _catalog_request_frame(text)
        project = re.fullmatch(r"(?:открой|открыть|запусти|запустить|open|launch)\s+(?:проект|project)\s+(.+)", catalog_frame)
        if project:
            try:
                result = self.projects.open_project(project[1], authorization_guard=self._catalog_authorized)
                return self._answer(Reply(result["message"]))
            except ValueError:
                return self._answer(Reply("Укажи одно имя проекта из каталога, без пути, ссылки или дополнительных команд."))
        application = re.fullmatch(r"(?:открой|открыть|запусти|запустить|включи|open|launch|start)\s+(?:(приложение|app)\s+)?(.+)", catalog_frame)
        if application:
            try:
                found = self.apps.resolve(application[2])
                if found["status"] != "missing" or application[1]:
                    return self._answer(Reply(self.apps.open_app(application[2], authorization_guard=self._catalog_authorized)["message"]))
            except ValueError:
                return self._answer(Reply("Укажи одно имя приложения из каталога, без пути, ссылки или дополнительных команд."))
        action = _desktop_action(intent_phrase)
        if action:
            return self._request_action(action)
        if intent_phrase in {"джарвис", "jarvis", "да", "привет", "как дела", "как твои дела", "как у тебя дела",
                             "ты здесь", "ты тут", "how are you", "hello", "are you there"}:
            return self._answer(Reply("На связи, сэр. Скажи команду или выбери следующий шаг."))
        if intent_phrase in {"ты меня слышишь", "слышишь меня", "меня слышно", "can you hear me", "do you hear me"}:
            return self._answer(Reply("Твой голосовой запрос распознан, сэр. Слушаю команду." if source == "voice"
                                      else "Сообщение получил, сэр. Я на связи."))
        if value in {"помощь", "команды", "что ты умеешь", "help", "/help"}:
            return self._answer(Reply(HELP, speech_text=HELP_SPOKEN))
        if value in {"статус", "статус компьютера", "status", "/pcstatus"}:
            return self._answer(Reply("Локальный помощник работает. Задачи и заметки доступны без сети. "
                                      "Доступность микрофона и голоса показана в настройках приложения."))
        organized = self.organizer.handle(original)
        if organized is not None:
            return self._answer(Reply(organized))
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
                if self.obsidian.status().get("configured") is True:
                    result = self.obsidian.create_note("Быстрая заметка", note[1], authorization_guard=self._catalog_authorized)
                    return self._answer(Reply(result["message"]))
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
            planned = [item for item in self.organizer.day()["items"] if item["status"] == "unknown"]
            tasks = [task for task in self.snapshot()["tasks"] if not task["done"]]
            if planned:
                text = (f"В твоём сегодняшнем плане: {planned[0]['title']}. "
                        "Выбери небольшой первый шаг или перенеси дело, если планы изменились. Результат отметь сам.")
            elif tasks:
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
            parts = [_note_previews(notes)] if notes else []
            if self.obsidian.status().get("configured") is True:
                parts.append(self._vault_notes_reply(search[1]))
            text = "\n\n".join(parts) if parts else "Подходящих заметок нет."
            return self._answer(Reply(text))
        if value in {"заметки", "мои заметки", "notes", "/notes"}:
            notes = self.snapshot()["notes"]
            parts = [_note_previews(notes)] if notes else []
            if self.obsidian.status().get("configured") is True:
                parts.append(self._vault_notes_reply())
            text = "\n\n".join(parts) if parts else "Заметок пока нет. Скажи «запомни» и свою мысль."
            return self._answer(Reply(text))
        clarification = _desktop_clarification(intent_phrase)
        if clarification is not None:
            return self._answer(Reply(clarification))
        if self.request_kind(text, source) != "chat":
            return self._answer(Reply("Уточни команду: что открыть, сохранить или изменить. Свободный вопрос можно задать отдельно."))
        preferences = self.store.preferences()
        from desktop.openai_mode import OpenAIEnvironment
        cloud_environment = OpenAIEnvironment(self.state_dir, self.store.preferences)
        cloud_chat = cloud_environment.get("JARVIS_CLOUD_CHAT_ENABLED") == "1"
        if preferences["local_chat_enabled"] or cloud_chat:
            from desktop.chat import LocalChat, LocalChatCancelled, LocalChatTruncated, LocalChatUnavailable
            from desktop.cloud_ai import CloudCancelled, CloudUnavailable
            client = None
            usage_model = preferences["local_chat_model"] if not cloud_chat else "cloud-router"
            try:
                # A model's result stays display-only. Optional context is
                # exclusively earlier completed model-chat exchanges, never
                # note/task/PC-command/personal-data records or the current row.
                if cloud_chat:
                    from desktop.ai_routing import ChatRouter
                    client = ChatRouter(self.state_dir, local_enabled=preferences["local_chat_enabled"],
                                        local_model=preferences["local_chat_model"], environ=cloud_environment)
                else:
                    client = LocalChat(preferences["local_chat_model"])
                options = {"on_chunk": on_chunk} if on_chunk is not None else {}
                cancel_event = getattr(self._request_context, "cancel_event", None)
                if cancel_event is not None:
                    options["cancel_event"] = cancel_event
                history = _dialogue_history(self.store.model_chat_history(before_id=request_id)) if preferences["local_chat_history"] else None
                if preferences["local_chat_plan_context"] and _needs_plan_context(text, history):
                    options["plan_context"] = self._current_plan_context(preferences)
                if history is not None:
                    answer = client.answer(text, history=history, **options)
                else:
                    answer = client.answer(text, **options)
            except (LocalChatCancelled, CloudCancelled):
                if client is not None:
                    self._record_chat_usage(usage_model, client, success=False)
                return Reply("Запрос отменён.", speak=False)
            except ValueError:
                return self._answer(Reply("Сократи вопрос до 8000 символов и проверь выбранную модель в настройках."))
            except LocalChatTruncated:
                self._record_chat_usage(usage_model, client, success=False)
                return self._answer(Reply("Ответ достиг лимита длины и не был озвучен. Разбей вопрос на части; готовые команды и план продолжают работать."))
            except LocalChatUnavailable:
                if client is not None:
                    self._record_chat_usage(usage_model, client, success=False)
                return self._answer(Reply("Локальная модель сейчас недоступна. Проверь, что Ollama запущена и выбранная локальная модель установлена. Команды, задачи и заметки продолжают работать."))
            except CloudUnavailable as error:
                if client is not None:
                    self._record_chat_usage(usage_model, client, success=False)
                reasons = {
                    "credit_balance_exhausted": "В OpenAI закончились средства API. Облачный голос и диалог пока недоступны.",
                    "insufficient_quota": "OpenAI ограничил квоту API. Проверь средства и лимиты аккаунта.",
                    "organization_spend_limit_exceeded": "Достигнут лимит расходов организации OpenAI.",
                    "project_spend_limit_exceeded": "Достигнут лимит расходов проекта OpenAI.",
                    "organization_usage_limit_exceeded": "Достигнут разрешённый OpenAI лимит использования API.",
                    "rate_limit_exceeded": "OpenAI временно ограничил скорость запросов. Повтори позже.",
                    "slow_down": "OpenAI просит снизить скорость запросов. Повтори позже.",
                    "budget_exceeded": "Достигнут дневной лимит Jarvis для OpenAI.",
                }
                message = reasons.get(error.reason, "Облачный помощник сейчас недоступен или достигнут локальный лимит запросов. Проверь подключение и настройки.")
                return self._answer(Reply(message + " Команды, задачи и заметки продолжают работать."))
            finally:
                if cloud_chat and client is not None:
                    client.close()
            model = getattr(client, "last_model", None) or usage_model
            self._record_chat_usage(model, client, success=True)
            prefix = "OpenAI: " if getattr(client, "last_provider", None) == "openai" else "Локальная модель: "
            return self._answer(Reply(prefix + answer))
        return self._answer(Reply("Не распознал точную команду. " + HELP))

    def confirm(self, token: str, source: str = "button") -> Reply:
        if self._closed.is_set():
            return Reply("Jarvis завершает работу. Повтори команду после запуска.", speak=False)
        if self.owner.enabled and not self.owner.allowed():
            return Reply("Подтверди владельца через Windows Hello перед выполнением.")
        if not isinstance(source, str) or source not in {"button", "typed"}:
            return self._answer(Reply("Для этого действия требуется кнопка подтверждения или исходная текстовая команда. Голос не подтверждает управление питанием."))
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            return self._answer(Reply("Неверное подтверждение. Запроси действие заново."))
        if self.computer is not None:
            result = self.computer.confirm(token, authorization_guard=self._catalog_authorized)
            if result is not None:
                return self._answer(Reply(result.text))
        action = self.store.claim_confirmation(token, time.time())
        if action not in RISKY_ACTIONS:
            return self._answer(Reply("Подтверждение истекло, отменено или уже использовано. Запроси действие заново."))
        return self._execute(action)

    def cancel(self, token: str) -> Reply:
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            return self._answer(Reply("Неверный код отмены."))
        if self.computer is not None:
            result = self.computer.cancel(token)
            if result is not None:
                return self._answer(Reply(result.text))
        if self.store.cancel_confirmation(token):
            return self._answer(Reply("Запрос отменен до выполнения."))
        return self._answer(Reply("Этот запрос уже использован или отменен. Для запланированного выключения: «отмени выключение»."))
