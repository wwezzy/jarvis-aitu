"""Explicit deterministic commands and conservative schedule edits."""
import re
from datetime import date, datetime, timedelta

from database import engine
from database.models import Reminder
from database.time import aware
from services.assignments import format_deadlines, lms_status
from services.gtg import gtg_stats
from services.planner import make_plan, sleep_plan
from services.schedule import list_schedule_entries, resolve_day, save_override, upsert_schedule_entry
from services.tasks import list_tasks, parse_task_command, save_task


def format_plan(plan):
    lines = ["План от текущего времени · предложенные блоки"]
    for block in plan["blocks"][:30]:
        start, end = datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])
        lines.append(f"{start:%d.%m %H:%M}–{end:%H:%M} · #{block['task_id']} {block['title']}")
    for item in plan["unscheduled"]:
        reason = "уточни оценку времени" if item["reason"] == "effort unknown" else f"не помещается: осталось {item['remaining_minutes']} мин"
        lines.append(f"#{item['task_id']} {item['title']}: {reason}")
    if not plan["blocks"] and not plan["unscheduled"]:
        lines.append("Активных задач нет.")
    if plan["overloaded"]:
        lines.append("Перегрузка: сократи объём или согласуй перенос срока; сон и отдых сохранены.")
    return "\n".join(lines)


async def apply_schedule_text(user_id, text, now):
    """Only an unambiguous existing occurrence can be changed by natural text."""
    lowered = text.lower()
    if "завтра" not in lowered:
        return None
    cancel = re.search(r"отмени\s+зал\s+(?:только\s+)?завтра", lowered)
    move = re.search(r"(?:завтра\s+зал|зал\s+завтра)(?:\s+не\s+в\s+\d{1,2}(?::\d{2})?,?\s+а)?\s+в\s+(\d{1,2})(?::(\d{2}))?", lowered)
    first = re.search(r"завтра\s+первая\s+пара\s+в\s+(\d{1,2})(?::(\d{2}))?", lowered)
    if not (cancel or move or first):
        return None
    target = aware(now).date() + timedelta(days=1)
    blocks = await resolve_day(user_id, target)
    candidates = ([b for b in blocks if b.get("block_type") == "fixed" and b.get("category") == "study"]
                  if first else [b for b in blocks if b.get("category") == "training" and b.get("block_type") != "flex"])
    if first:
        candidates = sorted(candidates, key=lambda b: b["start"])[:1]
    if len(candidates) != 1 or not isinstance(candidates[0]["id"], int):
        return "Нужен точный блок для изменения: открой Week и выбери дату. Ничего не изменено."
    block = candidates[0]
    changes = {}
    if not cancel:
        match = first or move
        hour, minute = int(match[1]), int(match[2] or 0)
        start = datetime.combine(target, datetime.min.time()).replace(hour=hour, minute=minute)
        duration = datetime.strptime(block["end"], "%H:%M") - datetime.strptime(block["start"], "%H:%M")
        end = start + duration
        if end.date() != target:
            raise ValueError("Block must end on the same day")
        changes = {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
    await save_override(user_id, target, block["id"], changes, cancelled=bool(cancel))
    return f"Сохранено только на {target}: {block['title']} · " + ("отменено" if cancel else changes["start"])


async def route_command(user_id, text, now):
    raw = text.strip()
    lowered = raw.lower()
    command, _, body = raw.partition(" ")
    command = command.split("@")[0].lower()
    if command == "/task":
        row = await save_task(user_id, parse_task_command(raw))
        return f"Сохранено задание #{row['id']}: {row['title']}"
    if command in {"/tasks", "/assignments"}:
        return format_deadlines(await list_tasks(user_id, status="pending"), title="Задачи")
    if command == "/lms_status":
        state = await lms_status(user_id)
        return f"LMS: {'подключён' if state['configured'] else 'не подключён'}\nПоследняя успешная синхронизация: {state['last_success_at'] or 'нет'}"
    if command in {"/plan", "/risk"} or lowered in {"что делать сейчас", "что я забываю", "сегодня ничего не сделал, перепланируй", "план на вечер"} or re.fullmatch(r"(?:у меня )?\d{2,3}\s+минут(?: свободно)?", lowered):
        window = re.search(r"\b(\d{2,3})\b", body or lowered)
        plan = await make_plan(user_id, now, minutes=int(window[1]) if window else None)
        if command == "/risk":
            tasks = {t["id"]: t for t in await list_tasks(user_id, status="pending")}
            return "\n".join(f"#{key} {tasks[key]['title']} · risk {risk['score']}/100\n" + ", ".join(f"{k}={v}" for k, v in risk['components'].items()) + "\n" + ", ".join(risk['uncertainties']) for key, risk in plan["risks"].items()) or "Активных задач нет."
        return format_plan(plan)
    if command == "/sleep":
        plan = await sleep_plan(user_id, now)
        return f"Сон: цель {plan['target_hours']} ч\nЛечь к {plan['bedtime']}\nПодъём {plan['wake_at']}\nЕсли лечь сейчас: {plan['available_hours']} ч."
    if command == "/stats":
        stats = await gtg_stats(user_id, now)
        tasks = await list_tasks(user_id)
        return f"GTG за неделю: {stats['week']['reps']} повторов\nЗадачи: {sum(t['status'] == 'pending' for t in tasks)} активных, {sum(t['status'] == 'done' for t in tasks)} завершённых."
    if command == "/remind":
        stamp, separator, content = body.partition("|")
        if not separator or not content.strip():
            raise ValueError("/remind 2030-01-08 15:00 | Текст напоминания")
        when = aware(datetime.fromisoformat(stamp.strip()))
        if when <= aware(now):
            raise ValueError("Напоминание должно быть в будущем")
        async with engine.async_session_factory() as db:
            row = Reminder(user_id=user_id, text=content.strip()[:255], remind_at=when)
            db.add(row)
            await db.commit()
            return f"Напоминание #{row.id} сохранено на {when.isoformat()}"
    if command == "/schedule":
        # Complete deterministic editing path; JSON is unnecessary in Telegram.
        parts = [p.strip() for p in body.split("|")]
        if len(parts) != 5:
            rows = await list_schedule_entries(user_id)
            return "Формат: /schedule 0 | 14:00 | 15:00 | WEB | fixed\n" + "\n".join(f"#{r.id} день {r.weekday} {r.start}–{r.end} {r.title}" for r in rows)
        day, start, end, title, block_type = parts
        saved = await upsert_schedule_entry(user_id, dict(weekday=int(day), start=start, end=end, title=title,
            category="study", block_type=block_type, notify_before_min=60 if block_type == "fixed" else None))
        return f"Недельный блок #{saved['id']} сохранён."
    if command == "/override":
        # /override DATE | ID | cancel OR start | end
        parts = [p.strip() for p in body.split("|")]
        if len(parts) not in {3, 4}:
            raise ValueError("/override 2030-01-08 | 12 | cancel (или 11:00 | 12:30)")
        target, entry = date.fromisoformat(parts[0]), int(parts[1])
        cancel = parts[2] == "cancel"
        if not cancel and len(parts) != 4:
            raise ValueError("Нужно время начала и конца")
        await save_override(user_id, target, entry, {} if cancel else {"start": parts[2], "end": parts[3]}, cancelled=cancel)
        return f"Изменение #{entry} сохранено только на {target}."
    if len(raw) <= 200:
        return await apply_schedule_text(user_id, raw, now)
    return None
