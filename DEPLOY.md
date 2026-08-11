# Deploying to Coolify — step by step

Written for someone who is not a programmer. Do the steps in order. Nothing here
costs money.

Total time: about 45 minutes, most of it waiting.

---

## Step 0 — Rotate your API keys first (do not skip)

The old `voice_app.py` had your Pollinations and Fish Audio keys written inside
it, and your AssemblyAI key has been pasted into a chat window. Treat all three
as compromised and replace them before you deploy:

| Service | Where to rotate |
|---|---|
| AssemblyAI | https://www.assemblyai.com/app/api-keys |
| Pollinations | https://enter.pollinations.ai — regenerate your key |
| Fish Audio | https://fish.audio/go-api/ — delete the old key, create a new one |

Keep the new keys in a password manager. From here on they only ever get typed
into Coolify's environment variables screen, never into a file.

---

## Step 1 — Generate your two app secrets

You need two long random strings. Run this twice and keep both answers:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

- The first is `SECRET_KEY`. It signs visitor cookies and session tokens.
  **Set it once and never change it** — changing it wipes every caller's memory.
- The second is `ADMIN_TOKEN`. It is the password for your `/admin` dashboard.

---

## Step 2 — Put the code on GitHub

The whole project folder is ready to push. `.gitignore` already excludes `.env`,
the database and the virtual environment, so no secret can leak.

Create an **empty private** repository at https://github.com/new — do not let
GitHub add a README, .gitignore or licence.

Then, in the `voice_agent` folder:

```bash
git init -b main
```

```bash
git add .
```

Check what is about to be committed. You should see `app/`, `static/`,
`templates/`, `knowledge/`, `Dockerfile` and so on — and **no `.env`, no
`.venv`, no `data/`**:

```bash
git status
```

```bash
git commit -m "Streaming voice agent: AssemblyAI STT, abuse limits, lead capture"
```

```bash
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
```

```bash
git push -u origin main
```

If `git push` asks for a password, use a Personal Access Token from
https://github.com/settings/tokens (classic, with the `repo` scope), not your
account password.

---

## Step 3 — Point a subdomain at your server

In your DNS provider, add an **A record**:

- Name: `voice`
- Value: your Coolify server's IP address
- Proxy/orange cloud: **off** for now (turn it on later if you want)

That gives you `voice.yourdomain.com`. DNS usually updates within a few minutes.

> HTTPS is not optional. Browsers refuse microphone access on plain HTTP for any
> host except `localhost`. Coolify issues the certificate for you, but only if
> the domain actually resolves to the server first.

---

## Step 4 — Create the application in Coolify

1. Open your Coolify dashboard.
2. **+ New** → **Application** → **Public Repository** (or **Private Repository
   (with GitHub App)** if you made the repo private).
3. Paste your repository URL. Branch: `main`.
4. **Build Pack: `Dockerfile`.** Do not use Nixpacks — the Dockerfile pins the
   Python version and the uvicorn websocket flags this app needs.
5. Set **Port** to `8000`.
6. Set **Domain** to `https://voice.yourdomain.com` (include `https://`).
7. Save. **Do not deploy yet** — set the environment variables first.

---

## Step 5 — Environment variables

Open the **Environment Variables** tab and add these. Coolify stores them
encrypted; they never touch your repository.

**Required:**

```
ASSEMBLYAI_API_KEY      your new AssemblyAI key
POLLINATIONS_API_KEY    your new Pollinations key
FISH_API_KEY            your new Fish Audio key
FISH_MODEL_ID           e0ea460db14b4430afb6311708937b6d
SECRET_KEY              the first random string from Step 1
ADMIN_TOKEN             the second random string from Step 1
ALLOWED_ORIGINS         https://voice.yourdomain.com
```

`ALLOWED_ORIGINS` has no trailing slash. It is what stops other websites
embedding your agent and spending your speech-to-text hours.

**Recommended limits** (defaults shown; adjust once you see real traffic):

```
SESSION_MAX_SECONDS         180
SESSIONS_PER_IP_PER_DAY     3
GLOBAL_SESSIONS_PER_DAY     120
GLOBAL_STT_SECONDS_PER_DAY  3600
MAX_CONCURRENT_SESSIONS     8
```

---

## Step 6 — Add persistent storage

Without this, every caller's memory and all captured leads are erased on each
redeploy.

**Storages** tab → **+ Add** → **Volume Mount**, twice:

| Name | Destination Path |
|---|---|
| `agent-data` | `/app/data` |
| `agent-knowledge` | `/app/knowledge` |

---

## Step 7 — Deploy

Press **Deploy** and watch the logs. The first build takes 2–4 minutes.

You are looking for:

```
INFO agent knowledge base: {'files': [...], 'mode': 'inline', ...}
INFO fillers filler clips ready (9/9)
INFO Uvicorn running on http://0.0.0.0:8000
```

The filler clips are synthesised once on first boot (about 15 seconds) and then
cached on the volume forever.

If you instead see:

```
ERROR agent MISSING REQUIRED ENV VARS: ...
```

a key is missing or misspelt. Fix it and redeploy.

---

## Step 8 — Check it works

1. Visit `https://voice.yourdomain.com`. Confirm the padlock is showing.
2. Press the green call button and **allow microphone access**.
3. You should hear the greeting within a couple of seconds.
4. Say *"I run a plumbing business and I keep missing calls."*
5. You should hear "Sure." almost immediately, then a real answer.

If the microphone is blocked, the site is not on HTTPS — go back to Step 3.

If the call connects but nothing is ever transcribed, open the browser console
(F12). A `4001`/`401` close code on the AssemblyAI socket means the API key is
wrong or the free allowance is exhausted.

---

## Step 9 — Turn on bot protection

Do this before you link to the page publicly.

1. Go to https://dash.cloudflare.com → **Turnstile** → **Add site**.
2. Domain: `voice.yourdomain.com`. Widget mode: **Invisible**.
3. Copy the **Site Key** and **Secret Key** into Coolify:

```
TURNSTILE_SITE_KEY      0x4AAA...
TURNSTILE_SECRET_KEY    0x4AAA...
```

4. Redeploy.

Visitors see nothing extra. Scripted abuse is turned away before it can consume
a single second of speech-to-text.

---

## Step 10 — Send leads to Google Sheets (optional)

1. Create a Google Sheet and name the first tab `Leads`.
2. **Extensions → Apps Script**, delete everything, paste in `sheets-webhook.gs`.
3. Change `SHARED_SECRET` to a long random string.
4. **Deploy → New deployment → Web app**, execute as **Me**, access **Anyone**.
5. Copy the `/exec` URL, then in Coolify:

```
SHEETS_WEBHOOK_URL      the /exec URL
SHEETS_WEBHOOK_SECRET   the same string as SHARED_SECRET
```

6. Redeploy.

Leads are always saved locally regardless, and visible at `/admin`. The sheet is
just a convenience.

---

## Step 11 — Load your real business knowledge

This is the step that decides whether the agent sounds like you or like a
generic bot. **Do not skip it.**

Open `https://voice.yourdomain.com/admin?token=YOUR_ADMIN_TOKEN`.

The four starter files under `knowledge/` were written from your existing system
prompt and README. They are a reasonable skeleton, but they are **my summary of
your business, not yours**. Rewrite them with:

- The services you actually sell, in the words you use to sell them
- Real answers to the questions you get asked most
- Your actual pricing approach (`03-pricing.md` has a placeholder — replace it)
- Case studies and results you are happy to quote out loud

You can edit the files in the repo and push, or upload `.md` files directly from
`/admin`. Uploads take effect immediately, no redeploy needed.

Keep it factual and specific. The agent is instructed never to invent a price or
a claim, so anything not written down becomes "the team will follow up" — which
is safe, but it does not sell.

---

## Step 12 — Embed it on your website (optional)

```html
<iframe src="https://voice.yourdomain.com"
        allow="microphone"
        style="border:0;width:400px;height:720px"></iframe>
```

The `allow="microphone"` attribute is required. Add your main site to
`ALLOWED_ORIGINS` as well, comma separated:

```
ALLOWED_ORIGINS=https://voice.yourdomain.com,https://indishmarketer.com
```

---

## Running it day to day

**Watch your usage** at `/admin?token=...`. The tiles to keep an eye on are
*STT minutes today* and *STT hours all-time* against your 300-hour allowance.

**If usage climbs faster than you like**, lower these and redeploy:

```
SESSIONS_PER_IP_PER_DAY     2
SESSION_MAX_SECONDS         120
GLOBAL_STT_SECONDS_PER_DAY  1800
```

**To change the agent's voice**, set `FISH_MODEL_ID` to another Fish Audio voice
ID. The filler clips re-render automatically in the new voice on next boot.

---

## Troubleshooting

**Microphone permission denied**
Not on HTTPS, or the browser has remembered a previous denial. Check the padlock
and reset site permissions.

**Calls connect but the websocket drops after ~60 seconds**
Something in front of Coolify is closing idle upgraded connections. If you put
Cloudflare's proxy in front of the domain, make sure WebSockets are enabled in
**Network** settings. The app already sends pings every 20 seconds.

**"All lines are busy"**
`MAX_CONCURRENT_SESSIONS` reached. Raise it if your server has the headroom.

**"You have used up today's free calls"**
Working as intended — that is the per-IP limit. It resets at midnight UTC.

**The agent gives vague answers**
Your knowledge base does not cover the question. Add it and re-upload. Check
`/admin` to confirm the file loaded and whether it is in `inline` or `retrieval`
mode.

**Deploys are slow or the container restarts repeatedly**
Check the healthcheck is passing — visit `/healthz`, which should return `ok`.
Note the app deliberately runs a **single worker**: session slots, the
concurrency cap and SQLite writes all live in one process. Do not raise the
worker count.
