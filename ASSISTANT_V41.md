# Active assistant: behavior, evidence and acceptance

This follow-up stays on `codex/jarvis-ultimate-v4`. PR #9 already merged. No new PR, production deployment or edits to another checkout are performed. The staging Key Value IP allowlist was updated only after explicit operator confirmation.

## Initiative and feedback

Jarvis now reads editable goals and actual constraints, chooses one next step, protects a user-started focus interval and reviews explicit outcomes. The loop never calls a model, operates the PC or changes an external account. It works with every LLM unavailable.

Three starter proposals appear once: movement, learning and organizing sources. They are editable proposals, not inferred personal facts or a training prescription. Disabled goals are not resurrected. Frequencies and minimum durations are adjustable heuristics.

- `/autopilot active` (requested default): up to five opportunities at 09/11/14/17/21, evaluated every five minutes in the first half-hour in TIMEZONE. Balanced uses 09/14/21; quiet 09/21; off disables initiative. Existing briefs/deadlines have separate preferences.
- `/autopilot pause 1` reserves one hour of rest (up to 168); resume clears pause. DND, fixed classes, training, protected rest, focus and recent replies suppress unnecessary prompts. Two unanswered recent messages reduce middle-of-day prompts. Unknown is not failure.
- `/next` prioritizes imminent task risk, then remaining weekly goals and explicit priority. `/bored`, “мне скучно”, “не могу начать” provide a small step plus rest/alternative options.
- `/goals` reports explicit weekly evidence. `/goal add НАЗВАНИЕ | ШАГ | МИНУТЫ | ДНЕЙ | СИГНАЛ | learning` creates a goal; categories: learning/movement/organization/other. `/goal edit ID | ...` edits; `/goal archive ID` disables.
- `/checkin ID done МИНУТЫ` or `/checkin ID skipped 0 ПРИЧИНА`: one outcome per goal/local date. Repeated taps cannot inflate days; skipping does not erase past progress.
- `/focus 25 goal ID`, `/focus 25 task ID`, `/focus 25 ОДИН ШАГ`: one interval, rejecting protected-block conflicts and foreign records. `/focus stop МИНУТЫ` records reported time, never automatic goal/task completion. `/focus cancel` cancels. Expiry asks for feedback; after eight hours an abandoned interval releases as unreported.

Initiative has authenticated Start / Another option / Rest / Explicit minimum buttons. Sunday evening uses a weekly review. Mini App has corresponding goals/focus editors. Ranking/timing/backoff are engineering heuristics, not psychological measurements.

## Source organization

`/capture ТЕКСТ` stores original source text; `/inbox` lists unclassified records; `/find QUERY` retrieves literal matches with note IDs. `/note ID reference|action|archived|inbox|delete` classifies/deletes. Classification does not invent tasks/deadlines; create the actual task explicitly. Mini App supports capture/search/classification/delete and displays HTML literally.

`auto_capture_materials=true` saves normalized document/collection text and voice transcripts of at least 200 characters after the reply. Up to 120,000 characters split into notes of at most 20,000 without silent truncation. Native bytes/audio are not stored. Image-only inputs without text are not claimed as searchable image content. Disable auto-capture in Settings; remove existing notes in the editor.

Source notes are untrusted data, cited by ID, and cannot authorize tools. Known credential/configuration patterns are rejected; this is not a universal secret detector. Never submit credentials as assistant materials.

## PC failure and updated transport

The real local probe on 2026-10-06 found an IP allowlist rejection. Redis cannot deliver commands while the laptop is blocked. The running agent came from `C:/Users/alnbu/jarvis-aitu/agent.py`; no file in that other checkout was edited. Two pythonw entries may be a venv launcher and child, not two instances.

On the same date, the authorized narrow IP rule restored TLS Redis access. The local updater replaced that runtime with this checkout's agent, preserved its existing credentials and enabled category observation. The limited interactive `JarvisPersonalAgent` task was verified running, with a signed version-4.1 heartbeat. A live diagnostic `status` command returned a nonce-correlated `completed` receipt, and local minute buckets were present. No live power action was performed. These checks do not substitute for deploying the server changes or completing Telegram acceptance.

Original typed requests now run before optional collection state. Exact “выключить”, “выключить ноутбук”, “Джарвис, выключи компьютер” and `/pc` commands are understood. Voice, documents and model output never authorize execution.

The October 10 chat regression is covered by exact aliases: `/pclock`, `/pcstatus`, `/pcshutdown`, `/pcrestart`, `/pchibernate`, `/pccancel` and the observed “джарсис выключи ноут”. PC requests route before SQL and AI. Forwarded messages are rejected. Mixed `/pclock выключи` is a conflict, answered deterministically with clarification. Shutdown/restart provide authenticated Confirm/Cancel buttons referencing the same one-use user/chat challenge; stale cancellation never claims an already-consumed command was stopped. `/pc status` gives a readable signed status without raw transport data.

Attendance is retained as LMS source data and a fixed calendar constraint. When its exact start matches one unambiguous fixed study class, the resolved schedule links it to that class and sends one class notice. Unmatched generic Attendance does not push by default; `lms_attendance_notices` in authenticated Settings opts in. Distinct parallel classes remain distinct; no course/group is guessed. Previously queued duplicate/disabled notices are checked against the current resolved schedule before delivery.

Before queueing, a fresh signed heartbeat is required. Shutdown/restart require a one-use 60-second same-user/chat confirmation. Pending commands cannot be overwritten; queue TTL is 20 seconds. Signed nonce-correlated ACK/results persist locally and separately for 24 hours. The bot waits six seconds and distinguishes no ACK, accepted, completed, Windows scheduled and failed. Ambiguity is not automatically retried. Shutdown/restart have a 30-second cancellation interval: `/pc cancel_shutdown`. No `/f` is used; unsaved apps may prevent shutdown. Hibernate remains the alternative to deliberately disabled sleep.

Read-only diagnosis: `.venv\Scripts\python.exe agent.py --doctor`. It reports presence, health and fixed reason labels, never secrets. `ip_not_allowed` requires authorized operator review of the intended Render Key Value Networking → IP allow list. Allow the laptop's current public IP with the narrow intended CIDR, preserving existing rules. The laptop uses the external TLS URL; the bot may use that instance's internal URL. No network rules/credentials are changed automatically. [Render external connections](https://render.com/docs/key-value#enabling-external-connections), [Windows shutdown semantics](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/shutdown).

To update locally: stop only the identified old agent/task, retain existing `LOCALAPPDATA/Jarvis/agent.env`, install this checkout's dependencies, run doctor, register `scripts/install-agent.ps1` with this checkout's Python and start its current-user limited interactive task. Do not regenerate secrets just to upgrade code. Uninstall stops/removes only that task and retains replay/log data. Automated tests do not perform live power actions.

`scripts/update-agent.ps1 -PreviousAgentPath ABSOLUTE_IDENTIFIED_OLD_AGENT_PATH -EnableMonitoring` automates this local upgrade while preserving credentials. It refuses an unhealthy connection. The explicit `-AllowOfflineMonitoring` switch permits local category collection while transport remains blocked; it never claims the PC command path is fixed. It stops only the named task and processes running the exact provided script.

## Authorized process-time observation

Enable both controls: local `PC_MONITOR_ENABLED=1` in existing `LOCALAPPDATA/Jarvis/agent.env`, and authenticated Settings activity receiving or `/activity on`. No new API key/dependency is needed; controls can be disabled separately.

Native Windows sampling reads the foreground executable basename and session idle time every two seconds, immediately classifying work/gaming/media/other/idle/unknown. It reads no titles, URLs, screenshots, keystrokes, full process lists or command lines. Background games are excluded. Browsers remain unknown: they cannot distinguish work from anime. Five minutes without input is idle; passive reading/video/controllers may be undercounted. Observation is not proof of productivity.

Optional `PC_ACTIVITY_RULES_JSON` overrides exact executable basenames, e.g. `{"mygame.exe":"gaming","myeditor.exe":"work"}`. Unknown apps stay unknown; labels are deterministic/editable.

An independent thread samples during Redis reconnect backoff. SQLite spools minute category durations for at most seven days; sleep/restart/long gaps and clock reversal are excluded. Closed buckets upload signed bounded batches, are acknowledged after SQL commit and deduplicated after restart. Server history also retains seven days. Tampered/stale/future/foreign/impossible data are rejected. `/activity off` stops receiving; `/activity delete` deletes server history. Stop the agent, run `agent.py --clear-activity`, disable sampling and restart to clear/stop local collection.

Reports show coverage, not a complete day. `gaming_budget_minutes` (default 60) is an editable intent, not a scientific threshold. Active initiative compares observed gaming with it. Two observed gaming minutes during a chosen focus can trigger one neutral prompt per session, capped at five/day; it passes its own focus DND but respects class/sleep/training DND. No process is automatically closed.

## Scientific basis and limits

Research informs design, not a guarantee that this app creates discipline:
- [Harkin et al., 2016](https://eprints.whiterose.ac.uk/id/eprint/91437/) supports explicit progress recording. Jarvis separates observed time, reported effort, unknown outcomes and explicit completion.
- [Lally et al., 2010](https://onlinelibrary.wiley.com/doi/full/10.1002/ejsp.674) supports consistent-context repetition with variable habit-development times. Cues are editable; there is no guaranteed “21-day” transformation.
- [Milkman et al., 2021](https://www.nature.com/articles/s41586-021-04128-4) found varied effects and limited persistence. Weekly review and returning after a lapse are preferable design choices to shame or claims of permanent motivation.

Default durations, ranking, timing, backoff and labels are heuristics. Compare explicit weekly outcomes, adjust one variable at a time and preserve sleep/recovery/chosen recreation. A fictional ideal is not a measurable completion claim.

## Migration and exact staging acceptance

Version 3 adds assistant_goals/checkins/state/focus/notes/activity_receipts. Versions 1/2 and existing UTC remain unchanged. Back up/rehearse using MIGRATION_V4.md, run init_db twice, verify versions 1/2/3 and old counts/statuses/timestamps. Roll back to a pre-v3 backup; older code intentionally rejects a newer schema.

1. Ruff, full pytest, all JS syntax checks, Windows/Ubuntu/PostgreSQL 18 CI, actual browser job.
2. Disable all models; test new commands and Mini App editing, with Redis unavailable too.
3. Seed/restart twice; edit/archive goals; repeat check-in: no reset/resurrection, one completed day.
4. Test mode/pause/DND/conflicts/recent-reply suppression/backoff; opt-out cancels queued initiative.
5. Restart during focus, expire/report; no automatic completion. After eight hours release as unreported.
6. Capture/search/classify/delete text/document/long voice; multipart preservation, literal HTML, auto-capture off, rejected secret patterns and source instructions unable to operate tools.
7. Run local doctor, resolve intended provider's networking only with operator authorization, verify signed heartbeat.
8. Use fake executors in tests. On a separately prepared live device verify lock, shutdown/restart confirmation/scheduled result/cancellation/OS failure. ACK is not powered-off proof.
   Also try the observed typo, all shortcut commands and conflicting `/pclock выключи`. Check forward rejection, wrong-user/chat buttons, expiry, repeated taps and cancellation after consumption. With SQL/AI down, PC diagnosis must still respond. Confirm one class push for a matched Attendance/class pair; unmatched Attendance remains visible with push off, and enabling its Settings control survives refresh.
9. Enable both observation controls; compare editor/game/browser/idle/background; disconnect/reconnect/restart: deduplication, gaps, no content leakage, one focus prompt and both opt-outs/deletions.
10. Missing/tampered/expired/other-user API auth; refresh persistence and mobile/desktop layouts.

Live Telegram/provider/device acceptance and Render networking require existing accounts/deployments. Offline mocks cannot substitute for that evidence; production is not deployed.
