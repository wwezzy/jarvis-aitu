"""Study records distinguish proposals, timer time and user-reported results."""
from datetime import timedelta

from sqlalchemy import select

from database import engine
from database.models import Reminder
from database.time import aware, utcnow
from database.v4_models import StudySession, StudyTopic


async def save_topic(user_id, course, topic, notes=None):
    if not course.strip() or not topic.strip():
        raise ValueError("Course and topic are required")
    async with engine.async_session_factory() as db:
        row = await db.scalar(select(StudyTopic).where(StudyTopic.user_id == user_id,
            StudyTopic.course == course[:180], StudyTopic.topic == topic[:255]))
        if row is None:
            row = StudyTopic(user_id=user_id, course=course[:180], topic=topic[:255])
            db.add(row)
        row.notes = (notes or "")[:8000] or None
        await db.commit()
        return row.id


async def review_topic(user_id, topic_id, score, now=None):
    if type(score) is not int or not 0 <= score <= 5:
        raise ValueError("Self-assessment must be 0..5")
    now = aware(now or utcnow())
    async with engine.async_session_factory() as db:
        row = await db.get(StudyTopic, topic_id)
        if row is None or row.user_id != user_id:
            raise ValueError("Topic not found")
        row.last_score = score
        row.reviews = row.reviews + 1 if score >= 3 else 0
        intervals = (1, 3, 7, 14, 30)
        row.next_review_at = now + timedelta(days=intervals[min(row.reviews, len(intervals) - 1)])
        await db.commit()
        return row.next_review_at


async def start_timer(user_id, minutes, topic, now=None):
    if not 5 <= minutes <= 180 or not topic.strip():
        raise ValueError("Timer: 5..180 minutes and a topic required")
    now = aware(now or utcnow())
    async with engine.async_session_factory() as db:
        row = StudySession(user_id=user_id, topic=topic[:255], planned_minutes=minutes, started_at=now)
        db.add(row)
        await db.flush()
        db.add(Reminder(user_id=user_id, text=f"Study timer #{row.id}: {topic[:180]}. Отметь результат через /study stop.",
                        remind_at=now + timedelta(minutes=minutes)))
        await db.commit()
        return row.id


async def finish_timer(user_id, session_id, reported_minutes, now=None):
    if not 0 <= reported_minutes <= 1440:
        raise ValueError("Reported minutes must be 0..1440")
    async with engine.async_session_factory() as db:
        row = await db.get(StudySession, session_id)
        if row is None or row.user_id != user_id:
            raise ValueError("Study session not found")
        row.completed_at = aware(now or utcnow())
        row.reported_minutes = reported_minutes
        await db.commit()


async def study_context(user_id):
    async with engine.async_session_factory() as db:
        rows = list(await db.scalars(select(StudyTopic).where(StudyTopic.user_id == user_id)
            .order_by(StudyTopic.next_review_at).limit(50)))
    return [{"id": r.id, "course": r.course, "topic": r.topic, "notes": r.notes,
             "self_reported_score": r.last_score, "reviews": r.reviews,
             "next_review_at": r.next_review_at.isoformat()} for r in rows]


async def study_command(user_id, body, now):
    action, _, detail = body.strip().partition(" ")
    if action == "topic":
        parts = [v.strip() for v in detail.split("|", 2)]
        if len(parts) < 2:
            raise ValueError("/study topic COURSE | TOPIC | notes")
        topic_id = await save_topic(user_id, *parts)
        return f"Тема #{topic_id} сохранена. Оценка знаний ещё не задана."
    if action == "review":
        topic_id, score = map(int, detail.split())
        next_review = await review_topic(user_id, topic_id, score, now)
        return f"Самооценка сохранена. Следующий повтор: {next_review:%d.%m %H:%M}."
    if action == "timer":
        minutes, _, topic = detail.partition(" ")
        session_id = await start_timer(user_id, int(minutes), topic, now)
        return f"Таймер #{session_id} начат. По окончании: /study stop {session_id} ФАКТИЧЕСКИЕ_МИНУТЫ"
    if action == "stop":
        session_id, minutes = map(int, detail.split())
        await finish_timer(user_id, session_id, minutes, now)
        return "Фактическое время записано. Освоение темы автоматически не засчитывается."
    if action in {"analyze", "defense", "explain", "quiz"}:
        return None  # Normal reply pipeline supplies document/context-grounded help.
    topics = await study_context(user_id)
    return ("Study: /study topic COURSE | TOPIC | notes; /study timer 25 TOPIC; "
            "/study review ID 0..5; /study analyze; /study defense; /study quiz\n" +
            "\n".join(f"#{t['id']} [{t['course']}] {t['topic']} · самооценка {t['self_reported_score'] if t['self_reported_score'] is not None else 'нет'} · повтор {t['next_review_at']}" for t in topics))
