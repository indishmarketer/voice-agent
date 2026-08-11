# Indish Marketer Voice Agent

A real-time, browser-based voice receptionist. The caller speaks, the agent
answers out loud from your own knowledge base, remembers them next time, and
captures their contact details into a spreadsheet.

Built to run for free on a self-hosted Coolify instance.

**To deploy it, follow [DEPLOY.md](DEPLOY.md).** This file explains how it
works and how to change its behaviour.

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
around **2–3 seconds** in, flowing continuously behind the filler. The previous
version took 4–6 seconds of complete silence before anything played.

## Why it is fast

Every stage overlaps instead of queuing:

| Stage | Before | Now |
|---|---|---|
| Speech to text | Browser Web Speech API, waits for full utterance | AssemblyAI v3 streaming, fires on end-of-turn |
| Text generation | Wait for the entire reply | SSE stream, cut into clauses as it writes |
| Speech synthesis | POST, wait for a whole MP3, base64 it | WebSocket, PCM frames forwarded as produced |
| Connection setup | Sequential | TTS socket opens *while* the model is thinking |
| Dead air | 4–6 s of silence | Pre-rendered filler covers it |

Two smaller wins that matter: `latency: "balanced"` on Fish Audio is 2.7× faster
to first audio than `"normal"`, and the reply is capped server-side
(`MAX_REPLY_CHARS`) so no model can ramble a caller into boredom.

## Why it is cheap

AssemblyAI's free allowance is the only scarce resource, so the browser
**only streams audio while somebody is actually speaking**:

- A local energy gate opens on speech and closes ~1.1 s after it stops.
- A 300 ms pre-roll buffer means the first word is never clipped.
- Nothing is sent while the agent is talking, or during silence.
- `KeepAlive` messages hold the session open while the gate is shut.

On a typical call this cuts billed speech-to-text time by roughly two thirds.

Pollinations and Fish Audio are on free tiers with no hard cap, so they are not
rate limited beyond the per-call reply cap.

## Why it is hard to abuse

There is no login, so protection is layered instead:

1. **Cloudflare Turnstile** — invisible bot check before a call can start. Free.
2. **One-time AssemblyAI tokens** — minted server side, valid for 60 seconds,
   single use. Your API key never reaches a browser.
3. **AssemblyAI enforces the session length itself** via
   `max_session_duration_seconds`, so even a stolen token cannot exceed one
   short call.
4. **Signed session tokens** — the websocket rejects anything we did not issue.
5. **Quotas** — per IP per day, per visitor per day, global per day, a global
   daily speech-to-text budget, and a concurrent-call cap.
6. **Origin checks** — only your own domain may start a call.
7. **Idle hangup** and a hard per-call timer, enforced server side.

All limits are environment variables; see `.env.example`.

## Knowledge and memory

**Knowledge** lives in `knowledge/*.md`. Edit the files, or upload new ones from
`/admin`. While the whole base is under `KB_INLINE_LIMIT` characters the agent
sees all of it on every turn; past that it automatically switches to BM25
retrieval and injects only the sections relevant to the question. The retriever
is plain Python, so there is no embedding API, no extra cost and no extra
latency.

**Memory** is a first-party cookie plus SQLite. After each call the transcript
is summarised and stored against the visitor. On their next call the agent
greets them as a returning caller and does not re-ask for details it already
has. Nothing about this runs during the call, so it costs no latency.

## Lead capture

After hangup, the transcript is passed through an extraction step that pulls
name, email, phone, company, problem and interest into `leads`, with a regex
fallback that reconstructs emails people spell out loud
("bob at gmail dot com" → `bob@gmail.com`). If `SHEETS_WEBHOOK_URL` is set they
are also appended to a Google Sheet — see `sheets-webhook.gs`.

## Project layout

```
app/
  main.py       FastAPI app, session issuing, the call websocket, admin
  config.py     every setting, all from environment variables
  security.py   Turnstile, signed tokens, quotas, origin checks
  stt.py        AssemblyAI temporary tokens
  llm.py        Pollinations streaming + clause splitting + reply cap
  tts.py        Fish Audio websocket synthesis
  fillers.py    pre-rendered acknowledgement clips
  knowledge.py  markdown loading and BM25 retrieval
  store.py      SQLite: visitors, sessions, transcripts, leads, usage
  leads.py      post-call summary, extraction and Sheets sync
  prompts.py    persona and prompt assembly
static/         app.js (client), pcm-worklet.js (capture), style.css
templates/      index.html (call UI), admin.html (dashboard)
knowledge/      your business knowledge, as markdown
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
microphone works without HTTPS. **On any other host you must have HTTPS** or the
browser will refuse microphone access — Coolify handles that for you.

## Tuning

| Setting | Effect |
|---|---|
| `POLLINATIONS_MODEL` | `gemini-fast` is the default: shortest replies at equal speed. `mistral-small-3.2` and `YoannDev90/mistral-glm-5.2:free` also work. |
| `MAX_REPLY_CHARS` | Hard cap on how much the agent says per turn. |
| `FILLER_BRIDGE_AFTER` | How long to wait before adding a second filler clip. |
| `ENABLE_FILLERS` | Set `false` to hear the raw latency. |
| `SESSION_MAX_SECONDS` | Hard call length. AssemblyAI enforces this too. |
| `GLOBAL_STT_SECONDS_PER_DAY` | Daily speech-to-text budget. 3600 stretches a 300-hour allowance across ~300 days. |

Changing the filler phrases or the voice automatically re-renders the clips —
they are cached under a fingerprint of the phrases, voice and sample rate.
