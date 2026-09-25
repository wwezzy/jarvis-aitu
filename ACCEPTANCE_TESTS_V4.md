# Jarvis v4 staging acceptance

Run on a dedicated staging bot, service, PostgreSQL copy and Redis namespace/database. Do not deploy or modify production. Record commit, environment type, timestamp, observed result and pass/fail for each step. Use synthetic material, not real personal data in screenshots/PR comments. Unperformed credentialed checks remain unperformed; mocks are not live acceptance.

## Automated quality gate

- [ ] Current branch is codex/jarvis-ultimate-v4; PR #9 alone targets feat/jarvis-v3 and is mergeable against its latest commit.
- [ ] `python -m ruff check .` and `python -m pytest -q -ra` pass. No xfails conceal claimed features. Two PostgreSQL-specific tests may skip locally without disposable PostgreSQL; the browser test runs in its dedicated Chromium job or locally with RUN_BROWSER_TESTS=1.
- [ ] GitHub Actions at the exact PR head succeeds for Ubuntu, Windows, PostgreSQL 18 and Chromium browser checks. PostgreSQL runs the full suite, not only DDL compilation.
- [ ] `node --check webapp/static/app.js`, `node --check webapp/static/v4.js`, `node --check webapp/static/deadlines.js` pass.
- [ ] Review the diff for secrets, real LMS URLs, database dumps and env files. None may be added. Verify no production branch/service mutation occurred.

## 1. Migration rehearsal

- [ ] Complete MIGRATION_V4.md backup/restore steps against a staging copy; record baseline counts and representative UTC/local times privately.
- [ ] Run `init_db()` twice. Versions are 1 and 2. Counts, IDs, memory, workouts and done statuses are preserved; the second run does not shift timestamps.
- [ ] Restart staging twice. Existing history/tasks/overrides/preferences persist; no duplicate seeded schedule or delivered notices appear.
- [ ] Rehearse rollback to the backup copy with the former staging commit. Keep production untouched.

## 2. All providers down

- [ ] Remove/disable AI provider keys in staging env and restart. `/today`, “что сейчас”, “что завтра”, `/deadlines`, `/tasks`, `/plan`, `/risk`, `/sleep`, `/stats`, `/cost` and `/diag` answer without an AI request.
- [ ] Create `/task Staging WEB | estimate 90 | course WEB`; it has an unknown deadline. Create another task with an exact future date/time. Complete it with `/done ID`; it remains done after restart.
- [ ] Add a future `/remind DATE TIME | staging reminder`, restart, and receive it once. Repeat with a reminder that became due during a short staging outage: it recovers once within the 24-hour recovery horizon.
- [ ] Edit a recurring block in Week and one occurrence in the dated editor. Today/tomorrow/planner/notifications use the dated change while next week's recurrence is unchanged.
- [ ] Disable Redis separately. Deterministic database queries keep working; `/collect` and PC commands report their unavailable dependency without pretending success.

## 3. LMS correctness

- [ ] Use a staging feed containing Attendance, assignment due, quiz opens, quiz closes and recurring class events. Attendance/class/quiz-open stay out of /deadlines; assignment due and quiz close appear once.
- [ ] Sync twice: no duplicates. Update a deadline: task and reminders change. Mark a task done and sync again: it stays done.
- [ ] Verify explicit teacher override protects that task's local due time; other LMS deadlines remain authoritative.
- [ ] Supply malformed/truncated iCal: existing records are preserved and /diag indicates the failed sync without showing the feed URL.
- [ ] With an explicitly configured authoritative window, remove a pending event for one successful snapshot: it remains. After the configured repeated-absence threshold it cancels; an event outside that window and a done task are preserved.
- [ ] Check TZID, all-day and recurring/exception events display on the expected local day. Unknown class duration is labeled estimated.

## 4. Mixed voice and collection

- [ ] Configure an entitled transcription model and reply model. Send a 3-minute Russian/Kazakh/English voice note including “WEB завтра; OS в воскресенье; SDP вроде на следующей неделе; завтра зал в 11:00”. Reply arrives, extraction uses the transcript, uncertain/date-only deadlines do not gain a made-up clock time, and only tomorrow's unique gym occurrence moves.
- [ ] Say “shutdown pc” in a voice note and include it in an image/PDF: no PC command is queued.
- [ ] `/collect`; send text, a voice note, a valid PDF with a diagram, a DOCX and UTF-8 source code; `/done`. All materials are represented together, visual content is retained for a capable provider, and reply is delivered before save confirmations.
- [ ] Repeat `/collect` during a collection: previous items remain. `/cancel_collect` deletes it. After 15 minutes without additions, expired input is unavailable.
- [ ] Submit 13 items or an over-limit document: explicit rejection, previously accepted items remain. Unsupported/malformed/oversized files never silently disappear from an analysis.
- [ ] Force all reply providers to fail on `/done`: materials remain until TTL/cancel; a retry can reuse them. Add a material during analysis: it is retained.
- [ ] Send a Telegram media album: conservative aggregation includes its images. Independent rapid messages remain independent unless /collect is active.
- [ ] Produce a reply longer than 4096 characters including emoji and angle brackets: complete plain text arrives in ordered chunks without Telegram formatting errors.

## 5. Planning, study, memory and training

- [ ] Give tasks estimates/progress/consequence/course weights. /risk explains deadline, importance, remaining effort/free capacity and uncertainty; /plan respects classes, commute/preparation, gym, meals/rest, sleep and a free-capacity reserve. Overload is explicit.
- [ ] “Сегодня ничего не сделал, перепланируй” and “у меня 90 минут” propose only future feasible work. A planned or elapsed block is never marked completed automatically.
- [ ] Move tomorrow's first class; /sleep recalculates wake/bedtime using preparation/commute/breakfast/shower and a sleep-duration target, without a guaranteed 90-minute-cycle claim.
- [ ] Analyze collected assignment requirements; request explanation, defense practice and quiz. Unknown information is acknowledged and grounded in supplied material. Save a topic, run /study timer, explicitly stop with actual minutes, record a 0..5 self-assessment; next review is visible without claiming mastery.
- [ ] `/memory remember staging_fact original`, correct it, restart and retrieve it; delete it and verify it no longer appears. Expired facts are excluded from retrieval. DB failure cannot produce a false saved confirmation.
- [ ] Log performed sets with weights/reps/RIR/technique; progression holds/reduces for bad form or inadequate/unknown RIR. Asking for a workout plan alone does not log performed sets. GTG day/week totals and legacy reflection/workout views remain valid.

## 6. Notification behavior

- [ ] Class and gym notices use the configured lead (default relevant blocks 60 minutes); assignments use 24h/3h and high-importance/large work gets 3 days. Flexible blocks remain silent.
- [ ] Enter DND (night/class/gym/deep-work): low-priority notices queue. Exit DND: still-relevant notices drain at no more than three per minute; expired/changed/done-task notices cancel. Critical high-importance 3h deadlines can bypass DND.
- [ ] Restart with queued notices: they survive and send at most once. Simulate an ambiguous Telegram send: it remains uncertain in diagnostics and is not automatically replayed.
- [ ] Use Done/Snooze/Reschedule/Mute buttons; verify ownership, persistence and subsequent behavior. Snooze adds one user-authorized delivery; reschedule points to the editor; muting suppresses that kind.
- [ ] Disable/enable morning and evening briefs and quiz-open notices in Settings. Quiz-open is off by default, low-priority, and never creates an assignment deadline.

## 7. Windows agent

- [ ] Configure a staging agent secret and staging Redis. Install with scripts/install-agent.ps1; verify the current-user logon task uses limited privileges and pythonw. A second agent process is rejected.
- [ ] /pc status shows a signed heartbeat and online state. Stop the agent: after 45 seconds status is offline with last-seen/result evidence.
- [ ] /pc lock: a signed command is atomically consumed once, ACK appears and the PC locks. Unlock manually.
- [ ] /pc sleep: accepted ACK appears before sleep; wake manually and observe completion/heartbeat recovery.
- [ ] /pc shutdown and /pc restart: no command executes until the one-use same-user/same-chat confirmation arrives within 60 seconds. Perform these tests only on the designated staging PC when ready for power loss; observe `scheduled`, then later heartbeat after restart.
- [ ] Expired, tampered, wrong-user and replayed payloads are rejected across agent restarts. Ordinary AI prose/transcripts cannot enqueue a command. A pending command is not overwritten.
- [ ] Disconnect Redis, reconnect, and verify 2–60 second backoff and recovery with no raw credentials in rotating logs. Remove startup with scripts/uninstall-agent.ps1; replay state remains intact.

## 8. Mini App and optional adapters

- [ ] On a narrow mobile viewport and desktop, use Today, Week/dated changes, task/source/course/status filters, task Done/edit, risk, workout/GTG, memory edit/delete, Settings and Diagnostics/LMS sync. All edits persist after refresh and restart.
- [ ] Missing, tampered, expired and wrong-user Telegram initData returns 401/403 on every private API. MINIAPP_DEV_MODE cannot bypass auth. Responses expose no credentials or raw LMS errors; expensive sync/diagnostic/AI operations are rate-limited.
- [ ] Google Calendar disabled/unconfigured: startup/local schedule remains healthy. With test OAuth configured, read actual events and detect conflicts; create a Jarvis event and update it; updating an unrelated event is rejected. Disable write flag and verify all writes stop. Simulate failed snapshot and verify prior events remain.
- [ ] Research disabled/unconfigured: clear unavailable result. With Tavily configured, /research a freshness-sensitive public query returns actual source URLs. Personal schedule/LMS/memory queries do not trigger public search; provider failure does not invent results.

## 9. Telemetry and observability

- [ ] Compare a provider response's reported token usage with the SQL ledger and /cost. Missing usage is marked estimated/unknown; missing model prices yield unpriced requests, not a fabricated price.
- [ ] Set explicit test pricing and a small budget. Cross 80%: /cost shows the warning and at most one daily budget notice is issued (DND still applies).
- [ ] Force timeout/429/malformed/empty responses: fallbacks, key/model cooldowns, latency and safe error classes are observable. Deterministic commands add zero provider requests.
- [ ] /diag includes DB/Redis/LMS/scheduler/models/transcription/PC/version/commit/delivery states. API responses have correlation IDs; application logs contain no prompts, transcripts, API keys, tokens, LMS URLs or raw provider exception bodies.

Acceptance is complete only when automated gates pass and the credentialed staging rows have recorded results. Any unavailable account/device check must remain explicitly unperformed; it must not be relabeled as passed.
