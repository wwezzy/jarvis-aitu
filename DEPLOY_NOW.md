# Jarvis v2 — activate it in your real Telegram bot

## 1. Keep your current secrets and data

Before replacing the old project, save these from the current PyCharm project if they exist:

- `.env`
- `jarvis.db`
- `profile.local.md`

Do **not** publish them to GitHub.

## 2. Replace the code

Extract this release into a new folder, for example `jarvis-aitu-v2`.
Copy your saved `.env`, `jarvis.db` and `profile.local.md` into that folder.

If your old `.env` does not contain the new variables, add them using `.env.example` as the reference.
At minimum the bot needs:

```env
BOT_TOKEN=...
ADMIN_ID=...
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.8-flash
DATABASE_URL=sqlite+aiosqlite:///jarvis.db
TIMEZONE=Asia/Almaty
ENABLE_MASTER_SCHEDULE=true
HOST=0.0.0.0
PORT=8080
MINIAPP_DEV_MODE=false
```

Redis is optional for basic bot startup, but required for Redis chat context / the PC agent:

```env
UPSTASH_REDIS_REST_URL=...
UPSTASH_REDIS_REST_TOKEN=...
PC_AGENT_SECRET=...
```

Rotate an Upstash token if it was previously committed publicly.

## 3. Install and run on Windows / PyCharm machine

Open PowerShell in the new project folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup_windows.ps1
.\run_jarvis.ps1
```

Or in PyCharm choose `.venv\Scripts\python.exe` as the interpreter and run `aihelper.py`.

A successful start should contain log lines similar to:

```text
Initializing Jarvis database
Scheduler active
HTTP/Mini App server listening on 0.0.0.0:8080
Jarvis online. Starting Telegram long polling
```

At that moment the **bot code is already changed in Telegram**, because it is the same BotFather token running the new program.
Test:

```text
/start
/today
/gtg
/workouts
/memory
/app
```

## 4. Mini App needs public HTTPS

The bot itself can work from your PC with long polling. The Telegram Mini App cannot point to `localhost`; Telegram needs a public HTTPS URL.

Deploy this same application to an HTTPS host, or temporarily expose local port `8080` with an HTTPS tunnel.
Set the resulting base URL in `.env`:

```env
WEBAPP_URL=https://YOUR-PUBLIC-HTTPS-URL
```

Restart Jarvis. On startup it configures the private chat menu button `Jarvis Console` to:

```text
https://YOUR-PUBLIC-HTTPS-URL/app
```

For a permanent production setup, host the same process on a service with HTTPS and a persistent PostgreSQL database:

```env
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME
WEBAPP_URL=https://your-domain.example
```

## 5. Important: only one bot process

Stop the old `aihelper.py` / server instance before starting v2. Two processes polling the same Telegram bot token will conflict and make behavior look random or unchanged.

If the old bot is deployed on Render/Railway/VPS/etc., updating only PyCharm is not enough: stop/redeploy that old service with the v2 source.
