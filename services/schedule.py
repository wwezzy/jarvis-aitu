from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time

from config import get_settings

settings = get_settings()


@dataclass(frozen=True, slots=True)
class ScheduleBlock:
    start: str
    end: str
    title: str
    category: str


DAY_A: tuple[ScheduleBlock, ...] = (
    ScheduleBlock("07:30", "08:30", "ЛФК и разгон ЦНС", "recovery"),
    ScheduleBlock("08:30", "11:30", "Deep Work — Jarvis / backend", "deep_work"),
    ScheduleBlock("11:30", "13:00", "Блог и монтаж", "content"),
    ScheduleBlock("13:00", "14:30", "Белково-углеводная загрузка", "nutrition"),
    ScheduleBlock("14:30", "17:00", "Зал 1Fit — силовой протокол", "training"),
    ScheduleBlock("17:00", "19:30", "Recovery / чтение / фоновый код", "recovery"),
    ScheduleBlock("19:30", "21:00", "Прогулка", "recovery"),
    ScheduleBlock("21:00", "22:30", "Физо для военной кафедры", "training"),
    ScheduleBlock("22:30", "23:00", "Вечерняя рефлексия", "reflection"),
    ScheduleBlock("23:30", "23:59", "Full Sleep Mode", "sleep"),
)

DAY_B: tuple[ScheduleBlock, ...] = (
    ScheduleBlock("07:30", "08:30", "ЛФК и декомпрессия спины", "recovery"),
    ScheduleBlock("08:30", "11:30", "Deep Work — C++ / Python / LeetCode / DB", "deep_work"),
    ScheduleBlock("11:30", "13:00", "Блог и контент Lock-in", "content"),
    ScheduleBlock("13:00", "14:30", "Обед и сброс фокуса", "nutrition"),
    ScheduleBlock("14:30", "16:30", "Бассейн 1Fit", "training"),
    ScheduleBlock("16:30", "19:30", "AITU / лабы", "study"),
    ScheduleBlock("19:30", "21:00", "Кросс / зона 2", "training"),
    ScheduleBlock("21:00", "22:30", "Душ / детокс / ужин", "recovery"),
    ScheduleBlock("22:30", "23:00", "Вечерняя рефлексия", "reflection"),
    ScheduleBlock("23:30", "23:59", "Full Sleep Mode", "sleep"),
)

FLEX_DAY: tuple[ScheduleBlock, ...] = (
    ScheduleBlock("09:00", "10:00", "ЛФК / мобильность / спокойный старт", "recovery"),
    ScheduleBlock("10:00", "13:00", "Flexible Deep Work / закрытие хвостов", "deep_work"),
    ScheduleBlock("13:00", "14:00", "Обед", "nutrition"),
    ScheduleBlock("14:00", "19:00", "Свободный блок: учёба / восстановление / личные задачи", "flex"),
    ScheduleBlock("22:30", "23:00", "Вечерняя рефлексия", "reflection"),
    ScheduleBlock("23:30", "23:59", "Full Sleep Mode", "sleep"),
)


def schedule_kind_for_weekday(weekday: int) -> str:
    if weekday in {0, 2}:  # Mon/Wed
        return "B"
    if weekday in {1, 3}:  # Tue/Thu
        return "A"
    return "FLEX"


def blocks_for_weekday(weekday: int) -> tuple[ScheduleBlock, ...]:
    kind = schedule_kind_for_weekday(weekday)
    if kind == "A":
        return DAY_A
    if kind == "B":
        return DAY_B
    return FLEX_DAY


def _clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def today_schedule(now: datetime | None = None) -> dict:
    local = now or datetime.now(settings.timezone)
    if local.tzinfo is None:
        local = local.replace(tzinfo=settings.timezone)
    else:
        local = local.astimezone(settings.timezone)

    current_time = local.time().replace(tzinfo=None)
    blocks = []
    for block in blocks_for_weekday(local.weekday()):
        payload = asdict(block)
        start_t = _clock(block.start)
        end_t = _clock(block.end)
        if start_t <= current_time < end_t:
            status = "current"
        elif current_time < start_t:
            status = "upcoming"
        else:
            status = "done"
        payload["status"] = status
        blocks.append(payload)

    return {
        "date": local.date().isoformat(),
        "weekday": local.strftime("%A"),
        "kind": schedule_kind_for_weekday(local.weekday()),
        "blocks": blocks,
    }
