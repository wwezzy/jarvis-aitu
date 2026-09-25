# Jarvis Ultimate v4

Private Telegram personal assistant and authenticated Mini App. Python 3.12, aiogram 3, SQLAlchemy asyncio, PostgreSQL/SQLite, APScheduler, optional Redis, OpenAI/NVIDIA/Gemini, and an optional Windows agent.

Issue #8 and its audit comment define this implementation. Work is on `codex/jarvis-ultimate-v4`; PR #9 targets `feat/jarvis-v3`. This work does not deploy or modify the production service or `feat/jarvis-v2-miniapp`.

## Start and validate

1. Create a Python 3.12 virtual environment and install `requirements.txt` (or `requirements-dev.txt` for tests).
2. Set environment variables through your process/service configuration. Never commit credentials, local profiles, database dumps or LMS feed URLs.
3. Read [MIGRATION_V4.md](MIGRATION_V4.md) before pointing this version at an existing database.
4. Run `python aihelper.py`. Startup upgrades the schema, restores scheduling, starts aiohttp and Telegram polling. Use one polling process for each bot token.
5. Open the staging bot in Telegram and press `/start`. Set `WEBAPP_URL` to that staging service's HTTPS origin for its Mini App menu.

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q -ra
```

The suite blocks external HTTP and uses synthetic inputs. CI runs Ubuntu and Windows with SQLite, and the full suite with isolated schemas on disposable PostgreSQL 18. Local PostgreSQL-only tests skip if `TEST_POSTGRES_URL` is absent. The dedicated browser job runs Chromium editing/layout checks; enable `RUN_BROWSER_TESTS=1` locally after installing a Playwright browser (or set `TEST_BROWSER_EXECUTABLE`). No feature is accepted by an xfail. See [ACCEPTANCE_TESTS_V4.md](ACCEPTANCE_TESTS_V4.md) for exact staging checks and evidence requirements.

## Architecture

```text
Telegram (ADMIN_ID only) / Mini App (signed Telegram initData on every API)
  -> deterministic commands and SQL services
  -> bounded inbox / voice transcription / document normalization
  -> durable SQL context + bounded optional Redis history
  -> capability-aware OpenAI -> NVIDIA text -> Gemini key/model pool
  -> plain reply delivered in Telegram-sized chunks
  -> separate validated action extraction -> SQL writes -> save confirmations

SQL: recurring schedule + dated overrides + normalized LMS + unified tasks
     memories + study sessions + workouts/GTG + preferences + usage ledger
     persistent notification outbox and deduplication claims
APScheduler: rebuildable triggers -> outbox -> relevance/DND/expiry checks
Redis: temporary collections, recent history, signed PC commands/status
Windows agent: explicit typed intent -> confirmation if destructive -> HMAC
               -> atomic consume -> persistent replay claim -> ACK -> action/result
```

Personal facts come from the database. Public web results are labeled external sources and never authorize actions. Model output and voice transcripts never authorize PC commands. If all AI providers are down, `/today`, schedule queries, `/tasks`, `/task`, `/done ID`, `/deadlines`, `/remind`, `/plan`, `/risk`, `/sleep`, `/stats`, workouts, GTG, `/cost`, `/diag` and PC text commands still work. Redis failure also leaves database-backed deterministic commands available. Transcription itself requires its configured provider.

Important modules: `services/lms.py` classifies and reconciles full snapshots; `tasks.py`, `schedule.py`, `planner.py` implement deterministic planning; `notifications.py` owns delivery state; `inbox.py` normalizes and collects materials; `llm.py` routes bounded calls; `telemetry.py` records safe usage; `calendar.py` and `research.py` implement optional external adapters; `webapp/v4.py` provides authenticated editing; `database/migrations.py` owns versioned upgrades.

## Environment variables

Only `BOT_TOKEN` and positive numeric `ADMIN_ID` are mandatory to start. SQLite is the default local database. PostgreSQL with a backed-up staging database is recommended for persistent hosting. Keep secrets solely in environment variables.

| Variable | Purpose/default |
| --- | --- |
| `BOT_TOKEN`, `ADMIN_ID` | Staging Telegram bot secret and sole authorized user |
| `DATABASE_URL` | PostgreSQL URL (postgres/postgresql normalized to asyncpg), or persistent SQLite path; default local `jarvis.db` |
| `TIMEZONE` | Display/input timezone; default `Asia/Almaty`; stored timestamps are UTC |
| `HOST`, `PORT`, `WEBAPP_URL` | Listen address/port (0.0.0.0/8080); optional HTTPS Mini App origin |
| `MINIAPP_AUTH_MAX_AGE` | Signed initData age limit in seconds, default 86400; `MINIAPP_DEV_MODE` no longer bypasses auth |
| `ENABLE_MASTER_SCHEDULE` | Recurring/dated block notices and daily briefs, default true; saved reminders and deadlines remain active |
| `LOG_LEVEL`, `APP_COMMIT` | Logging level; optional deployed commit for diagnostics (`RENDER_GIT_COMMIT` also recognized) |
| `REDIS_URL` | Preferred standard `redis://` or TLS `rediss://` connection |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | Legacy REST fallback, only when REDIS_URL is absent |
| `OPENAI_API_KEY`, `OPENAI_DEFAULT_MODEL` | OpenAI credentials and a model enabled for the account; no guessed default model |
| `OPENAI_PLANNER_MODEL`, `OPENAI_PREMIUM_MODEL` | Optional tiers; fall back to configured lower tier |
| `OPENAI_TRANSCRIBE_MODEL` | Required model for voice; independent of reply model |
| `OPENAI_REASONING_EFFORT`, `LLM_TIMEOUT_SECONDS` | Effort default low; per-call timeout default 35 seconds |
| `NVIDIA_API_KEY`, `NVIDIA_MODEL`, `NVIDIA_BASE_URL` | Optional text fallback; default endpoint is NVIDIA's OpenAI-compatible API |
| `GEMINI_API_KEY`, `GEMINI_API_KEYS`, `GEMINI_API_KEY_1` ... `_10` | Optional deduplicated key pool; packed variable is comma-separated |
| `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` | Explicit primary/optional fallback model, with key/model cooldowns |
| `AI_PRICE_JSON` | Operator-supplied prices per million tokens: map `provider:model` to numeric `input`/`output` USD rates. No built-in price assumptions |
| `AI_DAILY_BUDGET_USD` | Optional daily warning threshold; warn once per local day at 80% of configured-price estimate |
| `LMS_ICAL_URL`, `LMS_SYNC_MINUTES` | Personal feed held only in env; default interval 15 minutes, minimum 5 |
| `LMS_WINDOW_PAST_DAYS`, `LMS_WINDOW_FUTURE_DAYS` | Explicit authoritative disappearance window; absent/zero means no missing-item cancellation |
| `LMS_MISSING_SYNC_THRESHOLD` | Consecutive successful missing snapshots before cancellation; default 3, minimum 2 |
| `LEGACY_DATA_TIMEZONE`, `LEGACY_SERVER_TIMEZONE` | First migration only; see migration guide |
| `JARVIS_PROFILE_FILE` | Optional ignored private profile path; default profile.local.md; empty disables |
| `ENABLE_SEMANTIC_MEMORY`, `OPENAI_EMBEDDING_MODEL` | Optional pgvector ranking; default disabled, explicit embedding model and installed vector extension required |
| `PC_AGENT_SECRET` | Shared HMAC secret in bot and agent env; use a random high-entropy value |
| `ENABLE_GOOGLE_CALENDAR`, `ENABLE_GOOGLE_CALENDAR_WRITE` | Optional read integration and separate write gate; both default disabled |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CALENDAR_ID` | OAuth setup for selected calendar; all env-only |
| `ENABLE_WEB_RESEARCH`, `WEB_RESEARCH_API_KEY` | Optional real Tavily search; disabled by default |
| `RUN_BROWSER_TESTS`, `TEST_BROWSER_EXECUTABLE` | Test-only browser opt-in and optional installed Chromium executable |
| `TEST_POSTGRES_URL` | Test-only disposable loopback database named jarvis_audit_test; never a staging/production URL |

## Commands and workflows

- `/task WEB assignment | due 2030-01-08 18:00 | estimate 90 | course WEB`; omit `due` if uncertain. `/tasks`, `/done ID`, `/plan`, `/plan 90`, `/risk`, `/sleep` are deterministic. Risk components and uncertainty are visible in the Mini App.
- `/schedule 0 | 14:00 | 15:00 | WEB | fixed` adds a recurring Monday block. Week edits recurring entries. The dated editor or `/override DATE | ID | START | END` changes one occurrence; `cancel` cancels only that date. Natural text such as “завтра зал в 11:00” changes only a uniquely identified existing gym block.
- `/collect`, then text/voice/images/documents, then `/done`. `/done ID` completes a task. `/cancel_collect` deletes temporary collected input. The collection lasts 15 minutes, with at most 12 messages, 12 MB serialized material and a bounded text budget. Repeating `/collect` preserves its contents. A failed provider call retains the batch until TTL/cancellation. Inputs arriving during analysis are retained.
- Telegram media albums aggregate conservatively. Unrelated rapid messages are independent; use explicit collection when combining text, voice and separate documents. Materials are labeled. A fallback cannot silently ignore native images/PDFs.
- PDF (up to 100 pages), DOCX, UTF-8 TXT/MD/JSON/code, XLSX/XLS and images are normalized. Legacy DOC is sent only through a compatible native-file provider. Archives/executables, encrypted PDFs, oversized/decompression-heavy or malformed inputs are rejected with a safe error. PDF originals accompany extracted text to retain diagrams/scans. No permanent audio is stored; OGG conversion uses a temporary directory, finite timeout and restricted ffmpeg protocols.
- `/study topic COURSE | TOPIC | notes`, `/study timer 25 TOPIC`, `/study stop ID MINUTES`, `/study review ID 0..5` record explicit study evidence. `/study analyze`, `/study defense`, `/study explain`, `/study quiz` use document/context-grounded AI help. Self-assessment and time spent never become automatic mastery claims.
- `/memory`, `/memory remember KEY VALUE`, `/memory correct ID VALUE`, `/memory delete ID` expose durable facts (see command help for accepted syntax). Memory correction/delete is also in the Mini App.
- `/gtg`, `/workouts` and existing structured workout/reflection flows remain. Suggested workouts are not automatically logged; progression considers RIR and technique.
- `/cost` reports requests, reported/estimated tokens, unknown usage, configured-price estimates and budget warning. These are estimates, not provider invoices; missing prices are explicitly counted.
- `/diag` shows DB type/health, Redis backend/health, LMS last success, pending tasks, scheduler jobs, provider models/cooldowns/latency, transcription configuration, PC heartbeat/results, version/commit and delivery state counts. Logs use correlation IDs and omit raw messages, prompts, API URLs and exception payloads.

## Windows agent

Set `ADMIN_ID`, `PC_AGENT_SECRET`, and the same standard Redis or Upstash env variables for the interactive Windows account. Use a separate Redis database/credentials and HMAC secret for staging. Install dependencies in the repository's virtual environment. Run `python agent.py` manually first, or register startup with `powershell -File scripts/install-agent.ps1`. The installer creates a current-user, limited-privilege, hidden Python task at logon; it does not embed credentials or auto-run commands. `Start-ScheduledTask JarvisPersonalAgent` starts it; `scripts/uninstall-agent.ps1` stops/removes startup while retaining replay state/logs.

`/pc status` reads a signed heartbeat (online if seen within 45 seconds), last accepted command and result. `/pc lock` and `/pc hibernate` queue an exact allowlisted action. `/pc sleep` is intentionally disabled on this PC. `/pc shutdown` and `/pc restart` require the returned `/pc confirm TOKEN` in the same chat/user within 60 seconds. Commands expire after 90 seconds; a pending command cannot be overwritten. The agent atomically consumes, validates HMAC/time/nonce/user, persists its replay claim, publishes ACK, then executes a fixed API or argument list with `shell=False`. Redis failures use 2–60 second backoff. A local OS file lock prevents duplicate instances. Three rotating 1 MB logs and persistent replay/result files live in LOCALAPPDATA/Jarvis.

An ACK means accepted, not completed. Hibernate/shutdown/restart return `scheduled`; lock returns `completed`. A crash or ambiguous transport result is never automatically replayed. Verify `/pc status` before issuing a new explicit action. OS policy, logged-in session, power settings and device support still require the manual staging checks. Volume/app/URL launching, battery telemetry and Wake-on-LAN are optional extensions and are not implemented.

## LMS, Google Calendar and research setup

Configure the personal LMS feed only in the staging service environment. `/lms_sync` and Mini App Sync LMS run the same reconciler. Attendance, class events and quiz openings stay calendar events; only assignment deadlines and quiz closings become tasks. HTML is sanitized, recurrence expanded with bounds, IDs stable, completed status preserved, and malformed/partial feeds leave existing records unchanged. Disappearance cancellation requires an explicitly authoritative window and repeated successful absence; a single truncated snapshot cannot cancel a task. Manual teacher overrides explicitly protect the deadline from subsequent LMS refreshes.

For Google Calendar, enable the Calendar API in a Google Cloud project, configure OAuth consent and an OAuth client, and grant the staging account access. Obtain a refresh token through Google's documented offline OAuth consent flow; configure it in env. Read-only installations should request `calendar.events.readonly`; writing requires `calendar.events`. Select the calendar ID, then enable the read flag. `/calendar sync` or Settings → Sync calendar imports the bounded previous 7/next 90 day snapshot, including expanded recurrence; an optional 30-minute background sync is registered. A failed full snapshot does not erase local events. Review conflicts in Settings before creating/updating Jarvis events, and enable the separate write flag only when required. Existing unrelated events are never edited. Creation uses an idempotent operation-derived ID; if Google accepted a request but a response was lost, sync to recover it instead of creating another operation.

References: [Calendar events list](https://developers.google.com/workspace/calendar/api/v3/reference/events/list), [events insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert), [offline OAuth](https://developers.google.com/identity/protocols/oauth2/web-server#offline).

For research, configure a Tavily key and enable its flag. `/research latest public Python news` calls the real search API with only the explicit query; results include source URLs and are labeled external excerpts. Personal schedule/LMS/memory queries never route to search, and no DB context is sent. Unconfigured/unavailable providers return an explicit unavailable response. Reference: [Tavily search](https://docs.tavily.com/documentation/api-reference/endpoint/search).

## Limits and operational boundaries

- Live provider accounts, OAuth consent, real LMS data, Windows power behavior and Telegram delivery require credentialed staging acceptance. Offline mocks prove adapter behavior, not account availability or model entitlement.
- The planner is a deterministic heuristic with visible uncertainty, a work cap, rest and free-capacity reserve. It proposes blocks; it does not pretend that elapsed blocks or timers imply completion. Vague natural-language edits require clarification or the explicit editor.
- Semantic memory is optional: pgvector ranking embeds current candidates on demand. Missing credentials/extension/provider fall back to lexical selection. No automatic extension installation or autonomous embedding maintenance occurs.
- Telegram has no idempotent delivery API. Ambiguous sends remain `uncertain` (or `sending` after a process crash) and are visible in diagnostics; automatic replay is avoided. Deferred notices drain at up to three per minute and are checked for expiry/completed tasks/changed schedule.
- The Mini App always requires Telegram initData, including local development. Browser assets are public; all private data and writes require authentication. Rate limits assume one polling process; distributed deployment needs a shared request limiter and scheduler ownership.
- Google writes and research are explicitly initiated; neither is an autonomous background agent deciding to change accounts. Search returns cited excerpts, not a guaranteed exhaustive research report. Provider invoices and search/OAuth service charges are outside token-price estimates.
- Old v2/v3 audit documents describe historical behavior. This README and the v4 migration/acceptance documents describe the current branch.
