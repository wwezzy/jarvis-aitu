# Jarvis v2.1 — Week Planner update

This update replaces hard-coded DAY A / DAY B with a persistent weekly schedule.

## What changed

- Real AITU timetable for Monday–Saturday.
- Green `Online` university classes are treated as asynchronous/flexible blocks.
- Full-body A / B / C on Monday / Wednesday / Friday.
- Pool on Tuesday; optional pool/walk on Saturday.
- Daily short self-correction / LFK blocks.
- Protected Friday/Saturday/Sunday recovery time.
- Sunday Reset: cleaning, laundry, groceries, weekly review, Monday preparation.
- New **Week** tab inside Telegram Mini App.
- Add / edit / delete blocks directly from the Mini App.
- Block types: Fixed / Planned / Flex.
- Per-block Telegram notice: Off / 10 / 30 / 60 minutes.
- Scheduler reloads automatically after a schedule edit; no Render restart required.
- `/today` reads the SQL schedule.
- The LLM receives today's SQL schedule as context.

## Existing DB compatibility

No destructive migration is required. SQLAlchemy `create_all()` creates the new `schedule_entries` table automatically.
Existing users, workouts, GTG sets, reflections and memory facts are not deleted.

## Production memory

For durable production data on Render, set `DATABASE_URL` to PostgreSQL. SQLite on a Free Render web service is suitable only for testing because the local filesystem is ephemeral.

Example format:

```env
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE
```

The code also accepts a normal `postgresql://...` URL and converts it to the async SQLAlchemy driver automatically.

## Deploy from the existing feature branch

Copy the update files into your local `jarvis_v2_release` repository, then:

```powershell
git status
git add -A
git commit -m "feat: add editable weekly planner"
git push
```

Render is already tracking `feat/jarvis-v2-miniapp`, so Auto-Deploy should build the new commit. If it does not, use **Manual Deploy → Deploy latest commit**.

After deploy, open Jarvis → **Week**. On first start, the default weekly plan is seeded into SQL. Future edits are stored in the database.
