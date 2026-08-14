# Architecture reference

Technical detail behind the overview in [README.md](README.md). Read this if you
are changing the code; read `DEPLOY.md` if you just want it running.

## Processes and hops

The browser holds **two** websockets at once.

**1. Browser → AssemblyAI (direct).** Audio never passes through our server.
That removes a hop from the latency path and keeps the container almost idle,
which matters on a small Coolify box. The API key is protected because the
browser is given a **temporary token** instead:

```
GET https://streaming.assemblyai.com/v3/token
    ?expires_in_seconds=60&max_session_duration_seconds=180
Authorization: <api key>          ← no "Bearer" prefix on this endpoint
```

The token is single-use and short-lived. Critically,
`max_session_duration_seconds` is enforced *by AssemblyAI*, so the worst case
for a leaked token is one short session — this is the backbone of the cost
protection, not our own timers.

Connection URL:

```
wss://streaming.assemblyai.com/v3/ws
    ?token=…&sample_rate=16000&encoding=pcm_s16le
    &speech_model=universal-3-5-pro&format_turns=true
    &end_of_turn_confidence_threshold=0.4
    &min_turn_silence=400&max_turn_silence=1100
```

`min_turn_silence=400` is the main turn-latency dial. Lower it and the agent
interrupts people; raise it and the agent feels sluggish.

**2. Browser → our FastAPI server.** JSON control messages up, JSON status and
binary PCM down.

| Direction | Message |
|---|---|
| → | `{"type":"start"}` — play the greeting |
| → | `{"type":"user_turn","text":…}` — a finalised transcript |
| → | `{"type":"barge_in"}` — caller interrupted, abandon the reply |
| → | `{"type":"stt_usage","seconds":n}` — usage accounting |
| → | `{"type":"end"}` |
| ← | `{"type":"agent_start"}` / `{"type":"agent_text",…}` / `{"type":"agent_done"}` |
| ← | `{"type":"status","state":"thinking"｜"listening"}` |
| ← | `{"type":"ended","reason":…,"message":…}` |
| ← | binary frames: signed 16-bit little-endian mono PCM at `FISH_SAMPLE_RATE` |

Raw PCM is used rather than MP3 deliberately: it needs no Media Source
Extensions (patchy on iOS), no container parsing, and can be handed straight to
the Web Audio API.

## Audio capture

`static/pcm-worklet.js` runs on the audio thread. It buffers 800 samples (50 ms
at 16 kHz), converts to Int16 and reports the block's RMS. The gating decision
lives on the main thread in `app.js`, so it can take account of whether the
agent is currently speaking.

The capture `AudioContext` is created with `{ sampleRate: 16000 }` so the
browser resamples natively — faster and more accurate than doing it in JS.

**The voice gate** is what protects the free tier:

- Noise floor is tracked continuously while idle, threshold =
  `max(0.012, noiseFloor × 3.5)`.
- Two consecutive loud blocks open the gate; a 6-block (300 ms) pre-roll ring
  buffer is flushed first so the first word survives.
- The gate stays open for 22 blocks (~1.1 s) after speech stops, because
  AssemblyAI needs to *hear* the silence to fire `end_of_turn`.
- While the agent is speaking the threshold is multiplied by 2.6 and the onset
  requirement rises to 5 blocks — echo cancellation handles most feedback, this
  covers the rest.
- `KeepAlive` is sent every 10 s while the gate is shut so AssemblyAI does not
  drop the session.

## Playback

Each PCM frame becomes an `AudioBuffer` scheduled at
`max(currentTime + 0.12, nextPlayTime)`, and `nextPlayTime` advances by the
buffer's duration. That 120 ms jitter buffer absorbs network variance without
being audible. `AudioBuffer` carries its own sample rate, so Web Audio resamples
to the device rate for free.

Barge-in stops every scheduled source and resets `nextPlayTime`, so audio cuts
instantly on the client while the server tears down the upstream stream.

## The turn pipeline

In `app/tts.py::speak`, the Fish Audio socket connect (~0.85 s) and the first
Pollinations token (~1.3 s) are started **concurrently**:

```python
open_task = asyncio.create_task(stream.open())
# pump_text() begins consuming the LLM immediately and only awaits
# open_task just before it needs to send the first clause
```

Doing these sequentially — which is what happens if you `await open()` before
touching the lazy LLM generator — silently adds most of a second to every turn.

`app/llm.py::sentences` regroups the token stream into speakable clauses: a
sentence end of at least 8 characters, or a comma once 60 characters have built
up. `sentences` is wrapped in `limit()`, a hard character cap that stops a
verbose model mid-reply.

Fish Audio protocol (MessagePack over websocket, `wss://api.fish.audio/v1/tts/live`):

```
{"event":"start","request":{"text":"","reference_id":…,"format":"pcm",
                            "sample_rate":24000,"latency":"balanced", …}}
{"event":"text","text":"…"}     one per clause
{"event":"flush"}               forces synthesis without waiting for the buffer
{"event":"stop"}                drain and close
```

Server replies with `{"event":"audio","audio":<bytes>}` frames then
`{"event":"finish","reason":"stop"}`.

`latency: "balanced"` measures at ~0.62 s to first audio; `"normal"` at ~1.68 s.
Use `balanced`.

## Concurrency model

`_run_call` is a pure receive loop. Turns run as **background tasks**
(`call.turn`) rather than being awaited inline — otherwise the loop cannot read
a `barge_in` until the turn it should be cancelling has already finished.
`Call.stop_speaking()` cancels the synthesis task first, then the turn that owns
it.

A `_watchdog` task closes the socket at `SESSION_MAX_SECONDS` regardless of what
the client does, and `receive_json` carries an idle timeout.

Everything after hangup — summarising, lead extraction — is fired into a
detached task so it never touches the call's latency.

## Filler clips

`app/fillers.py` renders short acknowledgements once and caches them as raw PCM
on the data volume. The cache directory is named after a SHA-1 of the phrase
list, voice ID and sample rate, so editing any of them re-renders rather than
serving a stale clip under a reused index.

Two tiers: an **ack** (~0.7 s) goes out the instant the turn starts, and a
**bridge** (~1.2 s) follows only if no real audio has been produced after
`FILLER_BRIDGE_AFTER` seconds. Together they cover roughly 3 s of model latency,
which is more than the measured worst case.

Clips are cached **per voice**, keyed on `(voice_id, phrase_index)`, since
sub-accounts can pick from four voice presets (or, on the one-time plan,
their own voice entirely). `warm()` pre-renders every reachable voice at
startup so no account's first call ever waits on a cold filler cache; a
voice with nothing cached yet falls back to the default voice's clip rather
than dead air.

## Storage

SQLite with WAL, on the `/app/data` volume. Tables: `accounts`, `visitors`,
`sessions`, `turns`, `leads`, `settings`, `login_tokens`, `support_reports`.
IP addresses are stored only as a keyed HMAC hash.

`account_id` is `NULL` throughout for the original single-tenant agent - it
predates sub-accounts, so every table that gained the column got it via a
guarded `ALTER TABLE ... ADD COLUMN` in `store.init()`, run against the live
volume on every boot, not a migration tool. `settings` stays a plain
`(key, value)` table even for per-account overrides: a sub-account's key is
just namespaced (`f"acct{id}:{key}"`) rather than a schema change - see
`store._scoped_key`.

The app runs a **single uvicorn worker** on purpose — the concurrency cap, the
active-session set and SQLite writes are all in-process. Scaling out would need
those moved to Redis first.

## Multi-tenancy

Every request resolves an `account_id` (`None` = the main agent) before
anything else - `main.py::_resolve_account`, by `?account=<slug>` today,
by subdomain automatically once `PLATFORM_ROOT_DOMAIN` is set. That id
threads through everything downstream: `knowledge.get_kb(account_id)`,
`branding.get_branding(account_id)`, `prompts.build_system_prompt(...,
account_id)`, `voices.account_fish_model_id(account_id)`.

**Credentials are the one place this can't just be a function argument.**
`integrations.resolve_for_account(account_id, plan_type)` is resolved
**server-side, at WebSocket-connect time**, from the account row - never
carried in the client-visible session token. The token reaches whoever is
*on* the call (a website visitor, not necessarily the account owner), so
embedding a real provider API key in it would leak that key to every
caller. For every plan except `onetime` this resolves to the owner's global
keys (`integrations.py`'s plain env/settings getters); `onetime` resolves
to only that account's own stored keys, with no fallback - see
`integrations.py`'s module docstring for why a fallback would be wrong
here, not just redundant.

**Turnstile cannot gate an embeddable widget.** A Turnstile site key is
domain-allow-listed in the Cloudflare dashboard, and the widget itself does
not work reliably inside a cross-origin iframe on a third-party site
regardless - and there is no way to pre-register every future customer's
embedding domain with it. `embed.js` marks its own iframe's URL with
`&embed=1`; `index.html` skips loading Cloudflare's script entirely in that
mode, `app.js` skips requesting a token, and `/api/session/start` skips
requiring one - but does **not** skip the daily/IP/account quotas, which
are the actual abuse protection on that path.

## Deliberate trade-offs

- **Direct browser→AssemblyAI** trades exact server-side second counting for
  lower latency. Usage is client-reported; the real guarantee is the token's
  session cap plus the quota on token minting.
- **BM25 rather than embeddings** — no API call, no cost, no latency, and for a
  knowledge base of this size the quality difference is negligible.
- **Cookie-based visitor identity** is spoofable. It is for continuity, not
  security; the per-IP quota is the limit that actually binds.
