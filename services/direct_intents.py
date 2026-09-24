from __future__ import annotations

from datetime import datetime, timedelta

from config import get_settings
from services.assignments import format_deadlines, list_upcoming_assignments
from services.schedule import DAY_FOCUS, DAY_NAMES_RU, today_schedule, week_schedule

settings = get_settings()

_PLANNING_WORDS = {
    "распредели",
    "распределить",
    "спланируй",
    "спланировать",
    "составь план",
    "когда лучше делать",
    "как успеть",
    "приоритет",
    "приоритиз",
}

_SCHEDULE_WORDS = {
    "расписан",
    "что сейчас",
    "что у меня сейчас",
    "что дальше",
    "следующая пара",
    "следующий блок",
    "что завтра",
    "какие пары",
    "какая пара",
}

_DEADLINE_WORDS = {
    "дедлайн",
    "дедлайны",
    "что сдавать",
    "какие задания",
    "что по дз",
    "что по домаш",
    "lms",
    "лмс",
}

_SLEEP_WORDS = {
    "во сколько прос",
    "когда прос",
    "иду спать",
    "ложусь спать",
    "поспать хорошо",
    "цикл сна",
    "циклы сна",
}


def _simple_query(text: str) -> bool:
    lowered = text.lower().strip()
    if len(lowered) > 260:
        return False
    return not any(word in lowered for word in _PLANNING_WORDS)


def _format_day(day: dict, date_text: str | None = None) -> str:
    header = day.get("name") or DAY_NAMES_RU.get(day.get("weekday"), "День")
    if date_text:
        header = f"{date_text} · {header}"
    lines = [f"📅 {header} · {day.get('focus') or DAY_FOCUS.get(day.get('weekday'), '')}"]
    blocks = day.get("blocks") or []
    if not blocks:
        lines.append("Блоков нет.")
    else:
        for block in blocks:
            icon = "🔒" if block.get("block_type") == "fixed" else "·"
            lines.append(f"{icon} {block['start']}–{block['end']}  {block['title']}")
    return "\n".join(lines)


def _minutes(clock: str) -> int:
    hour, minute = [int(part) for part in clock.split(":", 1)]
    return hour * 60 + minute


def _clock_text(total_minutes: int) -> str:
    total_minutes %= 24 * 60
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


async def _sleep_answer(user_id: int, local: datetime) -> str:
    week = await week_schedule(user_id)
    target_date = local.date() + timedelta(days=1)
    weekday = target_date.weekday()
    day = next((item for item in week["days"] if item["weekday"] == weekday), None)
    blocks = (day or {}).get("blocks") or []

    fixed = [
        block for block in blocks
        if block.get("block_type") == "fixed"
    ]
    first_fixed = min(fixed, key=lambda item: item["start"]) if fixed else None

    # Prefer a commute block that ends shortly before the first fixed event.
    commute = None
    if first_fixed:
        fixed_start = _minutes(first_fixed["start"])
        candidates = [
            block for block in blocks
            if block.get("category") == "commute"
            and _minutes(block["end"]) <= fixed_start
        ]
        if candidates:
            commute = max(candidates, key=lambda item: _minutes(item["end"]))

    if first_fixed:
        anchor = _minutes(commute["start"]) if commute else _minutes(first_fixed["start"]) - 45
        latest_wake = max(0, anchor - 60)
        recommended_wake = max(0, latest_wake - 30)
        event_text = f"{first_fixed['start']} — {first_fixed['title']}"
    else:
        # No hard morning constraint: recommend a stable wake window, not an arbitrary cycle target.
        recommended_wake = 8 * 60 + 30
        latest_wake = 9 * 60
        event_text = "жёстких утренних событий нет"

    now_minutes = local.hour * 60 + local.minute
    sleep_if_recommended = (recommended_wake + 24 * 60 - now_minutes) % (24 * 60)
    sleep_hours = sleep_if_recommended / 60

    if sleep_hours < 6:
        note = "До этого времени сна мало; лучше лечь сразу и сократить необязательные утренние дела."
    elif sleep_hours < 7.5:
        note = "Сна получится меньше оптимального диапазона; лучше не затягивать отход ко сну."
    else:
        note = "Так получится нормальный запас на сон и спокойное утро."

    return (
        "🌙 На завтра:\n"
        f"Первое обязательное событие: {event_text}.\n"
        f"Рекомендованный подъём: около {_clock_text(recommended_wake)} "
        f"(крайний комфортный — {_clock_text(latest_wake)}).\n"
        f"{note}\n"
        "На 90-минутные «циклы» лучше не ориентироваться как на точную формулу — важнее общая длительность сна."
    )


async def try_direct_answer(user_id: int, text: str, now: datetime | None = None) -> str | None:
    if not text or not _simple_query(text):
        return None

    lowered = text.lower().strip()
    local = now or datetime.now(settings.timezone)
    if local.tzinfo is not None:
        local = local.astimezone(settings.timezone)

    if any(word in lowered for word in _SLEEP_WORDS):
        return await _sleep_answer(user_id, local)

    if any(word in lowered for word in _DEADLINE_WORDS):
        items = await list_upcoming_assignments(
            user_id, now=local, days=30, include_unknown=True
        )
        return format_deadlines(items)

    if not any(word in lowered for word in _SCHEDULE_WORDS):
        return None

    if "завтра" in lowered:
        week = await week_schedule(user_id)
        target_date = local.date() + timedelta(days=1)
        weekday = target_date.weekday()
        day = next((item for item in week["days"] if item["weekday"] == weekday), None)
        if day is None:
            return "На завтра расписание не найдено."
        return _format_day(day, target_date.strftime("%d.%m.%Y"))

    today = await today_schedule(user_id, local)
    blocks = today["blocks"]

    if any(
        phrase in lowered
        for phrase in ("что сейчас", "сейчас по расписанию", "что у меня сейчас")
    ):
        current = next((item for item in blocks if item.get("status") == "current"), None)
        upcoming = next((item for item in blocks if item.get("status") == "upcoming"), None)
        lines = [f"📍 Сейчас · {today['weekday_name']}"]
        if current:
            lines.append(f"▶ {current['start']}–{current['end']}  {current['title']}")
        else:
            lines.append("Сейчас активного блока нет.")
        if upcoming:
            lines.append(f"Дальше: {upcoming['start']}–{upcoming['end']}  {upcoming['title']}")
        return "\n".join(lines)

    if any(
        phrase in lowered
        for phrase in ("следующая пара", "какая пара", "следующий блок", "что дальше")
    ):
        upcoming = [
            item for item in blocks if item.get("status") in {"current", "upcoming"}
        ]
        if "пара" in lowered:
            upcoming = [
                item
                for item in upcoming
                if item.get("block_type") == "fixed" or item.get("category") == "study"
            ]
        if not upcoming:
            return "На сегодня следующих подходящих блоков нет."
        first = upcoming[0]
        marker = "Сейчас" if first.get("status") == "current" else "Следующий"
        return f"{marker}: {first['start']}–{first['end']}  {first['title']}"

    return _format_day(
        {
            "weekday": today["weekday"],
            "name": today["weekday_name"],
            "focus": today["focus"],
            "blocks": today["blocks"],
        },
        today["date"],
    )
