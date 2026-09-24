"""Explainable deterministic risk and bounded planning; independent of AI."""
from datetime import datetime, time, timedelta

from database.time import aware
from services.preferences import Preferences, get_preferences
from services.schedule import resolve_day
from services.tasks import list_tasks


def _stamp(value):
    return aware(datetime.fromisoformat(value)) if value else None


def remaining_minutes(task):
    estimate = task.get("estimated_minutes")
    return max(0, round(estimate * (1 - task.get("progress", 0) / 100))) if estimate else None


def risk_score(task, now, *, free_minutes=0, preferences=None):
    preferences = preferences or Preferences()
    if task.get("status") != "pending":
        return {"score": 0, "components": {}, "uncertainties": []}
    now = aware(now)
    due = _stamp(task.get("due_at"))
    hours = (due - now).total_seconds() / 3600 if due else None
    remaining = remaining_minutes(task)
    prep = task.get("preparation_minutes") or 0
    buffer = task.get("testing_buffer_minutes") or 0
    required = (remaining or 0) + prep + buffer
    components = {
        "deadline": 35 if hours is not None and hours <= 0 else 30 if hours is not None and hours <= 24 else 22 if hours is not None and hours <= 72 else 12 if hours is not None and hours <= 168 else 0,
        "importance": (task.get("importance") or 5) * 1.5,
        "consequence": (task.get("consequence") or 5) * 1.5,
        "capacity_gap": min(20, max(0, required - free_minutes) / max(1, required) * 20) if remaining is not None else 0,
        "preparation": min(5, prep / 12),
        "testing_buffer": 5 if hours is not None and hours * 60 < required else 0,
        "course_preference": preferences.course_weights.get(task.get("course"), 5),
        "progress": round(5 * (1 - task.get("progress", 0) / 100), 1),
    }
    uncertainties = []
    if due is None:
        uncertainties.append("deadline unknown")
    if remaining is None:
        uncertainties.append("effort unknown; estimate before promising a finish time")
    return {"score": min(100, round(sum(components.values()), 1)), "components": components,
            "uncertainties": uncertainties, "remaining_minutes": remaining, "free_minutes": free_minutes}


def free_slots(day, blocks, now, preferences):
    left = aware(datetime.combine(day, time.fromisoformat(preferences.day_start)))
    right = aware(datetime.combine(day, time.fromisoformat(preferences.day_end)))
    cursor = max(left, aware(now))
    occupied = []
    protected = {"sleep", "rest", "recovery", "nutrition", "routine", "commute", "training", "chores"}
    for block in blocks:
        # General planned study slots are capacity. Fixed classes, training and
        # explicit protected rest remain immovable even if marked flexible.
        if block.get("block_type") != "fixed" and block.get("category") not in protected:
            continue
        start = aware(datetime.combine(day, time.fromisoformat(block["start"])))
        end = aware(datetime.combine(day, time.fromisoformat(block["end"])))
        start -= timedelta(minutes=(block.get("commute_minutes") or 0) + (block.get("preparation_minutes") or 0))
        end += timedelta(minutes=block.get("commute_minutes") or 0)
        occupied.append((start, end))
    result = []
    for start, end in sorted(occupied):
        if start > cursor:
            result.append((cursor, min(start, right)))
        cursor = max(cursor, end)
    if cursor < right:
        result.append((cursor, right))
    return [(a, b) for a, b in result if b > a and (b - a).total_seconds() >= 15 * 60]


async def make_plan(user_id, now=None, *, minutes=None):
    now = aware(now or datetime.now().astimezone())
    preferences = await get_preferences(user_id)
    tasks = await list_tasks(user_id, status="pending")
    slots = []
    daily_capacity = {}
    for offset in range(7):
        day = now.date() + timedelta(days=offset)
        available = free_slots(day, await resolve_day(user_id, day), now, preferences)
        total = sum(int((b - a).total_seconds() / 60) for a, b in available)
        daily_capacity[day] = min(preferences.max_work_minutes, int(total * (1 - preferences.free_fraction)))
        slots.extend(available)
    if minutes is not None:
        if not 15 <= minutes <= 600:
            raise ValueError("Planning window must be 15..600 minutes")
        end = now + timedelta(minutes=minutes)
        slots = [(a, min(b, end)) for a, b in slots if a < end]
    risks = {}
    for task in tasks:
        due = _stamp(task.get("due_at"))
        capacity = sum(max(0, (min(b, due) - a).total_seconds() / 60) if due else (b - a).total_seconds() / 60 for a, b in slots)
        risks[task["id"]] = risk_score(task, now, free_minutes=int(capacity * (1 - preferences.free_fraction)), preferences=preferences)
    ranked = sorted(tasks, key=lambda t: (-risks[t["id"]]["score"], t["due_at"] or "9999", t["id"]))
    allocations, unscheduled = [], []
    for task in ranked:
        left = remaining_minutes(task)
        if left is None:
            unscheduled.append({"task_id": task["id"], "reason": "effort unknown", "title": task["title"]})
            continue
        left += task.get("preparation_minutes") or 0
        due = _stamp(task.get("due_at"))
        cutoff = due - timedelta(minutes=task.get("testing_buffer_minutes") or 0) if due else None
        for index, (start, end) in enumerate(slots):
            available_end = min(end, cutoff) if cutoff else end
            capacity = max(0, int((available_end - start).total_seconds() / 60))
            while left > 0 and capacity >= 15 and daily_capacity.get(start.date(), 0) >= 15:
                chunk = min(50, left, capacity, daily_capacity[start.date()])
                finish = start + timedelta(minutes=chunk)
                allocations.append({"task_id": task["id"], "title": task["title"], "start": start.isoformat(),
                                    "end": finish.isoformat(), "minutes": chunk, "status": "planned"})
                daily_capacity[start.date()] -= chunk
                left -= chunk
                start = finish + timedelta(minutes=10)  # Recovery between focus blocks.
                capacity = max(0, int((available_end - start).total_seconds() / 60))
                slots[index] = (start, end)
            if left <= 0:
                break
        if left > 0:
            unscheduled.append({"task_id": task["id"], "title": task["title"], "remaining_minutes": left,
                                "reason": "insufficient free time before submission buffer"})
    allocations.sort(key=lambda b: b["start"])
    return {"generated_at": now.isoformat(), "blocks": allocations, "risks": risks,
            "unscheduled": unscheduled, "overloaded": any("remaining_minutes" in item for item in unscheduled),
            "next_action": allocations[0] if allocations else None,
            "assumptions": ["Estimates are user supplied", "Planned blocks are not completion records",
                            f"Reserve {preferences.free_fraction:.0%} of free capacity plus focus breaks"]}


async def sleep_plan(user_id, now=None):
    now = aware(now or datetime.now().astimezone())
    preferences = await get_preferences(user_id)
    target = now.date() + timedelta(days=1)
    blocks = await resolve_day(user_id, target)
    fixed = sorted((b for b in blocks if b["block_type"] == "fixed"), key=lambda b: b["start"])
    first = fixed[0] if fixed else None
    overhead = preferences.breakfast_minutes + preferences.shower_minutes + preferences.preparation_minutes
    if first:
        anchor = aware(datetime.combine(target, time.fromisoformat(first["start"])))
        overhead += (first.get("commute_minutes") or (0 if first.get("location") == "online" else preferences.default_commute_minutes))
        overhead += first.get("preparation_minutes") or 0
        wake = anchor - timedelta(minutes=overhead)
    else:
        wake = aware(datetime.combine(target, time.fromisoformat(preferences.day_start)))
    bedtime = wake - timedelta(hours=preferences.sleep_hours)
    return {"first_fixed": first, "wake_at": wake.isoformat(), "bedtime": bedtime.isoformat(),
            "target_hours": preferences.sleep_hours, "available_hours": max(0, round((wake - now).total_seconds() / 3600, 1)),
            "preparation_minutes": overhead, "shortfall_minutes": max(0, int((now - bedtime).total_seconds() / 60))}
