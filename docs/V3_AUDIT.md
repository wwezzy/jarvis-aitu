# Jarvis v3 audit — issue #4

Reviewed base: `da36011` on `feat/jarvis-v3`. Work and fixes are confined to
`codex/jarvis-v3-audit`. The base advanced during review; this report and the final
tests cover its OpenAI-first router, response/action split, notification log,
model tiers, sleep guidance and morning/evening briefs.

This is an independent code audit, not production acceptance. No Render service,
remote database, production credentials or production branch were modified.

## Architecture

`aihelper.py` hosts Telegram polling, the aiohttp Mini App and in-process
APScheduler. SQLAlchemy stores users, memory, assignments, workouts, reminders,
schedules and `notification_log`. Redis holds recent conversation and the PC
command mailbox. The handler resolves deterministic requests first, gathers SQL
context for other requests, delivers an AI response and then extracts/applies
structured actions. Action writes are still performed in the same handler task.

The existing model tiers and provider order are preserved: OpenAI Responses,
NVIDIA compatible chat, Gemini pool, deterministic degradation. The existing
`NotificationLog` model is reused; this audit adds no competing table or schema.

## Fixes and regression evidence

| Failure found | Fix / tests |
| --- | --- |
| No AI credentials prevents all startup, including deterministic functions | Allow AI-free configuration; `test_boot_without_any_ai_credentials` |
| Sync SDK calls continue in background threads after asyncio timeout; SDK retries multiply attempts | Cancellable AsyncOpenAI clients, zero implicit retries, client cleanup and bounded provider/extraction budgets; transport/cancellation tests |
| OpenAI silently ignores non-image attachments and answers without their contents | Route audio/documents to Gemini; keep OpenAI image support; attachment routing test |
| Provider/schema errors copy response bodies into logs/diagnostics | Log/store exception types only; payload redaction tests |
| Global legacy Redis history crosses user boundaries; malformed/unbounded history is read | User-scoped, bounded history; filter malformed records and normalize legacy model roles |
| Unrelated high-importance facts always enter prompts | Require a lexical match; memory relevance test |
| UTC/local boundaries produce inconsistent tomorrow/deadline queries | Normalize supplied aware timestamps; deterministic intent and boundary tests |
| Nullable deadline sorting differs between PostgreSQL and SQLite | Explicit NULLS LAST and horizon before LIMIT; query regression |
| LMS exceptions can expose feed credentials | Sanitized logs/state/errors and hidden legacy error text in `/diag`; synthetic failure tests |
| Explicit cancelled iCal events remain pending | Honor STATUS:CANCELLED without reopening completed work |
| Class/training callbacks and reminder/deadline callbacks bypass DND | Shared dispatch helper checks DND before claiming; protected block and overnight tests |
| Sleep DND ends at midnight because seeded sleep rows end at 23:59; overlapping protected blocks can be missed | Baseline 23:30–07:00 quiet window plus all current protected rows; boundary and protected-block tests |
| Class/gym notification opt-out is ignored | Honor null/zero/custom leads, keeping the 60-minute default; lead tests |
| Stale callbacks notify completed/deleted/moved assignments or edited schedule rows | Recheck SQL ownership, status and expected event time before claiming/sending |
| Resync removes another user's jobs | User-scoped dynamic job IDs/removal; isolation test |
| All notification IntegrityErrors are treated as duplicates | Confirm an existing claim before suppressing; invalid identities rejected without truncation |
| Agent GET then DELETE can delete a newer command | Atomic GETDEL transport; consumer test |
| Signed commands replay within the validity window, including across restarts | Durable nonce claim before execution; restart and execution-failure tests |
| Malformed payload types/non-ASCII signatures raise instead of rejecting | Strict types and signature/nonce shape checks; tampering and timestamp tests |
| Model-generated PC output becomes an authenticated signed command | Sign only explicit whole-message intents such as `/pc lock`; ignore model PC output |
| `/diag` fails during database outage and implies configured dependencies are healthy | Bounded SQL probe; explicit unverified Redis/agent status and sanitized failures |
| Windows may lack IANA timezone data | Add tzdata dependency; Windows CI |

## Issue #3 acceptance matrix

| # | Requirement | Automated evidence / remaining gate |
| --- | --- | --- |
| 1 | Reply independent of schema | Real handler delivers before blocked/invalid extraction and retains its reply after action write failure |
| 2 | “Что сейчас?” with AI unavailable | SQL-backed service and handler tests; schedule/next/tomorrow/deadline intents and explicit PC commands bypass AI |
| 3 | Seven-day planning uses schedule + deadlines | SQL context includes seven dates, classes and deadlines, excludes completed/other-user data. Generated plan quality and deterministic priority scoring remain unverified |
| 4 | LMS imports/updates | Synthetic import/update/idempotency, duplicate UID, completion preservation, timezone, explicit cancellation and outage tests. Disappearance reconciliation remains a strict xfail |
| 5 | No recurring 30-minute spam | Existing event-driven job registration, repeated/concurrent durable claims and dispatch tests; existing morning/evening briefs retained |
| 6 | Class/gym warning around 60 minutes | Job registration, actual callback and previous-weekday tests; explicit opt-out/custom leads respected |
| 7 | Completed-task reminders stop | Dispatch-time state checks, including callbacks captured before completion |
| 8 | OpenAI → NVIDIA → Gemini | Failure injection at every provider: quota, timeout, empty/malformed response, cooldown recovery, pool rotation and total outage |
| 9 | PostgreSQL data survives redeploy | PostgreSQL 18 CI creates real tables, checks BIGINT IDs, adds the notification table to a preexisting schema and reconnects via fresh engines. Local tests compile PostgreSQL DDL and verify file persistence. Actual staging redeploy/restore remains a manual gate |
| 10 | PC agent survives Windows reboot | Signature/replay/transport behavior tested. Startup installation, single-instance guard, heartbeat/ack and real reboot acceptance remain open |
| 11 | `/diag` reports all dependencies | Provider configuration/cooldowns, bounded database probe and LMS state tested. Redis probe and actual agent heartbeat remain unimplemented and explicitly unverified |

## Remaining failure modes and release blockers

1. **P1 — No versioned migrations.** `create_all` creates missing tables but does
   not migrate existing columns, constraints or data. The existing notification
   table is additive and its creation is tested. Adopt a versioned baseline,
   reviewed upgrades, a migration lock and backup/restore rehearsal before
   production schema evolution. No remote migration was run by this audit.
2. **P1 — Hosting durability is not proven by ORM tests.** Ephemeral SQLite loses
   state on redeploy. Issue #3's infrastructure note describes a staging database
   expiring on 2026-10-24; the note is not proof of permanent/current durability.
   Verify the actual staging setup and a backup restore independently.
3. **P1 — Windows lifecycle is incomplete.** Setup installs dependencies and
   `run_jarvis.ps1` starts the bot, not the agent. No startup installer/uninstaller,
   single-instance guard, rotating local log, heartbeat or execution ack exists.
   Reconnect now backs off to 60 seconds but has no jitter. The single-slot Redis
   mailbox can overwrite an earlier command. Preserve/protect the local replay
   database; do not infer reboot acceptance from a replay-store test.
4. **P1 — No notification recovery/outbox.** DND suppresses date jobs without
   deferral; past-due reminders are not restored on startup. Notification claims
   are committed before sending, so uncertain delivery/crashes can lose notices.
   Add durable dispatch states, expiry/catch-up and manual recovery. The current
   `sent_at` field represents claim time, not confirmed Telegram delivery.
5. **P1 — LMS disappearance needs a completeness contract.** Missing UIDs can
   mean deletion, feed truncation or a changed window. Explicit cancellation is
   handled; deletion-by-disappearance is a strict expected failure. Recurrence
   expansion, stable identity for UID-less events, sync locking, download size
   limits and transactional sync metadata also remain open.
6. **P2 — Actions lack idempotency and transactional execution.** Replies survive
   failures, but earlier writes can commit before later actions fail. Retried
   Telegram updates can duplicate workouts/reminders; process termination after
   reply delivery loses extraction. Introduce update-ID idempotency and persisted
   action states. Attachment content is not passed to the extractor, so voice/
   document actions are not independently verified from the source content.
7. **P2 — Provider limits and policy.** Budgets now bound each provider and the
   full extraction phase. A large Gemini pool may not be fully attempted within
   the budget. Cooldowns are per process/provider, with no exclusive half-open
   probe or per-key backoff. Extraction uses OpenAI then Gemini, without NVIDIA.
   The legacy GEMINI_FALLBACK_MODEL setting is still not used by the v3 base.
   Model-tier heuristics are preserved and tested, but live model availability,
   quota and cost behavior require staging validation.
8. **P2 — Risk/planning policy remains heuristic.** Existing 3d/24h/3h warnings
   classify large tasks from title keywords; normal tasks use 24h/3h. There is no
   deterministic score using progress, available time, effort and consequence,
   no adaptive rescheduling and no durable user quiet-time preferences. The
   dispatch primitive supports critical bypass; automatic notices currently use
   normal priority. A sleep window based on defaults may not fit the user's day.
9. **P2 — Data and operational consistency.** Missing distinct `lms_events`,
   `user_preferences` and `planner_state` tables. Datetimes mix naive local values
   with DB-server timestamp defaults. Specify UTC/storage conversions through a
   migration rather than reinterpreting old rows. `/health` is liveness only;
   Redis health is configuration-only, logs are plain text, attachments lack size
   limits, and concurrent Redis chat updates can overwrite one another.

## Delivery and persistence semantics

The existing SQL `notification_log` unique constraint on `(user_id, event_key)`
prevents duplicate claims across workers and restarts. A claim is committed before
sending. If delivery times out, the claim remains because Telegram may already
have accepted the message. This is at-most-once dispatch, **not exactly-once or
guaranteed delivery**. DND is checked before claiming, so an explicit later retry
outside DND can still send. Database failures fail closed.

PostgreSQL DDL tests cover every table/index; the integration test uses a disposable
loopback PostgreSQL 18 database and independent engine recreation. Telegram IDs
are BIGINT. PostgreSQL URL aliases select asyncpg. No production connection
settings are used. Avoid a destructive notification-table downgrade that deletes
claims and permits duplicate sends.

## Running and interpreting tests

```text
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest -q -ra
```

Fixtures disable dotenv loading, overwrite bot/database settings, remove inherited
provider/Redis/LMS/PC credentials and block external HTTP/network. Test data is
synthetic. No test starts Telegram polling, sends a real PC command or contacts LMS.
One strict xfail tracks disappearance reconciliation; an unexpected pass fails CI
so the marker must be reviewed when implementation lands. PostgreSQL skips locally
without its dedicated disposable-test setting; CI supplies it.

CI runs Linux and Windows plus PostgreSQL 18, with read-only repository permissions,
no deployment step and no production secrets. It triggers only for the audit
branch and PRs into `feat/jarvis-v3`.

## Manual staging gates (not performed)

- On a separate staging bot and durable database, rehearse migrations, seed
  synthetic state, redeploy and restore a backup. Confirm memory, assignments
  and notification claims survive.
- Inject controlled provider/network outages using the actual configured models;
  verify latency bounds, recovery and accurate diagnostics.
- On a test Windows machine, install the future startup mechanism, reboot and
  confirm one agent, heartbeat, reconnect and signed-command ack. Validate lock
  in a supervised session before any power-command tests.
- Validate notification catch-up, scoring/adaptive rescheduling and LMS deletion
  handling once implemented. Keep production release blocked until those gates
  and the unresolved P1 findings are addressed.
