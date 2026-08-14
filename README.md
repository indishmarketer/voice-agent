# Indish Marketer Voice Agent

A real-time, browser-based voice receptionist — and a multi-tenant platform
around it. Any business can apply, get approved, and get their own agent:
trained on their own knowledge base, in their own voice and branding,
embeddable on their own website, with its own trial/billing plan.

Built to run for free on a self-hosted Coolify instance.

**To deploy it, follow [DEPLOY.md](DEPLOY.md).** This file explains how it
works and how to change its behaviour. **For the low-level call pipeline
(latency, audio framing, the turn state machine), see
[ARCHITECTURE.md](ARCHITECTURE.md).**

---

## What it does in one turn

```
caller speaks
   │
   ▼  browser streams 16 kHz PCM, but ONLY while someone is talking
AssemblyAI v3 streaming  ──── final transcript ────┐
   (browser connects directly, using a             │
    one-time token minted by our server)           ▼
                                            our FastAPI server
                                                   │
              ┌────────────────────────────────────┤
              ▼                                    ▼
      filler clip plays instantly          Pollinations (streaming)
      ("Sure." / "One moment.")                    │ clause by clause
                                                   ▼
                                          Fish Audio websocket TTS
                                                   │ PCM frames
                                                   ▼
                                        browser plays them as they arrive
```

Measured on the deployed path: the caller hears an acknowledgement about
**0.5 seconds** after they stop speaking, and the substantive answer begins
around **2–3 seconds** in, flowing continuously behind the filler.

## Accounts: the main agent, and everyone else's

Every request resolves to an **account** before anything else happens - the
main agent (`account_id = None`, unchanged since before multi-tenancy
existed) or one approved sub-account. Resolution is by `?account=<slug>`
query param today, and will also work by subdomain
(`<slug>.yourdomain.com`) automatically once `PLATFORM_ROOT_DOMAIN` is set -
no code change needed for that later.

```
someone applies at /apply (business name, email, website, country - required)
        │
        ▼
pending in /admin/accounts, capped at MAX_ACCOUNTS (default 20)
        │  owner clicks Approve
        ▼
approved: gets a slug, a trial plan, and a magic-link login of its own
   (their contact email doubles as their admin login - same magic-link
    mechanism as the main account, just scoped to their own account_id)
```

**What's isolated per account:** knowledge base, branding (logo/favicon/brand
name/greeting - seeded from their business name until customised), agent
behaviour rules, voice, leads, usage stats, and (on the one-time plan) their
own provider API keys. **What's shared/owner-controlled:** the provider
integrations page, Turnstile, and the global infra safety caps.

### Plans

| Plan | Who pays for API usage | Cap |
|---|---|---|
| `trial` | the owner | `daily_minutes_limit` per day (default 20) |
| `subscription` | the owner | `SUBSCRIPTION_MONTHLY_MINUTES_CAP` per month (default 500) |
| `onetime` | the customer, with their **own** provider keys | none - their own keys, their own cost |

A new approval gets `DEFAULT_TRIAL_END_DATE` (a fixed beta cutoff) while
that date is still in the future, or a rolling `TRIAL_DAYS_AFTER_CUTOFF`-day
trial from its own approval time once it has passed. There is no payment
gateway wired up - `/admin/accounts` → **Edit plan** is how an owner
manually marks an account `subscription`/`onetime` after being paid outside
the app.

**The `onetime` plan is the one real architectural fork.** Every provider
call (AssemblyAI, Pollinations, Fish Audio) resolves its credentials fresh,
server-side, per session (`integrations.resolve_for_account`) - the main
account and every other plan use the owner's global keys exactly as before;
`onetime` uses *only* that account's own stored keys, with **no fallback**,
because a fallback would mean the owner silently keeps paying. It also needs
its own Fish Audio voice id (a voice clone belongs to whichever account
created it, so the owner's own voice id will not work against a different
account's API key) - so it does not pick from the shared voice presets like
everyone else. Calls are blocked outright, with a clear message, until all
four fields are filled in on that account's own Settings page.

### Voice

Four presets, shared across every account except `onetime` (which brings its
own): English Male/Female, Indian English Male/Female. `/apply` asks for
country and defaults India → Indian Male, everywhere else → English Female;
either can be changed afterwards. Filler ("one moment") clips are rendered
and cached **per voice**, not just once, so the acknowledgement never plays
in a different voice than the reply that follows it.

### Embedding

`static/embed.js` - a `<script src=".../embed.js" data-account="<slug>">`
tag any business (including the owner, for the main account - just omit
`data-account`) can paste onto their own site. It injects a small launcher
button and a centered modal loading an iframe at `/?account=<slug>&embed=1`.
The `embed=1` flag matters: **Cloudflare Turnstile does not work reliably
inside a cross-origin iframe on a third-party site**, and there is no way to
pre-register every future customer's embedding domain with it, so an
embedded session skips Turnstile specifically and leans on the
daily/IP/account call quotas instead - those are not skipped.

## Why it is fast

Every stage overlaps instead of queuing - see
[ARCHITECTURE.md](ARCHITECTURE.md) for the byte-level detail (concurrent
socket-open + first-token, clause-by-clause TTS streaming, the filler-clip
bridge, etc).

## Why it is cheap

AssemblyAI is the one hard per-minute cost (roughly $0.01–0.02/min); the
browser **only streams audio while somebody is actually speaking** (a local
voice-activity gate), which cuts billed time by roughly two thirds on a
typical call. Pollinations moved to a paid pay-as-you-go credit system
(`$1 ≈ 1 Pollen`) - specific models are still free to call, but "free" is
model-specific now, not a blanket platform-wide tier, and which models are
free can change as they ship new ones. Fish Audio's tier is whatever is
currently configured on the account in use.

## Why it is hard to abuse

There is no login for a public caller, so protection is layered instead:

1. **Cloudflare Turnstile** — invisible bot check before a normal (non-embedded) call can start. Free.
2. **One-time AssemblyAI tokens** — minted server side, valid for 60 seconds,
   single use. The real API key never reaches a browser.
3. **AssemblyAI enforces the session length itself** via
   `max_session_duration_seconds`, so even a stolen token cannot exceed one
   short call.
4. **Signed session tokens** — the websocket rejects anything we did not issue, and carries only IDs (session/visitor/account), never provider credentials - those are re-resolved server-side when the call connects.
5. **Quotas, layered**: per-IP/per-visitor/global daily caps (the original single-tenant safety net, still active for everyone), plus per-account trial/monthly caps on top for sub-accounts.
6. **Origin checks** — only allow-listed domains may start a call.
7. **Cooldowns** on the unauthenticated `/admin/login` and `/apply` endpoints — one attempt per key per window, recorded whether or not the request was even eligible, so neither can be used to email-bomb an address or flood the applications queue.
8. **Idle hangup** and a hard per-call timer, enforced server side.

All limits are environment variables; see `.env.example`.

## Knowledge and memory

**Knowledge** lives in `knowledge/*.md` for the main account, and
`data/accounts/<id>/knowledge/*.md` per sub-account (same four-file
convention, same admin upload UI, just a different directory - see
`knowledge.py::dir_for`). While the whole base is under `KB_INLINE_LIMIT`
characters the agent sees all of it on every turn; past that it
automatically switches to BM25 retrieval and injects only the sections
relevant to the question.

**Memory** is a first-party cookie plus SQLite, namespaced per account
(`store.visitor_key`) so the same browser visiting two different embedded
widgets never leaks one business's caller history into another's.

## Lead capture

After hangup, the transcript is passed through an extraction step that pulls
name, email, phone, company, problem and interest into `leads`, with a regex
fallback that reconstructs emails people spell out loud
("bob at gmail dot com" → `bob@gmail.com`). Google Sheets sync is retired -
`/admin/leads` has CSV export (all leads, or just the ones selected)
instead, scoped to whichever account's admin is logged in.

## Project layout

```
app/
  main.py          FastAPI app: pages, session issuing, the call websocket, all of /admin
  config.py        every setting, all from environment variables
  security.py      Turnstile, signed tokens, quotas, cooldowns, origin checks
  stt.py           AssemblyAI temporary tokens
  llm.py           Pollinations streaming + clause splitting + reply cap
  tts.py           Fish Audio websocket synthesis
  fillers.py       pre-rendered acknowledgement clips, cached per voice
  knowledge.py     markdown loading and BM25 retrieval, per account
  store.py         SQLite: accounts, visitors, sessions, transcripts, leads, settings, support reports
  leads.py         post-call summary and extraction
  prompts.py       persona and prompt assembly, per account
  branding.py      logo/favicon/brand name/greeting, per account
  integrations.py  provider credentials/models - global, and resolve_for_account() for the one-time plan
  voices.py        the four shared voice presets
  mailer.py        Brevo transactional email (magic links, approvals, limit/email-change notices)
static/
  app.js, pcm-worklet.js, style.css   the call UI
  embed.js                            the embeddable widget
templates/
  index.html                          the call UI
  apply.html                          public application form
  admin_base.html, admin_dashboard.html, admin_leads.html, admin_knowledge.html,
  admin_settings.html, admin_integrations.html, admin_accounts.html, admin_support.html
knowledge/         the main account's business knowledge, as markdown
```

## Running locally

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env`, fill in the three API keys and a `SECRET_KEY`,
leave `ALLOWED_ORIGINS` blank for local work, then:

```bash
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000. `localhost` counts as a secure context, so the
microphone works without HTTPS. **On any other host you must have HTTPS** or
the browser will refuse microphone access — Coolify handles that for you.

## Tuning

| Setting | Effect |
|---|---|
| `POLLINATIONS_MODEL` | `gemini-fast` is the default: shortest replies at equal speed. |
| `MAX_REPLY_CHARS` | Hard cap on how much the agent says per turn. |
| `SESSION_MAX_SECONDS` | Hard call length. AssemblyAI enforces this too. |
| `GLOBAL_STT_SECONDS_PER_DAY` | Daily speech-to-text budget across every account combined. |
| `MAX_ACCOUNTS` | Approved-account cap; `/apply` shows "beta full" and disables submission past it. |
| `DEFAULT_TRIAL_END_DATE` / `TRIAL_DAYS_AFTER_CUTOFF` | The beta cutoff, and the rolling trial length once it has passed. |
| `SUBSCRIPTION_MONTHLY_MINUTES_CAP` | Fair-use ceiling for the `subscription` plan. |
| `PLATFORM_ROOT_DOMAIN` | Empty until a real domain is bought - subdomain account resolution activates automatically once set. |

Changing the filler phrases or a voice automatically re-renders the clips for
that voice — they are cached under a fingerprint of the phrases, voice ID and
sample rate.
