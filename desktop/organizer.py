"""Offline, user-authored plans; notifications and self-reports grant no authority.

Dates and recurring rules use UTC+5, independently of the host timezone. A
missing report always means unknown. Past occurrences are not inferred to be
missed; elapsed timers and application use are not evidence of completion.
"""
from __future__ import annotations

import hashlib
import json
import re
import time as system_time
from datetime import date, datetime, time, timedelta, timezone


LOCAL_ZONE = timezone(timedelta(hours=5))
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_KINDS = {"goal", "task", "study", "workout", "rest"}
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_DATE = r"(?:сегодня|завтра|вчера|[0-9]{4}-[0-9]{2}-[0-9]{2})"
_DAY_NAMES = {
    "пн": 0, "понедельник": 0, "понедельникам": 0,
    "вт": 1, "вторник": 1, "вторникам": 1,
    "ср": 2, "среда": 2, "среду": 2, "средам": 2,
    "чт": 3, "четверг": 3, "четвергам": 3,
    "пт": 4, "пятница": 4, "пятницу": 4, "пятницам": 4,
    "сб": 5, "суббота": 5, "субботу": 5, "субботам": 5,
    "вс": 6, "воскресенье": 6, "воскресеньям": 6,
}


def _date(value) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Date must be YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if not 1900 <= parsed.year <= 2200:
        raise ValueError("Date is outside the supported range")
    return parsed.isoformat()


def _time(value):
    if value is not None and (not isinstance(value, str) or not _CLOCK.fullmatch(value)):
        raise ValueError("Time must be HH:MM or None")
    return value


def _minutes(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 1440):
        raise ValueError("Minutes must be 1..1440 or None")
    return value


def _title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or "\x00" in value:
        raise ValueError("Title must contain 1..1000 characters")
    return value.strip()


class LocalOrganizer:
    """Small public adapter shared by desktop and future explicitly paired clients.

    Item ids are opaque strings. ``goal:N`` identifies a dated local item;
    ``routine:KEY:YYYY-MM-DD`` identifies one occurrence of a current rule.
    Removing an occurrence creates a dated skip, never removes the weekly rule.
    """

    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock if clock is not None else system_time.time

    def _local(self, now=None) -> datetime:
        now = self.clock() if now is None else now
        if isinstance(now, datetime):
            if now.tzinfo is None:
                raise ValueError("A timezone-aware datetime is required")
            return now.astimezone(LOCAL_ZONE)
        return datetime.fromtimestamp(self.store._moment(now), LOCAL_ZONE)

    def _resolve_date(self, value, now=None):
        today = self._local(now).date()
        offsets = {"сегодня": 0, "завтра": 1, "вчера": -1}
        return _date(today + timedelta(days=offsets[value])) if value in offsets else _date(value)

    @staticmethod
    def _identity(item_id):
        if not isinstance(item_id, str):
            raise ValueError("Invalid plan item id")
        match = re.fullmatch(r"goal:([1-9][0-9]{0,12})", item_id)
        if match:
            return "goal", int(match[1]), None
        match = re.fullmatch(r"routine:([a-z][a-z0-9_-]{0,39}):([0-9]{4}-[0-9]{2}-[0-9]{2})", item_id)
        if match:
            return "routine", match[1], _date(match[2])
        raise ValueError("Invalid plan item id")

    def set_routine(self, key, title, days, *, start_time=None, minutes=None, enabled=True):
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", key):
            raise ValueError("Invalid routine key")
        if not isinstance(days, (list, tuple, set)) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
            raise ValueError("Days must be integers 0..6")
        if type(enabled) is not bool:
            raise ValueError("Enabled must be boolean")
        days = sorted(set(days))
        if enabled and not days:
            raise ValueError("An enabled routine needs at least one weekday")
        title, start_time, minutes = _title(title), _time(start_time), _minutes(minutes)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT key FROM organizer_routines WHERE key=?", (key,)).fetchone()
            if not exists and db.execute("SELECT count(*) FROM organizer_routines").fetchone()[0] >= 32:
                raise ValueError("At most 32 weekly routines are supported")
            db.execute("""INSERT INTO organizer_routines(key,title,days,start_time,minutes,enabled,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,
                days=excluded.days,start_time=excluded.start_time,minutes=excluded.minutes,
                enabled=excluded.enabled,version=version+1,updated_at=excluded.updated_at""",
                       (key, title, json.dumps(days), start_time, minutes, enabled, self._local().timestamp()))
        return self.routine_settings(key)

    def routine_settings(self, key):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM organizer_routines WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["days"] = json.loads(value["days"])
        value["time"] = value.pop("start_time")
        value["enabled"] = bool(value["enabled"])
        return value

    def gym_settings(self):
        return self.routine_settings("gym") or {
            "key": "gym", "title": "Зал", "days": [], "time": None,
            "minutes": None, "enabled": False, "version": 0,
        }

    def set_weekly_gym(self, days, time=None, minutes=None, enabled=True):
        return self.set_routine("gym", "Зал", days, start_time=time, minutes=minutes, enabled=enabled)

    def add_item(self, title, on_date, start_time=None, *, kind="goal", minutes=None):
        title, on_date = _title(title), _date(on_date)
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ValueError("Unsupported plan item kind")
        start_time, minutes = _time(start_time), _minutes(minutes)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM organizer_items WHERE deleted=0").fetchone()[0] >= 1000:
                raise ValueError("At most 1000 active dated items are supported")
            item_id = db.execute("""INSERT INTO organizer_items(title,on_date,start_time,minutes,kind,updated_at)
                VALUES(?,?,?,?,?,?)""", (title, on_date, start_time, minutes, kind, self._local().timestamp())).lastrowid
        return f"goal:{item_id}"

    # Explicit name useful to other local adapters; no model/tool dispatcher.
    plan_goal = add_item

    def _day(self, db, on_date):
        weekday = date.fromisoformat(on_date).weekday()
        items = []
        for row in db.execute("SELECT * FROM organizer_routines WHERE enabled=1 ORDER BY key"):
            if weekday not in json.loads(row["days"]):
                continue
            override = db.execute("SELECT * FROM organizer_overrides WHERE routine_key=? AND on_date=?",
                                  (row["key"], on_date)).fetchone()
            if override and override["skipped"]:
                continue
            patch = json.loads(override["patch"]) if override else {}
            items.append({"id": f"routine:{row['key']}:{on_date}", "date": on_date,
                          "title": patch.get("title", row["title"]), "time": patch.get("start_time", row["start_time"]),
                          "minutes": patch.get("minutes", row["minutes"]), "kind": "routine",
                          "routine_key": row["key"], "rule_version": row["version"],
                          "override_version": override["version"] if override else 0})
        for row in db.execute("SELECT * FROM organizer_items WHERE on_date=? AND deleted=0 ORDER BY id", (on_date,)):
            items.append({"id": f"goal:{row['id']}", "title": row["title"], "date": on_date,
                          "time": row["start_time"], "minutes": row["minutes"], "kind": row["kind"],
                          "rule_version": None, "version": row["version"]})
        for item in items:
            report = db.execute("SELECT * FROM organizer_reports WHERE item_id=? AND on_date=? ORDER BY id DESC LIMIT 1",
                                (item["id"], on_date)).fetchone()
            item.update(status=report["status"] if report else "unknown",
                        provenance=report["provenance"] if report else None,
                        reported_minutes=report["minutes"] if report else None,
                        reported_at=report["reported_at"] if report else None,
                        report_id=report["id"] if report else None)
        return {"date": on_date, "timezone": "UTC+5", "items": sorted(items, key=lambda item: (item["time"] or "99:99", item["id"]))}

    def day(self, on_date=None, *, now=None):
        on_date = _date(on_date if on_date is not None else self._local(now).date())
        with self.store.connection() as db:
            return self._day(db, on_date)

    def snapshot(self, *, now=None):
        today = self._local(now).date()
        with self.store.connection() as db:
            keys = [row[0] for row in db.execute("SELECT key FROM organizer_routines ORDER BY key")]
            current, tomorrow = self._day(db, today.isoformat()), self._day(db, (today + timedelta(days=1)).isoformat())
        return {"today": current, "tomorrow": tomorrow, "routines": [self.routine_settings(key) for key in keys],
                "preferences": self.store.preferences()}

    def _find(self, db, item_id):
        kind, key, on_date = self._identity(item_id)
        if kind == "goal":
            row = db.execute("SELECT on_date FROM organizer_items WHERE id=? AND deleted=0", (key,)).fetchone()
            if not row:
                return None
            on_date = row[0]
        return next((item for item in self._day(db, on_date)["items"] if item["id"] == item_id), None)

    def update_item(self, item_id, **fields):
        fields = dict(fields)
        for alias, name in (("date", "on_date"), ("time", "start_time")):
            if alias in fields:
                if name in fields:
                    raise ValueError("Duplicate field")
                fields[name] = fields.pop(alias)
        if not fields or set(fields) - {"title", "on_date", "start_time", "minutes"}:
            raise ValueError("Unsupported plan fields")
        validators = {"title": _title, "on_date": _date, "start_time": _time, "minutes": _minutes}
        fields = {key: validators[key](value) for key, value in fields.items()}
        kind, key, on_date = self._identity(item_id)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self._find(db, item_id)
            if not item:
                raise ValueError("Plan item not found")
            if kind == "goal":
                # Column names are taken exclusively from the finite validators.
                assignments = ",".join(f"{name}=?" for name in fields)
                db.execute(f"UPDATE organizer_items SET {assignments},version=version+1,updated_at=? WHERE id=?",
                           (*fields.values(), self._local().timestamp(), key))
            else:
                target_date = fields.pop("on_date", on_date)
                if target_date != on_date:
                    # One moved occurrence is a dated item plus a skip. The
                    # recurring rule and other days remain untouched.
                    if db.execute("SELECT count(*) FROM organizer_items WHERE deleted=0").fetchone()[0] >= 1000:
                        raise ValueError("At most 1000 active dated items are supported")
                    moved = db.execute("""INSERT INTO organizer_items(title,on_date,start_time,minutes,kind,updated_at)
                        VALUES(?,?,?,?,?,?)""", (fields.get("title", item["title"]), target_date,
                        fields.get("start_time", item["time"]), fields.get("minutes", item["minutes"]),
                        "workout" if key == "gym" else "task", self._local().timestamp())).lastrowid
                    self._override(db, key, on_date, {}, skipped=True)
                    return f"goal:{moved}"
                previous = db.execute("SELECT patch FROM organizer_overrides WHERE routine_key=? AND on_date=?", (key, on_date)).fetchone()
                patch = {**(json.loads(previous[0]) if previous else {}), **fields}
                self._override(db, key, on_date, patch)
        return item_id

    def _override(self, db, key, on_date, patch, *, skipped=False):
        db.execute("""INSERT INTO organizer_overrides(routine_key,on_date,patch,skipped,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(routine_key,on_date) DO UPDATE SET patch=excluded.patch,
            skipped=excluded.skipped,version=version+1,updated_at=excluded.updated_at""",
                   (key, on_date, json.dumps(patch, ensure_ascii=False), skipped, self._local().timestamp()))

    def delete_item(self, item_id):
        kind, key, on_date = self._identity(item_id)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._find(db, item_id):
                return False
            if kind == "goal":
                db.execute("UPDATE organizer_items SET deleted=1,version=version+1,updated_at=? WHERE id=?",
                           (self._local().timestamp(), key))
            else:
                self._override(db, key, on_date, {}, skipped=True)
        return True

    def report_item(self, item_id, status="done", minutes=None):
        if not isinstance(status, str) or status not in {"unknown", "done", "missed"}:
            raise ValueError("Unsupported completion status")
        minutes = _minutes(minutes)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self._find(db, item_id)
            if not item:
                raise ValueError("Plan item not found")
            db.execute("""INSERT INTO organizer_reports(item_id,on_date,status,minutes,provenance,reported_at)
                VALUES(?,?,?,?, 'self_report',?)""", (item_id, item["date"], status, minutes, self._local().timestamp()))
        return {"item_id": item_id, "status": status, "minutes": minutes, "provenance": "self_report"}

    set_status = report_item

    @staticmethod
    def _line(item):
        labels = {"unknown": "нет отметки", "done": "выполнено со слов пользователя", "missed": "пропуск со слов пользователя"}
        return (f"{item['time'] or 'Время свободное'} · {item['title'][:180]} · {labels[item['status']]}"
                + (f"; {item['reported_minutes']} мин. со слов пользователя" if item["reported_minutes"] else ""))

    def _plan_text(self, on_date):
        items = self.day(on_date)["items"]
        label = date.fromisoformat(on_date).strftime("%d.%m.%Y")
        lines = [f"План на {label}, UTC+5."]
        lines.extend(self._line(item) for item in items[:12])
        if not items:
            lines.append("Датированных дел пока нет. Можно добавить одну цель на этот день.")
        if len(items) > 12:
            lines.append("Остальные дела — в разделе «План».")
        return "\n".join(lines)

    def handle(self, text, *, now=None):
        """Parse original user text only; return None for unrelated requests."""
        if not isinstance(text, str) or not text.strip() or len(text) > 20000 or "\x00" in text:
            return None
        original = text.strip()
        value = re.sub(r"\s+", " ", original.casefold().replace("ё", "е")).rstrip(".!?")
        gym = re.fullmatch(r"(?:я\s+)?(?:качаюсь|хожу в зал|тренируюсь)\s+(.+)", value)
        if gym:
            tail = re.sub(r"^(?:по|во|в)\s+", "", gym[1])
            clock = re.search(r"\s+в\s*([0-9]{1,2}:[0-9]{2})$", tail)
            start_time = clock[1].zfill(5) if clock else None
            tail = tail[:clock.start()] if clock else tail
            tokens = [token for token in re.split(r"[,;/\s]+", tail) if token and token != "и"]
            try:
                if not tokens or any(token not in _DAY_NAMES for token in tokens):
                    raise ValueError("Unknown weekday")
                settings = self.set_weekly_gym([_DAY_NAMES[token] for token in tokens], time=start_time)
            except ValueError:
                return "Укажи дни, например: «я хожу в зал во вт чт вс» или «я качаюсь во вт чт вс в 18:00». Время можно не задавать."
            days = ", ".join(WEEKDAYS[day] for day in settings["days"])
            return f"Запомнил правило зала: {days}. " + (f"Время {start_time}, UTC+5." if start_time else "Время гибкое; напоминаний по часам не будет.")
        if value in {"мой план", "план", "планы", "план на сегодня", "план сегодня", "что у меня сегодня", "сегодня",
                     "расписание", "мое расписание", "schedule", "my schedule", "today", "my day"}:
            return self._plan_text(self._local(now).date().isoformat())
        if value in {"план на завтра", "план завтра", "что у меня завтра", "завтра"}:
            return self._plan_text((self._local(now).date() + timedelta(days=1)).isoformat())
        target = re.fullmatch(r"план(?: на)? ([0-9]{4}-[0-9]{2}-[0-9]{2})", value)
        if target:
            try:
                return self._plan_text(_date(target[1]))
            except ValueError:
                return "Нужна существующая дата плана в формате ГГГГ-ММ-ДД."
        add = re.fullmatch(r"(?:(?:добавь|добавить)\s+(?:цель|план|задачу)|запланируй)(?:\s+на)?\s+(" + _DATE + r")(?:\s+в\s+([0-9]{1,2}:[0-9]{2}))?\s*[:—-]?\s+(.+)", original, re.I | re.S)
        if add:
            try:
                on_date = self._resolve_date(add[1].casefold(), now)
                item_id = self.add_item(add[3], on_date, add[2].zfill(5) if add[2] else None)
            except ValueError:
                return "Не сохранил план: проверь дату, время ЧЧ:ММ и название до 1000 символов."
            return f"Добавил {item_id} в план на {on_date}: {add[3].strip()}"
        gym_report = re.fullmatch(r"(?:я\s+)?(сходил в зал|потренировался|пропустил зал)(?:\s+(" + _DATE + r"))?", value)
        if gym_report:
            try:
                on_date = self._resolve_date(gym_report[2] or "сегодня", now)
                items = self.day(on_date)["items"]
                item = next((item for item in items if item.get("routine_key") == "gym"), None)
                item_id = item["id"] if item else self.add_item("Зал вне недельного плана", on_date, kind="workout")
                status = "missed" if gym_report[1] == "пропустил зал" else "done"
                self.report_item(item_id, status=status)
            except ValueError:
                return "Не смог записать отметку: проверь дату."
            return f"Записал на {on_date}: " + ("зал выполнен" if status == "done" else "пропуск зала") + " с твоих слов. Самостоятельно проверить тренировку не могу."
        study = re.fullmatch(r"(?:я\s+)?учился\s+([0-9]{1,4})\s+минут(?:у|ы)?(?:\s+(" + _DATE + r"))?", value)
        if study:
            try:
                minutes = _minutes(int(study[1]))
                item_id = self.add_item("Учёба", self._resolve_date(study[2] or "сегодня", now), kind="study")
                self.report_item(item_id, minutes=minutes)
            except ValueError:
                return "Длительность учёбы должна быть от 1 до 1440 минут; дату можно указать явно."
            return f"Записал {minutes} минут учёбы с твоих слов. Это самоотчёт, а не автоматическая проверка результата."
        skip = re.fullmatch(r"(?:пропусти|убери)\s+зал(?:\s+(" + _DATE + r"))?", value)
        if skip:
            try:
                on_date = self._resolve_date(skip[1] or "сегодня", now)
                removed = self.delete_item(f"routine:gym:{on_date}")
            except ValueError:
                removed = False
            return "Убрал зал только из плана на этот день. Недельное правило сохранено." if removed else "На этот день в текущем правиле зала нет."
        move = re.fullmatch(r"перенеси\s+(?:цель\s+#?([0-9]{1,13})|зал)(?:\s+с\s+(" + _DATE + r"))?\s+на\s+(" + _DATE + r")(?:\s+в\s+([0-9]{1,2}:[0-9]{2}))?", value)
        if move:
            try:
                item_id = f"goal:{int(move[1])}" if move[1] else f"routine:gym:{self._resolve_date(move[2] or 'сегодня', now)}"
                fields = {"on_date": self._resolve_date(move[3], now)}
                if move[4]:
                    fields["start_time"] = move[4].zfill(5)
                self.update_item(item_id, **fields)
            except ValueError:
                return "Не перенёс: проверь существующее дело, дату и время."
            return "Перенёс одно дело; остальные дни недельного правила не изменились."
        report = re.fullmatch(r"(?:отметь\s+)?цель\s+#?([0-9]{1,13})\s+(выполнена|пропущена|неизвестно)", value)
        if report:
            try:
                self.report_item(f"goal:{int(report[1])}", {"выполнена": "done", "пропущена": "missed", "неизвестно": "unknown"}[report[2]])
            except ValueError:
                return "Такой цели в текущем плане нет."
            return "Записал твою отметку. Статус основан на самоотчёте."
        toggles = {
            "пауза планировщика": {"organizer_paused": True},
            "возобнови планировщик": {"organizer_paused": False},
            "тихий режим": {"organizer_dnd": True}, "отключи тихий режим": {"organizer_dnd": False},
            "выключи утренний план": {"morning_brief_enabled": False},
            "включи утренний план": {"morning_brief_enabled": True},
            "выключи вечерний план": {"evening_plan_enabled": False},
            "включи вечерний план": {"evening_plan_enabled": True},
        }
        if value in toggles:
            self.store.set_preferences(toggles[value])
            return "Настройки планировщика сохранены на этом компьютере."
        if value.startswith(("добавь цель", "цель на ", "запланируй ", "я качаюсь ", "я хожу в зал ", "я учился ", "учился ")):
            return "Уточни план: «добавь цель на завтра закончить проект», «я хожу в зал во вт чт вс» или «учился 40 минут»."
        return None

    def _brief(self, on_date, kind):
        items = self.day(on_date)["items"]
        if kind == "morning":
            lines = ["Сэр, сегодня " + date.fromisoformat(on_date).strftime("%d.%m.%Y") + ". Вот твой текущий локальный план:"]
        else:
            lines = ["Сэр, пора выбрать план на завтра, " + date.fromisoformat(on_date).strftime("%d.%m.%Y") + ":"]
        lines.extend(self._line(item) for item in items[:6])
        if not items:
            lines.append("Датированных дел пока нет. Какую одну цель добавим?")
        if len(items) > 6:
            lines.append("Остальные дела видны в разделе «План».")
        if kind == "morning":
            tasks = [task for task in self.store.snapshot()["tasks"] if not task["done"]]
            if tasks:
                lines.append("Без назначенной даты: " + "; ".join(task["title"][:120] for task in tasks[:3]) + ". Это очередь, а не обязательства на сегодня.")
        return "\n".join(lines)

    @staticmethod
    def _signature(item):
        return hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _enabled(self):
        prefs = self.store.preferences()
        return prefs if prefs["organizer_enabled"] and not prefs["organizer_paused"] and not prefs["organizer_dnd"] else None

    def due_events(self, now=None, trigger="tick"):
        """Claim at most once before delivery; no OS/voice/network side effects.

        Morning needs an actual startup/interaction during 05:00..12:00.
        Evening catches up for one hour. A clocked routine may remind within
        15 minutes after its start, then ask once after 60 minutes (one-hour
        catch-up). These windows do not assert a workout duration or outcome.
        Call event_is_current immediately before queued UI/voice delivery.
        """
        if trigger not in {"tick", "startup", "interaction"}:
            raise ValueError("Unsupported organizer trigger")
        local = self._local(now)
        prefs = self._enabled()
        if not prefs:
            return []
        stamp, today = local.timestamp(), local.date().isoformat()
        candidates = []
        midnight = datetime.combine(local.date(), time(), LOCAL_ZONE)
        if trigger in {"startup", "interaction"} and prefs["morning_brief_enabled"] and 5 <= local.hour < 12:
            candidates.append({"id": f"morning:{today}", "kind": "morning", "date": today,
                               "text": self._brief(today, "morning"), "expires_at": (midnight + timedelta(hours=12)).timestamp()})
        evening = datetime.combine(local.date(), time.fromisoformat(prefs["evening_plan_time"]), LOCAL_ZONE)
        # A configured late-night clock may have its one-hour window cross midnight.
        if local < evening:
            previous = evening - timedelta(days=1)
            if previous <= local < previous + timedelta(hours=1):
                evening = previous
        if prefs["evening_plan_enabled"] and evening <= local < evening + timedelta(hours=1):
            target = (evening.date() + timedelta(days=1)).isoformat()
            candidates.append({"id": f"evening:{evening.date().isoformat()}", "kind": "evening", "date": target,
                               "text": self._brief(target, "evening"), "expires_at": (evening + timedelta(hours=1)).timestamp(),
                               "planning_time": prefs["evening_plan_time"]})
        # A Tuesday routine must not produce a Wednesday workout notification,
        # even when a late-night start would push its follow-up past midnight.
        for on_date in (today,):
            for item in self.day(on_date)["items"]:
                if item["kind"] != "routine" or not item["time"] or item["status"] != "unknown":
                    continue
                start = datetime.combine(date.fromisoformat(on_date), time.fromisoformat(item["time"]), LOCAL_ZONE)
                for kind, due, window in (("routine", start, 900), ("followup", start + timedelta(hours=1), 3600)):
                    if not due.timestamp() <= stamp < due.timestamp() + window:
                        continue
                    text = (f"Сэр, по твоему плану сейчас: {item['title'][:180]}. Отметь результат, когда будет удобно."
                            if kind == "routine" else f"Сэр, по делу «{item['title'][:180]}» пока нет твоей отметки. Получилось выполнить, перенесём или оставим статус неизвестным?")
                    candidates.append({"id": f"{kind}:{item['id']}", "kind": kind, "date": on_date,
                                       "item_id": item["id"], "text": text, "rule_version": item["rule_version"],
                                       "signature": self._signature(item), "expires_at": due.timestamp() + window})
        claimed = []
        for event in candidates:
            if len(claimed) >= 12:
                break
            if not self.event_is_current(event, now=local):
                continue
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                # Recheck rule/report versions under the same write lock as
                # the claim, so an edit cannot leave a stale durable claim.
                if event.get("item_id"):
                    item = self._find(db, event["item_id"])
                    if not item or self._signature(item) != event["signature"]:
                        continue
                inserted = db.execute("""INSERT INTO organizer_deliveries(event_key,kind,on_date,claimed_at)
                    VALUES(?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                                      (event["id"], event["kind"], event["date"], stamp)).rowcount
            if inserted:
                claimed.append(event)
        return claimed

    def event_is_current(self, event, *, now=None):
        """Revalidate queued literals after edits, skips, reports, opt-out or DND."""
        if not isinstance(event, dict):
            return False
        prefs = self._enabled()
        if not prefs:
            return False
        try:
            stamp = self._local(now).timestamp()
            if stamp >= self.store._moment(event["expires_at"]):
                return False
            if event["kind"] in {"morning", "evening"}:
                flag = "morning_brief_enabled" if event["kind"] == "morning" else "evening_plan_enabled"
                return (prefs[flag] and (event["kind"] != "evening" or event.get("planning_time") == prefs["evening_plan_time"])
                        and event["text"] == self._brief(_date(event["date"]), event["kind"]))
            if event["kind"] not in {"routine", "followup"}:
                return False
            with self.store.connection() as db:
                item = self._find(db, event["item_id"])
            return bool(item and item["status"] == "unknown" and self._signature(item) == event["signature"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
