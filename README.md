# Jarvis AITU v2

Private Telegram productivity + training assistant built on **aiogram 3**, **Gemini**, **SQLAlchemy**, **APScheduler**, Redis and a Telegram Mini App.

## What changed in v2

- Short-term chat context and long-term memory are separated.
- Redis stores only a small recent dialogue window; durable facts live in SQL.
- Workouts are stored as `WorkoutSession -> ExerciseSet`, including weight, reps, RIR and technique status.
- Deterministic double-progression recommendations are computed in Python.
- GTG pull-up sets are stored and summarized by day/week.
- Daily reflection is persisted in SQL.
- APScheduler restores unsent one-off reminders after restart and runs the master schedule.
- Telegram Mini App provides a dashboard, quick GTG logging, structured workout logging, reflection and memory view.
- Mini App API validates Telegram `initData` on the server before accepting writes.
- LLM output uses a typed Pydantic schema instead of regex extraction.
- Generic fake "data corrupted" failures were removed; server logs now carry real exceptions with a short debug id shown to the user.
- Raw LLM replies are not parsed as Telegram Markdown, eliminating a class of Telegram formatting failures.
- Windows agent secrets are loaded from environment variables and the command key is scoped to the configured admin id.

## Architecture

```text
Telegram user
   |
   +--> aiogram handlers -----------------------------+
   |                                                  |
   |                                       +----------v-----------+
   |                                       | Context builder       |
   |                                       | recent Redis chat     |
   |                                       | + durable SQL facts   |
   |                                       | + workout history     |
   |                                       +----------+-----------+
   |                                                  |
   |                                             Gemini API
   |                                                  |
   |                                       typed JarvisResponse
   |                                                  |
   +--> SQLAlchemy repositories/services <------------+
   |
   +--> APScheduler reminders / protocol notifications
   |
   +--> Telegram Mini App (aiohttp)
           |
           +--> initData HMAC validation
           +--> dashboard / GTG / workout / reflection APIs
```

## Project layout

```text
jarvis-aitu/
├── aihelper.py                 # application bootstrap
├── agent.py                    # optional Windows PC agent
├── config.py                   # env-backed settings
├── database/
│   ├── engine.py
│   └── models.py
├── handlers/
│   ├── assistant.py
│   ├── gtg.py
│   ├── memory.py
│   ├── start.py
│   └── workouts.py
├── prompts/
│   └── jarvis_system.md
├── services/
│   ├── gtg.py
│   ├── llm.py
│   ├── memory.py
│   ├── reflections.py
│   ├── schedule.py
│   ├── scheduler.py
│   ├── schemas.py
│   ├── telegram_auth.py
│   ├── users.py
│   └── workouts.py
├── webapp/
│   ├── server.py
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── styles.css
├── tests/
├── .env.example
├── profile.example.md
├── Dockerfile
└── requirements.txt
```

## Local setup

1. Create a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill the real credentials.
4. Copy `profile.example.md` to `profile.local.md` and put any private Jarvis context there.
5. On Windows you can also run `setup_windows.ps1` and `run_jarvis.ps1`.
6. Start:

```bash
python aihelper.py
```

The Mini App backend listens on `PORT` and `/health` returns service health.

## Mini App setup

Telegram requires a public HTTPS URL for a production Web App. Deploy the bot/backend, set:

```env
WEBAPP_URL=https://your-public-domain.example
```

On startup Jarvis configures a private-chat menu button automatically. You can additionally set the bot's **Main Mini App** in BotFather if you want the prominent `Launch app` entry on the bot profile.

Do not enable `MINIAPP_DEV_MODE=true` in production. It intentionally bypasses Telegram Mini App authentication for local browser UI testing.

## Memory model

### Short-term

Redis key:

```text
jarvis:chat_history:<telegram_user_id>
```

Only the most recent 24 messages are kept. This is conversation continuity, not long-term memory.

### Long-term

`memory_facts`, structured workouts, GTG sets and daily reflections live in SQL. The context builder selects important facts, the newest workout sessions and query-relevant older sessions before each LLM call.

That means increasing a chat window from 50 to 500 messages is no longer necessary for remembering a gym session.

## Workout schema

A workout now has real sets:

```text
WorkoutSession
  id
  user_id
  title
  started_at
  notes
  source

ExerciseSet
  workout_session_id
  exercise_name
  set_number
  weight_kg
  reps
  rir
  technique_ok
```

Example user message:

```text
Сегодня incline DB press: 26 кг — 10, 9, 8; RIR 2.
```

The LLM can extract three `ExerciseSet` rows. Later Jarvis retrieves them from SQL rather than hoping the sentence is still inside Redis history.

## Existing SQLite database

The old `workouts` table is kept for compatibility. `Base.metadata.create_all()` only creates missing tables; it does not delete old data.

New tables include:

- `workout_sessions`
- `exercise_sets`
- `memory_facts`
- `gtg_sets`
- `daily_reflections`

For a larger multi-user/product deployment, add Alembic migrations before changing existing table columns.

## Security checklist

- Never commit `.env`, `profile.local.md` or `jarvis.db`.
- Rotate the Upstash token that appeared in earlier public repository history.
- Consider rotating any other secret that has ever been committed.
- Keep `ADMIN_ID` restriction if Jarvis remains a private bot.
- Mini App writes require validated Telegram `initData`.
- Use PostgreSQL in production if the deployment filesystem is ephemeral.

## Telegram commands

```text
/start
/today
/gtg
/workouts
/memory
/app
```

Normal text, photos, documents and voice messages go through the Jarvis LLM pipeline.
