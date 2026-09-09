# Jarvis v2.2 — Reliability + LMS + Deadline Planner

## What changed

### 1. Gemini reliability
- Supports a primary Gemini project plus backup project keys.
- Structured-output failure no longer immediately kills the conversation.
- Retryable 429/5xx/timeouts fail over to backup projects.
- If structured JSON still fails, Jarvis falls back to a plain-text Gemini answer.
- `/diag` exposes the last LLM state without exposing API keys.

Environment:
- `GEMINI_API_KEY` — primary
- `GEMINI_API_KEY_2` … `GEMINI_API_KEY_5` — optional backup projects
- `GEMINI_FALLBACK_MODEL`
- `LLM_TIMEOUT_SECONDS`

### 2. Direct deterministic answers
Short queries about:
- current schedule
- tomorrow's schedule
- next class/block
- deadlines / LMS tasks

are answered from SQL directly without Gemini.

### 3. 7-day planning context
LLM context now contains:
- the next 7 days of editable schedule blocks
- upcoming assignments/deadlines
- durable memory
- recent structured workouts
- GTG and reflections

This fixes planning requests that previously only saw today's schedule.

### 4. Assignments
New SQL `assignments` table.
Jarvis can extract homework/deadlines from normal Telegram messages into structured records.

Commands:
- `/deadlines`
- `/done <id>`

Automatic deadline notices:
- 24 hours before
- 3 hours before
- 30 minutes before
- morning 7-day digest at 08:10

### 5. Moodle/LMS iCal sync
Set a personal Moodle calendar-export URL in Render:
- `LMS_ICAL_URL`
- `LMS_SYNC_MINUTES=15`

Jarvis will:
- fetch the feed periodically
- upsert changed LMS events/deadlines
- schedule Telegram notices
- expose sync state through `/diag`
- show deadlines in the Mini App

Never commit or paste the personal iCal URL publicly.

### 6. Mini App
Today dashboard now shows:
- upcoming assignments
- LMS connection status
- manual LMS sync button
- Done button for assignments

### 7. Database durability warning
The code still supports SQLite for development.
On Render, use PostgreSQL for durable memory/deadlines across redeploys:
`DATABASE_URL=postgresql+asyncpg://...`

`/diag` warns when the app is still using SQLite.
