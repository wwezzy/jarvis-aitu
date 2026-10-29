# Desktop 4.6: Gemini with a local reserve

This is an opt-in free-project route, not an unlimited-service promise. The
desktop's commands, tasks, notes, recognition and accepted personal voice work
without a paid language provider. Gemini answers free-form questions; one failed
cloud attempt falls back to the installed Ollama model. Model output is display
and speech data and never authorizes a PC action. Cancellation is terminal.

## Migration and configuration

1. Exit the previous desktop normally. Back up the private profile with SQLite's
   backup API, including desktop.db, conversations.db, profilememory.db and
   documents.sqlite3 when present. Keep imported document blobs and the private
   voice manifest outside the repository/distribution. Do not copy live WAL
   databases as ordinary files. Install into a new versioned directory; preserve
   the old runtime for rollback.
2. Use an existing Gemini API key from a project whose AI Studio billing display
   says **Free**. Do not enable billing or rotate keys to evade project quotas.
   `desktop.gemini_mode.save_existing_key` writes only an explicitly selected
   private profile, after operator verification. It saves `gemini.env`, an
   explicit source manifest and a key-bound free-project attestation. Neither
   the key nor these private files belongs in Git or an installer. Process
   `GEMINI_API_KEY` is also supported; no home/repository key search occurs.
3. Enable Gemini and local reserve in Settings. The tested owner configuration
   uses `gemini-3.5-flash-lite`; `gemini-3.8-flash` is a separate supported choice.
   Keep **Без платного OpenAI** enabled. Recognition remains local. The selected
   personal voice remains the default; optional Google speech makes an extra
   quota-consuming request. It falls back before any audio, never after partial
   playback, and never silently substitutes an unrelated Windows voice.
4. Import the identified historical personal snapshot through the Memory panel's
   preview and explicit Import action. Historical facts retain their date and
   uncertainty. Current routine settings (including flexible Tue/Thu/Sun gym)
   and the current evening time outrank archived facts. Review/edit/delete facts
   and separately enable their use as model context. A forgotten fact is excluded
   from later context; the transcript remains visible as history.
5. The first run creates separate persistent **Переписка** and **Голосовой**
   conversations. Older mixed history remains a read-only archive. Typed input
   is silent, voice input is spoken. Only completed model pairs from the selected
   conversation enter bounded optional model history; PC commands and notices do
   not. History and personal-context revocation reset future model context.

## Files and context

Select local TXT, Markdown, text PDF or DOCX in the text conversation, inspect
the extracted preview, then explicitly Analyze. Selecting a file alone sends
nothing to a model. Parsing is local and bounded: 10 MB/file, 30 PDF pages,
50,000 extracted characters, and at most 12,000 selected context characters.
The UI selects up to four files. Images, scanned PDFs/OCR, encrypted documents,
archives and arbitrary executables are not supported. A limit or unsupported
format produces an explicit result; it is not silently treated as analyzed.

Document contents and stored facts are untrusted quoted data, not instructions.
Analysis uses a dedicated path with no computer-command dispatch. Keys and
credential/LMS URL patterns are redacted before model context. This is a
defense-in-depth filter, not a guarantee that arbitrary documents contain no
personal data. Gemini Free may use submitted content to improve its products;
send only documents appropriate for that service. Deleting a local import does
not retract material already sent to a provider.

## Quota and service limits

- Desktop reserves at most 40 cloud requests, 48,000 input characters and 300
  speech seconds per local budget day in a metadata-only private SQLite ledger.
  Failed or uncertain attempts count. Output and history are bounded. The
  provider's own project quotas can be smaller or change independently.
- Quota failures start a 15-minute cooldown. There is no retry loop, automatic
  key rotation or paid fallback. Local wake/recognition/voice keep ordinary
  conversation to one cloud text request; Google TTS is optional.
- The free-project attestation expires after at most 30 days and must be
  reverified. It cannot prevent a later billing change in Google's console.
  Jarvis does not enroll the project in billing. The provider remains the
  authority for quotas and billing.
- Offline free-form answers depend on a running, installed Ollama model and
  available RAM/VRAM. Deterministic commands and local records remain available
  even when both language models fail. Local response quality is not equal to
  every cloud model.

## Telegram staging recovery

Only the staging service is permitted to change. Restore a verified dump into a
new empty database, apply existing idempotent schema migrations, compare all
canonical table hashes and row counts, and close temporary external access.
Use the new database's **internal** URL for the service. Never replace or delete
the expired database as part of recovery. A historical dump cannot recover
records created after its snapshot date.

Staging environment (secrets are values in Render, never committed):

| Variable | Value / purpose |
| --- | --- |
| `DATABASE_URL` | New staging database internal connection URL |
| `BOT_TOKEN`, `ADMIN_ID`, `REDIS_URL`, `TIMEZONE` | Preserve existing staging identity, owner and queue settings |
| `LLM_FREE_ONLY` | `1`; masks paid OpenAI/NVIDIA paths before provider selection |
| `GEMINI_API_KEY` | One existing key; packed/numbered rotation is ignored |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` (allowlisted) |
| `JARVIS_GEMINI_FREE_PROJECT_ATTESTATION` | Verified, unexpired key-bound marker JSON |
| `LLM_FREE_HISTORY_ENABLED` | `1` only when prior dialogue context is intended |
| `LLM_FREE_CONTEXT_ENABLED` | `1` only when personal context is intended |
| `JARVIS_GEMINI_FREE_STATE_DIR` | Optional private local guard directory |
| `ENABLE_WEB_RESEARCH` | `false`; avoids the separate external research API |
| `ENABLE_MASTER_SCHEDULE` | `false` for this owner; avoid obsolete fixed GTG/reflection notices |

The existing PostgreSQL ProviderUsage table reserves Telegram's 40/day allowance
before HTTP under a transaction lock. Failed/unknown requests and quota cooldown
survive restarts; Redis is not its durable allowance. The local character guard
is per instance, not a durable aggregate provider spending limit. Free-only
Telegram refuses unsupported binary model attachments honestly. It has no local
Ollama on Render: when Gemini is unavailable, deterministic bot features remain,
and cloud chat reports that limitation.

Render Free PostgreSQL expires after 30 days, free web services can sleep, and
free Key Value is not durable storage. Recreating a trial is a temporary recovery,
not a permanent backup strategy. Keep a verified private export. The database
created for this recovery expires **2026-11-28**. No paid plan or production
deployment is authorized by this document.

## Exact staging acceptance checklist

- [ ] Full suite passes with real isolated PostgreSQL, Chrome and Qt; all Ubuntu,
      Windows and PostgreSQL CI jobs pass for the exact deployed source commit.
- [ ] A public bounded question reaches Gemini using the product adapter, with
      no private context/audio. Record model, elapsed time and token metadata.
- [ ] Fake quota, timeout and outage each produce one local fallback; cancellation
      and partial speech produce no second response. Paid transports are traps.
- [ ] With both providers offline, tasks, notes, arithmetic and safe command
      classification still work; no model output executes a command.
- [ ] Type a question: no speech. Submit a voice question: spoken reply is queued.
      Switch conversations and restart: histories remain separate and intact.
- [ ] Preview/import a text document; explicit analysis summarizes its contents.
      An embedded shutdown instruction causes no PC action. Unsupported formats,
      path links, oversized input, deletion and canceled parsing are explicit.
- [ ] Import old facts once, verify their date, edit/forget/revoke context and
      confirm the next request excludes both removed facts and old model context.
      Current gym days and evening time win over archived schedules.
- [ ] Lock/revoke the owner during a blocked model answer: no further chunks,
      stored assistant answer or speech is delivered after revocation.
- [ ] New staging DB matches the verified snapshot's canonical table hashes and
      records. Temporary external IP list is empty. Existing production resources
      and expired source DB are unchanged.
- [ ] Staging uses the new internal DATABASE_URL, the same bot identity, free-only
      flags and the reviewed branch commit; health and logs show no DB DNS errors.
      Verify Telegram identity/webhook without sending an unsolicited message.
- [ ] Native copied runtime launches; installed source hashes match the commit;
      current preferences, tasks, notes, phone secrets and accepted voice pins
      are preserved. Real user microphone/noise and destructive power actions
      remain separate, supervised acoustic/device acceptance.

Primary references: [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing),
[project rate limits](https://ai.google.dev/gemini-api/docs/rate-limits),
[Render Free limits](https://render.com/docs/free).
