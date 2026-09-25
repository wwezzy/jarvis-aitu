"""Bounded iCalendar normalization and authoritative-snapshot reconciliation."""
import hashlib
import json
import os
import re
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit

from dateutil.rrule import rrulestr
from icalendar import Calendar
from sqlalchemy import select

from database.models import Assignment
from database.time import aware
from database.v4_models import LmsEvent

MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 5000
DEADLINE_TYPES = {"assignment_due", "quiz_close"}


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_text(value, limit=8000):
    parser = PlainText()
    parser.feed(str(value or ""))
    return " ".join("".join(parser.parts).split())[:limit]


def safe_url(value):
    value = str(value or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    if any(re.search(r"token|secret|password|auth|key", key, re.I) for key, _ in parse_qsl(parsed.query)):
        return None
    return value[:2000]


def classify(title, categories="", description=""):
    label = f"{title} {categories}".lower()
    if any(s in label for s in ("attendance", "посещаем", "қатысу", "присутств")):
        return "attendance"
    if any(s in label for s in ("quiz", "тест", "викторин")):
        if any(s in label for s in ("open", "откр", "начал", "ашыл")):
            return "quiz_open"
        if any(s in label for s in ("clos", "due", "закр", "дедлайн", "аяқтал")):
            return "quiz_close"
        return "other"  # A bare quiz title does not establish a deadline.
    if any(s in label for s in ("assignment", "due", "deadline", "submission", "задани", "сдать", "дедлайн", "тапсырма")):
        return "assignment_due"
    if any(s in label for s in ("lecture", "class", "lesson", "лекц", "практ", "пара", "сабақ")):
        return "class_event"
    return "other"


def stamp(prop, *, end_of_day=False):
    value = getattr(prop, "dt", prop)
    if isinstance(value, datetime):
        return aware(value)
    if isinstance(value, date):
        return aware(datetime.combine(value, time(23, 59) if end_of_day else time.min))
    if isinstance(value, str):
        try:
            return aware(datetime.fromisoformat(value))
        except ValueError:
            pass
    return None


def snapshot(raw, *, now=None):
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError("LMS feed exceeds limit")
    calendar = Calendar.from_ical(raw)
    if calendar.name != "VCALENDAR":
        raise ValueError("Expected VCALENDAR")
    now = aware(now or datetime.now().astimezone())
    window_start = stamp(calendar.get("X-JARVIS-WINDOW-START"))
    window_end = stamp(calendar.get("X-JARVIS-WINDOW-END"))
    past = int(os.getenv("LMS_WINDOW_PAST_DAYS", "0"))
    future = int(os.getenv("LMS_WINDOW_FUTURE_DAYS", "0"))
    if not window_start and future > 0 and past >= 0:
        window_start, window_end = now - timedelta(days=past), now + timedelta(days=future)
    if bool(window_start) != bool(window_end) or (window_start and window_start >= window_end):
        raise ValueError("Invalid authoritative feed window")
    expansion_start = window_start or now - timedelta(days=7)
    expansion_end = window_end or now + timedelta(days=180)
    events = {}
    components = calendar.walk("VEVENT")
    if len(components) > MAX_EVENTS:
        raise ValueError("Too many LMS events")
    # Detached recurrence overrides must replace the generated occurrence.
    components.sort(key=lambda c: c.get("RECURRENCE-ID") is not None)
    for component in components:
        title = clean_text(component.get("summary"), 255)
        if not title:
            raise ValueError("LMS event has no title")
        cats = component.get("categories")
        cats = cats if isinstance(cats, list) else [cats]
        course = ", ".join(clean_text(v, 180) for c in cats if c for v in getattr(c, "cats", [c]))[:180] or None
        description = clean_text(component.get("description")) or None
        kind = classify(title, course or "", description or "")
        start = stamp(component.get("dtstart"), end_of_day=kind in DEADLINE_TYPES)
        end = stamp(component.get("dtend"))
        due = stamp(component.get("due"), end_of_day=True) or start
        cancelled = str(component.get("status", "")).upper() == "CANCELLED"
        if start is None and not cancelled:
            raise ValueError("LMS event has no valid time")
        uid = str(component.get("uid") or "").strip()
        if not uid:
            uid = hashlib.sha256(f"{title}|{start}|{course}".encode()).hexdigest()
        if len(uid) > 180:
            uid = hashlib.sha256(uid.encode()).hexdigest()
        url = safe_url(component.get("url"))
        if not url:
            urls = re.findall(r"https?://[^\s<>]+", description or "")
            url = safe_url(urls[0].rstrip(").,;")) if urls else None
        organizer = component.get("organizer")
        teacher = clean_text(component.get("X-TEACHER") or (getattr(organizer, "params", {}).get("CN")), 180) or None
        occurrences = [start]
        recurrence_id = stamp(component.get("recurrence-id"))
        recurring = component.get("rrule") is not None or component.get("rdate") is not None
        if recurring and start is not None:
            rule_text = component.get("rrule")
            if rule_text:
                rule = rrulestr(rule_text.to_ical().decode(), dtstart=start)
                occurrences = []
                for occurrence in rule.xafter(expansion_start, count=MAX_EVENTS + 1, inc=True):
                    if occurrence > expansion_end:
                        break
                    occurrences.append(occurrence)
            else:
                occurrences = [start]
            for prop in component.get("rdate", []) if isinstance(component.get("rdate"), list) else [component.get("rdate")]:
                if prop:
                    occurrences.extend(stamp(v) for v in prop.dts)
            excluded = set()
            for prop in component.get("exdate", []) if isinstance(component.get("exdate"), list) else [component.get("exdate")]:
                if prop:
                    excluded.update(stamp(v) for v in prop.dts)
            occurrences = sorted({v for v in occurrences if v and expansion_start <= v <= expansion_end and v not in excluded})
        for occurrence in occurrences:
            external_uid = uid
            if recurring or recurrence_id:
                external_uid += ":" + (recurrence_id or occurrence).isoformat()
            shift = occurrence - start if start and occurrence else timedelta()
            item = dict(external_uid=external_uid, external_id=external_uid, event_type=kind, title=title,
                        course=course, teacher=teacher, description=description, notes=description,
                        starts_at=occurrence, ends_at=end + shift if end else None,
                        due_at=due + shift if due and kind in DEADLINE_TYPES else None,
                        url=url, status="cancelled" if cancelled else "active", cancelled=cancelled,
                        last_modified=stamp(component.get("last-modified")) or stamp(component.get("dtstamp")))
            item["raw_hash"] = hashlib.sha256(json.dumps(item, default=str, sort_keys=True).encode()).hexdigest()
            previous = events.get(external_uid)
            if previous is None or recurrence_id or (item["last_modified"] or now) >= (previous["last_modified"] or now):
                events[external_uid] = item
            if len(events) > MAX_EVENTS:
                raise ValueError("Recurrence expansion exceeds limit")
    return list(events.values()), (window_start, window_end)


async def reconcile(db, user_id, items, window, now, serialize):
    # Serialize snapshots for this user, including manual and scheduled syncs.
    from database.models import User
    await db.execute(select(User.telegram_id).where(User.telegram_id == user_id).with_for_update())
    old = {row.external_uid: row for row in await db.scalars(select(LmsEvent).where(LmsEvent.user_id == user_id))}
    tasks = {row.external_id: row for row in await db.scalars(select(Assignment).where(
        Assignment.user_id == user_id, Assignment.source == "lms_ical"))}
    seen = set()
    created = updated = unchanged = 0
    changed = []
    for item in items:
        uid = item["external_uid"]
        seen.add(uid)
        row = old.get(uid)
        if row is None:
            row = LmsEvent(user_id=user_id, external_uid=uid, first_seen_at=now)
            db.add(row)
        for key in ("event_type", "course", "teacher", "title", "description", "starts_at", "ends_at", "due_at", "url", "last_modified", "raw_hash"):
            setattr(row, key, item[key])
        if row.status != "done":
            row.status = item["status"]
        row.last_seen_at, row.missing_sync_count = now, 0
        task = tasks.get(uid)
        if item["event_type"] not in DEADLINE_TYPES:
            if task and task.status != "done":
                task.status = "cancelled"  # Repair legacy attendance-as-deadline records.
                updated += 1
                changed.append(serialize(task))
            continue
        if task is None:
            task = Assignment(user_id=user_id, source="lms_ical", external_id=uid, title=item["title"],
                              provenance="LMS iCalendar", confidence=1.0,
                              status="cancelled" if item["cancelled"] else "pending")
            db.add(task)
            tasks[uid] = task
            created += 1
            before = None
        else:
            before = (task.title, task.course, task.due_at, task.notes, task.url, task.status)
        task.title, task.course, task.notes, task.url = item["title"], item["course"], item["description"], item["url"]
        if not task.deadline_overridden and item["due_at"] is not None:
            task.due_at = item["due_at"]
        if task.status != "done":
            task.status = "cancelled" if item["cancelled"] else "pending"
        after = (task.title, task.course, task.due_at, task.notes, task.url, task.status)
        await db.flush()
        await db.refresh(task)
        if before != after:
            updated += int(before is not None)
            changed.append(serialize(task))
        else:
            unchanged += 1
    left, right = window
    threshold = max(2, int(os.getenv("LMS_MISSING_SYNC_THRESHOLD", "3")))
    if left and right:
        for uid, row in old.items():
            anchor = row.due_at or row.starts_at
            if uid in seen or not anchor or not left <= anchor <= right or row.status == "done":
                continue
            row.missing_sync_count += 1
            task = tasks.get(uid)
            if row.missing_sync_count >= threshold:
                row.status = "cancelled"
                if task and task.status not in {"done", "cancelled"}:
                    task.status = "cancelled"
                    updated += 1
                    changed.append(serialize(task))
    return dict(configured=True, created=created, updated=updated, unchanged=unchanged,
                changed=changed[:20], events_seen=len(items), authoritative_window=bool(left and right))
