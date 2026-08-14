# Deploying to Coolify — step by step

Written for someone who is not a programmer. Do the steps in order. Nothing
here costs money by itself (see "Running it day to day" for what does).

Total time: about 45 minutes, most of it waiting.

---

## Step 0 — Rotate your API keys first (do not skip)

If any of your Pollinations/Fish Audio/AssemblyAI keys have ever been pasted
into a chat window, a file, or anywhere outside Coolify's environment
variables screen, treat them as compromised and replace them before you
deploy:

| Service | Where to rotate |
|---|---|
| AssemblyAI | https://www.assemblyai.com/app/api-keys |
| Pollinations | https://enter.pollinations.ai — regenerate your key |
| Fish Audio | https://fish.audio/go-api/ — delete the old key, create a new one |

Keep the new keys in a password manager. From here on they only ever get
typed into Coolify's environment variables screen, never into a file.

---

## Step 1 — Generate your two app secrets

You need two long random strings. Run this twice and keep both answers:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

- The first is `SECRET_KEY`. It signs visitor cookies and session tokens for
  **every account on the platform**, not just the main one.
  **Set it once and never change it** — changing it wipes every caller's
  memory and logs every admin (main and sub-account) out at once.
- The second is `ADMIN_TOKEN`. It is the master password for `/admin` — it
  always works for the main account, regardless of magic-link login.

---

## Step 2 — Put the code on GitHub

`.gitignore` already excludes `.env`, the database and the virtual
environment, so no secret can leak through a normal `git add .`.

```bash
git init -b main
git add .
git status   # check for app/, static/, templates/, knowledge/, Dockerfile —
             # and NO .env, .venv, or data/
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

If `git push` asks for a password, use a Personal Access Token from
https://github.com/settings/tokens, not your account password.

---

## Step 3 — Point a subdomain at your server

In your DNS provider, add an **A record**:

- Name: `voice` (or whatever you want the main agent's subdomain to be)
- Value: your Coolify server's IP address
- Proxy/orange cloud: **off** for now (turn it on later if you want)

> HTTPS is not optional. Browsers refuse microphone access on plain HTTP for
> any host except `localhost`. Coolify issues the certificate for you, but
> only if the domain actually resolves to the server first.

If you plan to let sub-accounts live on `<slug>.yourdomain.com` instead of
`?account=<slug>`, you'll also want a **wildcard** A record
(`*.yourdomain.com`) pointed at the same IP — see `PLATFORM_ROOT_DOMAIN`
in Step 5. Not required to launch; `?account=<slug>` works everywhere
already, on day one, with no DNS changes at all.

---

## Step 4 — Create the application in Coolify

1. Open your Coolify dashboard.
2. **+ New** → **Application** → **Public Repository** (or **Private
   Repository (with GitHub App)** if your repo is private).
3. Paste your repository URL. Branch: `main`.
4. **Build Pack: `Dockerfile`.** Do not use Nixpacks — the Dockerfile pins
   the Python version and the uvicorn websocket flags this app needs.
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

`ALLOWED_ORIGINS` has no trailing slash, comma-separated if you add more
domains later. It gates the raw `/api/session/start` call itself (embedded
widgets go through their own iframe and are unaffected by this list).

**Sub-account platform settings** (all optional — sane defaults, only set
what you want to change):

```
MAX_ACCOUNTS                  20        how many approved sub-accounts at once
DEFAULT_TRIAL_END_DATE        2026-08-31   the beta free-access cutoff, YYYY-MM-DD
TRIAL_DAYS_AFTER_CUTOFF       3         rolling trial length for signups after the cutoff
DEFAULT_TRIAL_DAILY_SECONDS   1200      new trial account's default daily cap (seconds)
SUBSCRIPTION_MONTHLY_MINUTES_CAP  500   fair-use ceiling for the subscription plan
PLATFORM_ROOT_DOMAIN                    empty until you buy a domain for sub-accounts -
                                        see Step 3's wildcard DNS note
```

**Magic-link admin login** (optional — `ADMIN_TOKEN` always works without
this, but sub-account owners need it to log into their own dashboard):

```
BREVO_API_KEY           from Brevo → Settings → SMTP & API → API Keys
BREVO_SENDER_EMAIL      a verified sender in your Brevo account
BREVO_SENDER_NAME       Voice Agent
ADMIN_EMAILS            comma-separated - only these can log into the MAIN account
```

**Recommended limits** (defaults shown; adjust once you see real traffic —
these are the original global anti-abuse numbers and still apply on top of
everything above, across every account combined):

```
SESSION_MAX_SECONDS         180
SESSIONS_PER_IP_PER_DAY     3
GLOBAL_SESSIONS_PER_DAY     120
GLOBAL_STT_SECONDS_PER_DAY  3600
MAX_CONCURRENT_SESSIONS     8
```

---

## Step 6 — Add persistent storage

Without this, every caller's memory, every account's data, and all captured
leads are erased on each redeploy. **This one `agent-data` volume covers
every sub-account too** — their knowledge base and branding files live
under `data/accounts/<id>/`, on the same volume, automatically.

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
INFO agent approved sub-accounts: 0/20
INFO fillers filler clips ready (...)
INFO Uvicorn running on http://0.0.0.0:8000
```

If you instead see:

```
ERROR agent MISSING REQUIRED ENV VARS: ...
```

a key is missing or misspelt. Fix it and redeploy.

---

## Step 8 — Check it works

1. Visit `https://voice.yourdomain.com`. Confirm the padlock is showing.
2. Press the call button and **allow microphone access**.
3. You should hear the greeting within a couple of seconds.
4. Say something relevant to your business and confirm the reply is accurate.

If the microphone is blocked, the site is not on HTTPS — go back to Step 3.

If the call connects but nothing is ever transcribed, open the browser
console (F12). A `4001`/`401` close code on the AssemblyAI socket means the
API key is wrong or the allowance is exhausted.

---

## Step 9 — Turn on bot protection

Do this before you link to the page publicly.

1. Go to https://dash.cloudflare.com → **Turnstile** → **Add site**.
2. Domain: `voice.yourdomain.com`. Widget mode: **Invisible**.
3. Copy the **Site Key** and **Secret Key** into Coolify (or set them later
   from `/admin/integrations` — no redeploy needed either way):

```
TURNSTILE_SITE_KEY      0x4AAA...
TURNSTILE_SECRET_KEY    0x4AAA...
```

4. Redeploy if you set them via Coolify env vars.

Visitors on the main site see nothing extra. **Embedded widgets on
third-party sites skip Turnstile entirely** — Cloudflare's own widget does
not work reliably inside a cross-origin iframe, and there is no way to
pre-register every future customer's embedding domain with it — so that
path leans on the daily/IP/account quotas instead. Nothing to configure for
that; it's automatic.

---

## Step 10 — Load your real business knowledge

This is the step that decides whether the main agent sounds like you or
like a generic bot. **Do not skip it.**

Open `https://voice.yourdomain.com/admin?token=YOUR_ADMIN_TOKEN`.

The four starter files under `knowledge/` are a reasonable skeleton, not
your actual business. Rewrite them with:

- The services you actually sell, in the words you use to sell them
- Real answers to the questions you get asked most
- Your actual pricing — the agent is instructed never to invent a price, so
  anything not written down becomes "the team will follow up," which is
  safe but does not sell
- Case studies and results you are happy to quote out loud

You can edit the files in the repo and push, or upload `.md` files directly
from `/admin/knowledge`. Uploads take effect immediately, no redeploy
needed, capped at 2MB per file.

---

## Step 11 — Embed it on your website

Every account — the main one and every sub-account — has its own ready-made
snippet on its dashboard (`/admin` → Share your agent). Copy-paste, no
manual construction needed:

```html
<script src="https://voice.yourdomain.com/embed.js"
        data-account="a-sub-accounts-slug"
        data-label="Talk to us"
        async></script>
```

Omit `data-account` entirely to embed the **main** account's agent instead
of a sub-account's. `data-label` and `data-position` (`bottom-right` /
`bottom-left`) are optional. It renders a small launcher button and a
centered modal — no iframe sizing or styling to get right by hand, and it
works regardless of where in the page (`<head>` or `<body>`) the script tag
ends up, or whether the host page injects it via raw HTML or its own JS.

---

## Step 12 — Running a beta: applications and approvals

1. Share `https://voice.yourdomain.com/apply` — a public form (business
   name, email, website — required, since callers get pointed there — and
   country, which sets their default voice).
2. Review submissions at `/admin/accounts`. **Approve** creates their
   account, sends them a login email (if Brevo is configured), and assigns
   them a trial per Step 5's settings. **Reject** or **Delete** to undo a
   mistake — delete also removes their leads, knowledge base and usage
   history.
3. Once they're paid outside the app (no gateway wired up yet), use
   **Edit plan** on that account to mark it `subscription` (you keep
   covering everything, capped at the monthly fair-use minutes) or
   `onetime` (they bring their own provider keys — their own Settings page
   gets a section for that once the plan is switched; their agent is
   blocked from taking calls until all four fields are filled in).

Sub-account owners manage their own knowledge base, branding, voice, and
leads from their own login — same `/admin` URL, scoped automatically to
their account. They cannot see or reach Integrations, global Settings, or
the Accounts queue.

---

## Running it day to day

**Watch your usage** at `/admin` — the tiles to keep an eye on are STT
minutes today and STT hours all-time, against whatever allowance you're on.

**If usage climbs faster than you like**, lower these and redeploy:

```
SESSIONS_PER_IP_PER_DAY     2
SESSION_MAX_SECONDS         120
GLOBAL_STT_SECONDS_PER_DAY  1800
```

**To change the main agent's voice**, set `FISH_MODEL_ID` (or edit it from
`/admin/integrations`) to another Fish Audio voice ID from your own account.
Sub-accounts pick from the four built-in presets on their own Settings page
instead (or bring their own, on the one-time plan).

---

## Troubleshooting

**Microphone permission denied**
Not on HTTPS, or the browser has remembered a previous denial. Check the
padlock and reset site permissions.

**Calls connect but the websocket drops after ~60 seconds**
Something in front of Coolify is closing idle upgraded connections. If you
put Cloudflare's proxy in front of the domain, make sure WebSockets are
enabled in **Network** settings. The app already sends pings every 20
seconds.

**"All lines are busy"**
`MAX_CONCURRENT_SESSIONS` reached. Raise it if your server has the
headroom.

**"You have used up today's free calls" / "this month's fair-use limit"**
Working as intended — a quota was hit. The daily ones reset at midnight
UTC; the subscription one resets monthly. If it's the account owner
themselves hitting it while testing, log into that account's own admin
dashboard first (in the same browser) and use the "Open your agent" link
there — that bypasses that account's own caps without touching the public
quotas for anyone else.

**"We could not verify you are human" — but only when embedded**
This was a real bug, fixed: Turnstile does not work inside a cross-origin
iframe, and embedded calls now skip it entirely (see Step 9). If you still
see this on an embed, confirm the widget is loading from the current
`embed.js` (browser cache can hold an old copy — hard refresh the host
page).

**A one-time-plan account's agent won't take calls**
Expected until all four of their own provider fields (AssemblyAI,
Pollinations, Fish Audio API keys, and their own Fish Audio voice ID) are
filled in on their Settings page — there is no fallback to your keys on
that plan by design.

**The agent gives vague answers**
Its knowledge base does not cover the question. Add it and re-upload.
Check `/admin/knowledge` to confirm the file loaded and whether it is in
`inline` or `retrieval` mode.

**Deploys are slow or the container restarts repeatedly**
Check the healthcheck is passing — visit `/healthz`, which should return
`ok`. Note the app deliberately runs a **single worker**: session slots,
the concurrency cap and SQLite writes all live in one process. Do not raise
the worker count.
