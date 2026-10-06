"""Deterministic initiative, explicit evidence, and bounded attention costs.

The loop proposes and checks in. It cannot invoke PC, Calendar or model tools.
"""
import json
from datetime import datetime, time, timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from config import get_settings
from database import engine
from database.assistant_models import AssistantState, FocusSession, Goal, GoalCheckIn, ActivityReceipt
from database.models import Assignment
from database.time import aware
from database.v4_models import NotificationDelivery
from services.preferences import get_preferences, save_preferences

STARTERS = (
    dict(template_key="movement", title="Вернуть регулярное движение", category="movement",
         next_step="Подготовить одежду и начать с комфортного движения", minimum_minutes=5,
         target_per_week=3, priority=6, cue="После свободного окна, до развлечений"),
    dict(template_key="learning", title="Развитие навыка", category="learning",
         next_step="Открыть один учебный материал и разобрать один вопрос", minimum_minutes=15,
         target_per_week=5, priority=7, cue="Перед первой игровой сессией"),
    dict(template_key="organization", title="Разобрать накопленное", category="organization",
         next_step="Разобрать одну запись: действие, справка или архив", minimum_minutes=5,
         target_per_week=3, priority=5, cue="После завершения работы"),
)


class GoalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=180)
    next_step: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=500)
    category: str = Field(default="learning", pattern=r"^(movement|learning|organization|other)$")
    minimum_minutes: int = Field(default=10, ge=2, le=120)
    target_per_week: int = Field(default=3, ge=1, le=7)
    priority: int = Field(default=5, ge=1, le=10)
    cue: str = Field(default="После первого свободного окна", min_length=1, max_length=180)
    enabled: bool = True


async def ensure_state(user_id):
    async with engine.async_session_factory() as db:
        if await db.get(AssistantState, user_id) is None:
            db.add(AssistantState(user_id=user_id))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()


async def ensure_starter_goals(user_id):
    """Editable starter proposals, not inferred personal facts or prescriptions."""
    await ensure_state(user_id)
    async with engine.async_session_factory() as db:
        for template in STARTERS:
            if await db.scalar(select(Goal.id).where(Goal.user_id == user_id, Goal.template_key == template["template_key"])) is None:
                # Archive/disable preserves the key so a removed goal is never resurrected.
                db.add(Goal(user_id=user_id, reason="Редактируемый стартовый ориентир", **template))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()


def goal_dict(row):
    return {key: getattr(row, key) for key in GoalInput.model_fields} | {"id": row.id}


async def save_goal(user_id, payload, identity=None):
    async with engine.async_session_factory() as db:
        row = await db.get(Goal, identity) if identity else None
        if identity and (row is None or row.user_id != user_id):
            raise ValueError("Goal not found")
        value = GoalInput.model_validate({**({k: getattr(row, k) for k in GoalInput.model_fields} if row else {}), **payload})
        if not value.title.strip() or not value.next_step.strip():
            raise ValueError("Empty goal")
        if row is None:
            if await db.scalar(select(Goal.id).where(Goal.user_id == user_id).offset(49).limit(1)):
                raise ValueError("Archive an old goal first; maximum 50")
            row = Goal(user_id=user_id)
            db.add(row)
        for key, val in value.model_dump().items():
            setattr(row, key, val.strip() if isinstance(val, str) else val)
        await db.commit()
        return goal_dict(row)


async def goals_snapshot(user_id, now):
    now = aware(now)
    start = now.date() - timedelta(days=now.weekday())
    async with engine.async_session_factory() as db:
        goals = list(await db.scalars(select(Goal).where(Goal.user_id == user_id, Goal.enabled.is_(True)).order_by(Goal.priority.desc(), Goal.id)))
        logs = list(await db.scalars(select(GoalCheckIn).where(GoalCheckIn.user_id == user_id,
            GoalCheckIn.on_date >= start, GoalCheckIn.on_date <= now.date())))
    result = []
    for goal in goals:
        completed = [row for row in logs if row.goal_id == goal.id and row.outcome == "done"]
        today = next((row for row in logs if row.goal_id == goal.id and row.on_date == now.date()), None)
        result.append(goal_dict(goal) | {"week_completed": len(completed), "week_minutes": sum(r.minutes for r in completed),
            "today": today.outcome if today else "unknown", "obstacle": today.obstacle if today else "",
            "remaining": max(0, goal.target_per_week - len(completed)), "week_start": start.isoformat()})
    return result


async def check_in(user_id, goal_id, outcome, minutes=0, obstacle="", now=None):
    now = aware(now or datetime.now(get_settings().timezone))
    if outcome not in {"done", "skipped"} or type(minutes) is not int or not 0 <= minutes <= 1440 or len(obstacle) > 500:
        raise ValueError("Invalid check-in")
    async with engine.async_session_factory() as db:
        goal = await db.get(Goal, goal_id)
        if not goal or goal.user_id != user_id or not goal.enabled:
            raise ValueError("Goal not found")
        if outcome == "done" and minutes < goal.minimum_minutes:
            raise ValueError(f"Для этой цели минимум {goal.minimum_minutes} минут. Можно уменьшить цель в редакторе.")
        row = await db.scalar(select(GoalCheckIn).where(GoalCheckIn.user_id == user_id,
            GoalCheckIn.goal_id == goal_id, GoalCheckIn.on_date == now.date()))
        if row is None:
            row = GoalCheckIn(user_id=user_id, goal_id=goal_id, on_date=now.date())
            db.add(row)
        row.outcome, row.minutes, row.obstacle = outcome, minutes, obstacle.strip()
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            # A duplicated tap never adds a second completion.
        await db.execute(update(AssistantState).where(AssistantState.user_id == user_id).values(last_interaction_at=now))
        await db.commit()
    return "Отметка сохранена. Один пропуск не обнуляет сделанное; следующий шаг можно уменьшить."


async def touch(user_id, now=None):
    await ensure_state(user_id)
    async with engine.async_session_factory() as db:
        await db.execute(update(AssistantState).where(AssistantState.user_id == user_id).values(
            last_interaction_at=aware(now or datetime.now(get_settings().timezone))))
        await db.commit()


async def active_focus(user_id):
    async with engine.async_session_factory() as db:
        state = await db.get(AssistantState, user_id)
        row = await db.get(FocusSession, state.active_focus_id) if state and state.active_focus_id else None
        return {"id": row.id, "title": row.title, "goal_id": row.goal_id, "task_id": row.task_id,
            "start": row.started_at.isoformat(), "end": row.ends_at.isoformat(), "status": row.status} if row else None


async def focus_running(user_id, now):
    focus = await active_focus(user_id)
    return bool(focus and aware(datetime.fromisoformat(focus["end"])) > aware(now))


async def start_focus(user_id, minutes=25, title="Один следующий шаг", goal_id=None, task_id=None, now=None):
    from services.schedule import resolve_day
    now = aware(now or datetime.now(get_settings().timezone))
    if type(minutes) is not int or not 2 <= minutes <= 120 or not title.strip() or len(title) > 255 or (goal_id and task_id):
        raise ValueError("Focus: 2..120 минут, одна цель или задача")
    finish = now + timedelta(minutes=minutes)
    prefs = await get_preferences(user_id)
    if (finish.date() != now.date() or now.strftime("%H:%M") < prefs.day_start
            or finish.strftime("%H:%M") > prefs.day_end):
        raise ValueError("Фокус должен помещаться в твой рабочий день. Сейчас сохрани отдых.")
    blocks = await resolve_day(user_id, now.date())
    protected = {"sleep", "training", "rest", "recovery", "nutrition", "routine", "commute", "chores"}
    for block in blocks:
        if block["block_type"] != "fixed" and block["category"] not in protected:
            continue
        left = aware(datetime.combine(now.date(), time.fromisoformat(block["start"]))) - timedelta(minutes=(block.get("commute_minutes") or 0) + (block.get("preparation_minutes") or 0))
        right = aware(datetime.combine(now.date(), time.fromisoformat(block["end"]))) + timedelta(minutes=block.get("commute_minutes") or 0)
        if now < right and finish > left:
            raise ValueError(f"Фокус пересекается с {block['title']}. Выбери свободное окно.")
    await ensure_state(user_id)
    async with engine.async_session_factory() as db:
        if goal_id:
            goal = await db.get(Goal, goal_id)
            if not goal or goal.user_id != user_id or not goal.enabled:
                raise ValueError("Goal not found")
            title = goal.next_step[:255]
        if task_id:
            task = await db.get(Assignment, task_id)
            if not task or task.user_id != user_id or task.status != "pending":
                raise ValueError("Task not found")
            title = task.title
        row = FocusSession(user_id=user_id, title=title.strip(), goal_id=goal_id, task_id=task_id, started_at=now, ends_at=finish)
        db.add(row)
        await db.flush()
        claim = await db.execute(update(AssistantState).where(AssistantState.user_id == user_id,
            AssistantState.active_focus_id.is_(None)).values(active_focus_id=row.id, last_interaction_at=now))
        if claim.rowcount != 1:
            await db.rollback()
            raise ValueError("Сначала заверши или отмени текущий фокус: /focus stop МИНУТЫ или /focus cancel")
        await db.commit()
    return await active_focus(user_id)


async def finish_focus(user_id, minutes=None, now=None):
    now = aware(now or datetime.now(get_settings().timezone))
    async with engine.async_session_factory() as db:
        state = await db.get(AssistantState, user_id)
        row = await db.get(FocusSession, state.active_focus_id) if state and state.active_focus_id else None
        if row is None:
            raise ValueError("Нет активного фокуса")
        elapsed = max(0, int((now - row.started_at).total_seconds() / 60))
        if minutes is not None and (type(minutes) is not int or not 0 <= minutes <= min(1440, elapsed + 1)):
            raise ValueError("Укажи фактические минуты, не больше прошедшего времени")
        row.status, row.reported_minutes = ("reported", minutes) if minutes is not None else ("cancelled", None)
        goal_id = row.goal_id
        state.active_focus_id, state.last_interaction_at = None, now
        await db.commit()
    # A reported focus duration is evidence of time, never automatic task/goal completion.
    return f"Фокус завершён: {minutes} мин по твоей отметке." + (f" /checkin {goal_id} done {minutes} — если шаг действительно выполнен." if goal_id else " Задача не помечена выполненной.") if minutes is not None else "Фокус отменён. Отчёт о выполнении не создан."


async def activity_summary(user_id, now):
    now = aware(now).astimezone(get_settings().timezone)
    left = aware(datetime.combine(now.date(), time.min))
    totals = {category: 0 for category in ("work", "gaming", "media", "other", "idle", "unknown")}
    async with engine.async_session_factory() as db:
        rows = list(await db.scalars(select(ActivityReceipt).where(ActivityReceipt.user_id == user_id,
            ActivityReceipt.starts_at >= left, ActivityReceipt.starts_at <= now)))
    for row in rows:
        for key in totals:
            totals[key] += row.seconds.get(key, 0)
    return {"date": now.date().isoformat(), "seconds": totals, "coverage_minutes": round(sum(totals.values()) / 60, 1),
            "interpretation": "Foreground process + idle sampling; browser content and actual accomplishment unknown"}


async def next_action(user_id, now=None, low_energy=False):
    from services.planner import make_plan, free_slots
    from services.schedule import resolve_day
    now = aware(now or datetime.now(get_settings().timezone))
    focus = await active_focus(user_id)
    if focus:
        ended = aware(datetime.fromisoformat(focus["end"])) <= now
        return {"kind": "review" if ended else "focus", "text": ("Таймер закончился. Сколько реально сделал? /focus stop МИНУТЫ" if ended else f"Сейчас один шаг: {focus['title']}. Остальные предложения подождут."), "focus": focus}
    prefs = await get_preferences(user_id)
    slots = free_slots(now.date(), await resolve_day(user_id, now.date()), now, prefs)
    if not slots or slots[0][0] > now + timedelta(minutes=10):
        return {"kind": "rest", "text": "Сейчас сохрани обязательный блок или отдых. Следующий шаг предложу в свободное окно."}
    plan = await make_plan(user_id, now, minutes=120)
    candidate = plan["next_action"]
    if candidate and candidate["start"] <= (now + timedelta(minutes=10)).isoformat() and plan["risks"][candidate["task_id"]]["score"] >= 45:
        minutes = min(10 if low_energy else 25, candidate["minutes"])
        return {"kind": "task", "task_id": candidate["task_id"], "minutes": minutes,
            "text": f"Следующий шаг: #{candidate['task_id']} {candidate['title']} · {minutes} мин. Ближайший срок/риск по данным задач. /focus {minutes} task {candidate['task_id']}"}
    goals = [g for g in await goals_snapshot(user_id, now) if g["remaining"] > 0 and g["today"] == "unknown"]
    if goals:
        # Smallest weekly deficit fraction + priority; no psychological diagnosis.
        goal = max(goals, key=lambda g: (g["remaining"] / g["target_per_week"], g["priority"]))
        available = int((slots[0][1] - max(slots[0][0], now)).total_seconds() / 60)
        minutes = min(5 if low_energy else goal["minimum_minutes"], available)
        return {"kind": "goal", "goal_id": goal["id"], "minutes": minutes,
            "text": f"Один шаг к «{goal['title']}»: {goal['next_step']} · {minutes} мин.\nЕсли {goal['cue'].lower()}, то начинаю этот шаг.\nЗа неделю: {goal['week_completed']}/{goal['target_per_week']} отмеченных дней. /focus {minutes} goal {goal['id']}"}
    if candidate:
        return {"kind": "task", "task_id": candidate["task_id"], "minutes": min(25, candidate["minutes"]),
            "text": f"Можно продвинуть #{candidate['task_id']} {candidate['title']}. /focus 25 task {candidate['task_id']}"}
    return {"kind": "rest", "text": "На сегодня отмеченные ориентиры выполнены или отложены. Выбери отдых осознанно; новые задачи можно сохранить через /capture."}


async def boredom_help(user_id, now):
    action = await next_action(user_id, now, low_energy=True)
    return "Скука — сигнал выбрать следующий небольшой шаг.\n" + action["text"] + "\nЕщё варианты: 5 минут без экрана; разобрать одну запись /inbox; выбрать ограниченный отдых /autopilot pause 1. Игры с друзьями — твой выбор; сравни его со своей целью, без обвинений."


async def weekly_review(user_id, now):
    goals = await goals_snapshot(user_id, now)
    lines = ["Обзор недели · только явные отметки"]
    for goal in goals:
        lines.append(f"#{goal['id']} {goal['title']}: {goal['week_completed']}/{goal['target_per_week']} дней, {goal['week_minutes']} мин.")
    lines.append("Без отметки результат неизвестен. Выбери одно препятствие и один более простой шаг на следующую неделю. /goal edit ID | НАЗВАНИЕ | ШАГ | МИНУТЫ | ДНЕЙ | СИГНАЛ | КАТЕГОРИЯ")
    return "\n".join(lines)


async def autonomy_relevant(user_id, now):
    prefs = await get_preferences(user_id)
    async with engine.async_session_factory() as db:
        state = await db.get(AssistantState, user_id)
    return prefs.autonomy_mode != "off" and not (state and state.paused_until and state.paused_until > now)


async def assistant_tick(bot, user_id, now=None):
    from services.notifications import is_dnd, notify_once
    now = aware(now or datetime.now(get_settings().timezone))
    await ensure_state(user_id)
    focus = await active_focus(user_id)
    # A forgotten timer does not block initiative forever; outcome stays unknown.
    if focus and aware(datetime.fromisoformat(focus["end"])) < now - timedelta(hours=8):
        async with engine.async_session_factory() as db:
            await db.execute(update(FocusSession).where(FocusSession.user_id == user_id,
                FocusSession.id == focus["id"], FocusSession.status == "active").values(status="unreported"))
            await db.execute(update(AssistantState).where(AssistantState.user_id == user_id,
                AssistantState.active_focus_id == focus["id"]).values(active_focus_id=None))
            await db.commit()
        focus = None
    if focus and aware(datetime.fromisoformat(focus["end"])) <= now:
        await notify_once(bot, user_id, f"focus:{focus['id']}", "Фокус завершён. Сколько реально удалось сделать? /focus stop МИНУТЫ. Таймер сам не засчитывает результат.", now=now, expires_at=now + timedelta(hours=2))
    if not await autonomy_relevant(user_id, now) or focus or await is_dnd(user_id, now):
        return False
    prefs = await get_preferences(user_id)
    hours = {"quiet": (9, 21), "balanced": (9, 14, 21), "active": (9, 11, 14, 17, 21)}[prefs.autonomy_mode]
    if now.hour not in hours or now.minute >= 30:
        return False
    async with engine.async_session_factory() as db:
        state = await db.get(AssistantState, user_id)
        if state.last_interaction_at and now - state.last_interaction_at < timedelta(minutes=45):
            return False
        recent = list(await db.scalars(select(NotificationDelivery).where(NotificationDelivery.user_id == user_id,
            NotificationDelivery.kind == "autonomy", NotificationDelivery.status.in_(("sent", "uncertain", "sending")),
            NotificationDelivery.scheduled_at >= now - timedelta(days=2),
            NotificationDelivery.scheduled_at > (state.last_interaction_at or now - timedelta(days=2)))))
    # No engagement is unknown, not failure; back off to morning/evening after 2 ignored nudges.
    if len(recent) >= 2 and now.hour not in {9, 21}:
        return False
    action = await next_action(user_id, now)
    if action["kind"] == "rest" and now.hour != 21:
        return False
    text = await weekly_review(user_id, now) if now.weekday() == 6 and now.hour == 21 else action["text"]
    if prefs.activity_enabled:
        usage = await activity_summary(user_id, now)
        gaming = usage["seconds"]["gaming"] // 60
        if gaming >= prefs.gaming_budget_minutes and gaming > 0:
            text = f"По наблюдаемому активному окну игры: {gaming} мин; твой ориентир {prefs.gaming_budget_minutes} мин. Это не полный учёт дня.\n" + text
    text += "\n/next — следующий шаг · /bored — сменить занятие · /autopilot pause 1 — час отдыха · /autopilot off — пауза инициативы"
    return await notify_once(bot, user_id, f"autonomy:{now.date()}:{now.hour}", text, now=now, expires_at=now + timedelta(minutes=30))


async def focus_drift_notice(bot, user_id, now=None):
    """At most one factual, non-coercive check-in per focus, and five per day."""
    from services.notifications import notify_once
    now = aware(now or datetime.now(get_settings().timezone))
    prefs = await get_preferences(user_id)
    focus = await active_focus(user_id)
    if not prefs.activity_enabled or prefs.autonomy_mode != "active" or not focus or not await autonomy_relevant(user_id, now):
        return False
    start, end = aware(datetime.fromisoformat(focus["start"])), aware(datetime.fromisoformat(focus["end"]))
    if not start <= now < end:
        return False
    async with engine.async_session_factory() as db:
        receipts = list(await db.scalars(select(ActivityReceipt).where(ActivityReceipt.user_id == user_id,
            ActivityReceipt.starts_at >= start, ActivityReceipt.starts_at <= now)))
        day = aware(datetime.combine(now.date(), time.min))
        claims = list(await db.scalars(select(NotificationDelivery.id).where(NotificationDelivery.user_id == user_id,
            NotificationDelivery.kind == "focuscoach", NotificationDelivery.scheduled_at >= day)))
    if len(claims) >= 5 or sum(r.seconds.get("gaming", 0) for r in receipts) < 120 or not any(
        r.seconds.get("gaming", 0) > 0 and r.starts_at > now - timedelta(minutes=2) for r in receipts):
        return False
    return await notify_once(bot, user_id, f"focuscoach:{focus['id']}",
        f"Во время выбранного фокуса наблюдалось минимум 2 минуты активного окна игры. План: {focus['title']}. Вернуться к шагу или осознанно закончить фокус и отдыхать? Приложения не закрываю.",
        now=now, expires_at=end)


async def assistant_snapshot(user_id, now):
    await ensure_state(user_id)
    prefs = await get_preferences(user_id)
    async with engine.async_session_factory() as db:
        state = await db.get(AssistantState, user_id)
    return {"mode": prefs.autonomy_mode, "paused_until": state.paused_until.isoformat() if state.paused_until else None,
            "next_action": await next_action(user_id, now), "goals": await goals_snapshot(user_id, now),
            "focus": await active_focus(user_id), "activity": await activity_summary(user_id, now)}


async def autonomy_command(user_id, raw, now):
    command, _, body = raw.strip().partition(" ")
    command = command.split("@")[0].lower()
    if command == "/autopilot":
        value = body.strip().lower()
        if value in {"on", "quiet", "balanced", "active", "off"}:
            await save_preferences(user_id, {"autonomy_mode": "active" if value == "on" else value})
            if value != "off":
                await ensure_starter_goals(user_id)
        elif value == "resume" or value.startswith("pause "):
            hours = int(value.split()[1]) if value.startswith("pause ") else 0
            if not 0 <= hours <= 168:
                raise ValueError("Пауза: 1..168 часов")
            await ensure_state(user_id)
            async with engine.async_session_factory() as db:
                await db.execute(update(AssistantState).where(AssistantState.user_id == user_id).values(
                    paused_until=aware(now) + timedelta(hours=hours) if hours else None))
                await db.commit()
        elif value:
            raise ValueError("/autopilot on|active|balanced|quiet|off|pause ЧАСЫ|resume")
        return json.dumps(await assistant_snapshot(user_id, now), ensure_ascii=False, indent=2) if not value else "Режим инициативы обновлён. /next · /goals · /bored. Цели — редактируемые ориентиры; выполнение подтверждаешь ты."
    if command in {"/next", "/bored"} or raw.lower().strip() in {"мне скучно", "скучно", "не могу начать", "опять залип", "хочу поиграть", "что мне делать", "верни меня к цели"}:
        return await boredom_help(user_id, now) if command == "/bored" or "скуч" in raw.lower() or "начать" in raw.lower() else (await next_action(user_id, now))["text"]
    if command == "/goals":
        return await weekly_review(user_id, now)
    if command == "/goal":
        action, _, rest = body.partition(" ")
        parts = [p.strip() for p in rest.split("|")]
        if action == "archive":
            await save_goal(user_id, {"enabled": False}, int(rest))
        elif action in {"add", "edit"}:
            identity = int(parts.pop(0)) if action == "edit" else None
            if len(parts) != 6:
                raise ValueError("/goal add НАЗВАНИЕ | ШАГ | МИНУТЫ | ДНЕЙ В НЕДЕЛЮ | СИГНАЛ | КАТЕГОРИЯ (learning/movement/organization/other)")
            title, step, minutes, target, cue, category = parts
            goal = await save_goal(user_id, dict(title=title, next_step=step, minimum_minutes=int(minutes), target_per_week=int(target), cue=cue, category=category), identity)
            return f"Цель #{goal['id']} сохранена. /next — начать маленьким шагом."
        else:
            raise ValueError("/goal add|edit|archive. /goals — текущие ориентиры")
        return "Цель отключена. История сохранена."
    if command == "/checkin":
        parts = body.split(maxsplit=3)
        if len(parts) < 2:
            raise ValueError("/checkin ID done МИНУТЫ или /checkin ID skipped 0 ПРИЧИНА")
        return await check_in(user_id, int(parts[0]), parts[1], int(parts[2]) if len(parts) > 2 else 0,
                              parts[3] if len(parts) > 3 else "", now)
    if command == "/focus":
        if body.startswith("stop "):
            return await finish_focus(user_id, int(body.split()[1]), now)
        if body == "cancel":
            return await finish_focus(user_id, now=now)
        if not body:
            focus = await active_focus(user_id)
            return json.dumps(focus, ensure_ascii=False) if focus else "Фокус не запущен. /focus 25 goal ID или /focus 25 task ID или /focus 25 ТЕКСТ"
        parts = body.split(maxsplit=2)
        minutes = int(parts[0])
        kwargs = {"goal_id" if parts[1] == "goal" else "task_id": int(parts[2])} if len(parts) == 3 and parts[1] in {"goal", "task"} else {"title": body.partition(" ")[2] or "Один следующий шаг"}
        focus = await start_focus(user_id, minutes, now=now, **kwargs)
        return f"Фокус #{focus['id']} до {datetime.fromisoformat(focus['end']):%H:%M}: {focus['title']}. Уведомления подождут; приложения автоматически не закрываются. /focus cancel — отменить."
    if command == "/activity":
        value = body.strip().lower()
        if value in {"on", "off"}:
            await save_preferences(user_id, {"activity_enabled": value == "on"})
            return "Приём статистики " + ("включён. На ноутбуке также нужен PC_MONITOR_ENABLED=1. Содержимое окон не собирается." if value == "on" else "выключен. /activity delete — удалить принятую историю; PC_MONITOR_ENABLED=0 отключает локальный сбор.")
        if value == "delete":
            await ensure_state(user_id)
            async with engine.async_session_factory() as db:
                await db.execute(delete(ActivityReceipt).where(ActivityReceipt.user_id == user_id))
                # Do not reimport an already queued pre-deletion minute.
                await db.execute(update(AssistantState).where(AssistantState.user_id == user_id).values(
                    activity_reset_at=aware(now).replace(second=0, microsecond=0) + timedelta(minutes=1)))
                await db.commit()
            return "Принятая история удалена. Локальная история очищается agent.py --clear-activity."
        return json.dumps(await activity_summary(user_id, now), ensure_ascii=False)
    return None
