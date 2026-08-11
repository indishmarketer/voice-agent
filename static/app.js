/**
 * Voice agent client.
 *
 * Two sockets run at once:
 *   sttSocket    -> AssemblyAI, opened with a one-time token from our server.
 *                   Receives partial and final transcripts.
 *   agentSocket  -> our server. Sends final transcripts, receives the reply as
 *                   raw PCM frames that start playing before the model has
 *                   finished writing.
 *
 * Audio is only forwarded to AssemblyAI while someone is actually speaking. On
 * a normal call that is roughly a third of the wall-clock time, which is the
 * single biggest saving on the free speech-to-text tier.
 */
(() => {
  "use strict";

  const STT_RATE = 16000;
  const BLOCK_MS = 50;
  const PREROLL_BLOCKS = 6;      // 300 ms of audio kept before speech starts
  const HANGOVER_BLOCKS = 22;    // ~1.1 s of trailing audio so AAI ends the turn
  const SPEECH_ONSET_BLOCKS = 2;
  const BARGE_ONSET_BLOCKS = 5;  // stricter while the agent is talking
  const BASE_THRESHOLD = 0.012;
  const JITTER_BUFFER = 0.12;    // seconds of audio queued before playback

  const el = (id) => document.getElementById(id);
  const dialScreen = el("dial-screen");
  const callScreen = el("call-screen");
  const btnDial = el("btn-dial");
  const btnEnd = el("btn-end");
  const btnMute = el("btn-mute");
  const statusEl = el("call-status");
  const wavesEl = el("audio-waves");
  const waveBars = Array.from(wavesEl.querySelectorAll(".wave"));
  const orbEl = el("agent-orb");
  const transcriptEl = el("transcript-text");
  const timerEl = el("call-timer");
  const noticeEl = el("notice");

  const state = {
    active: false,
    muted: false,
    session: null,
    sttSocket: null,
    agentSocket: null,
    micStream: null,
    captureCtx: null,
    workletNode: null,
    micAnalyser: null,
    micFreqData: null,
    playCtx: null,
    playGain: null,
    playAnalyser: null,
    playFreqData: null,
    nextPlayTime: 0,
    scheduled: [],
    preroll: [],
    voiced: 0,
    silence: 0,
    streaming: false,
    sentBlocks: 0,
    noiseFloor: 0.004,
    agentSpeaking: false,
    timerHandle: null,
    keepAliveHandle: null,
    visualizerHandle: null,
    endsAt: 0,
    finished: false,
  };

  // --- UI helpers -----------------------------------------------------------

  function setStatus(text, mode) {
    statusEl.textContent = text;
    wavesEl.className = "audio-waves" + (mode ? " " + mode : "");
  }

  function showTranscript(text, who) {
    transcriptEl.textContent = text;
    transcriptEl.className = "transcript-text " + (who || "");
  }

  function notify(message, isError) {
    noticeEl.textContent = message || "";
    noticeEl.className = "notice" + (message ? " visible" : "") +
      (isError ? " error" : "");
  }

  function startTimer(maxSeconds) {
    state.endsAt = Date.now() + maxSeconds * 1000;
    const tick = () => {
      const left = Math.max(0, Math.round((state.endsAt - Date.now()) / 1000));
      const m = String(Math.floor(left / 60)).padStart(2, "0");
      const s = String(left % 60).padStart(2, "0");
      timerEl.textContent = `${m}:${s}`;
      timerEl.classList.toggle("warning", left <= 30);
    };
    tick();
    state.timerHandle = setInterval(tick, 500);
  }

  // --- Playback -------------------------------------------------------------

  function ensurePlayback() {
    if (state.playCtx) return;
    state.playCtx = new (window.AudioContext || window.webkitAudioContext)();
    state.playGain = state.playCtx.createGain();
    state.playGain.connect(state.playCtx.destination);

    // Tapped for the visualizer only - it does not affect what is heard.
    state.playAnalyser = state.playCtx.createAnalyser();
    state.playAnalyser.fftSize = 256;
    state.playAnalyser.smoothingTimeConstant = 0.6;
    state.playFreqData = new Uint8Array(state.playAnalyser.frequencyBinCount);
    state.playGain.connect(state.playAnalyser);

    state.nextPlayTime = 0;
  }

  function enqueueAudio(arrayBuffer) {
    ensurePlayback();
    const ctx = state.playCtx;
    const pcm = new Int16Array(arrayBuffer);
    if (!pcm.length) return;

    const rate = state.session.audio_sample_rate;
    const buffer = ctx.createBuffer(1, pcm.length, rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(state.playGain);

    const earliest = ctx.currentTime + JITTER_BUFFER;
    const startAt = Math.max(earliest, state.nextPlayTime);
    source.start(startAt);
    state.nextPlayTime = startAt + buffer.duration;
    state.agentSpeaking = true;

    state.scheduled.push(source);
    source.onended = () => {
      state.scheduled = state.scheduled.filter((s) => s !== source);
      if (state.nextPlayTime <= ctx.currentTime + 0.05) {
        state.agentSpeaking = false;
      }
    };
  }

  function stopPlayback() {
    state.scheduled.forEach((source) => {
      try { source.stop(); } catch (_) { /* already finished */ }
    });
    state.scheduled = [];
    state.agentSpeaking = false;
    if (state.playCtx) state.nextPlayTime = state.playCtx.currentTime;
  }

  // --- Visualizer -------------------------------------------------------
  //
  // The orb glow and the wave bars are driven every frame from real audio
  // amplitude - the caller's own mic while listening, the synthesized speech
  // while the agent talks - not a canned CSS loop. Nothing else on the page
  // animates on its own; the ambient background blobs are the only exception,
  // and those are decorative and independent of the call.

  const BAR_COUNT = waveBars.length;
  const barLevels = new Array(BAR_COUNT).fill(0);
  let orbLevel = 0;

  function bandLevel(freqData, band, bands) {
    // Ignore the very top of the spectrum - it is mostly noise for both a
    // 16 kHz mic feed and 24 kHz speech, and skip bin 0 (DC offset).
    const usable = Math.max(2, Math.floor(freqData.length * 0.65));
    const start = Math.max(1, Math.floor((band / bands) * usable));
    const end = Math.max(start + 1, Math.floor(((band + 1) / bands) * usable));
    let sum = 0;
    for (let i = start; i < end; i++) sum += freqData[i];
    return sum / (end - start) / 255;
  }

  function visualizerFrame() {
    let source = null;
    if (state.agentSpeaking && state.playAnalyser) {
      state.playAnalyser.getByteFrequencyData(state.playFreqData);
      source = state.playFreqData;
    } else if (state.streaming && state.micAnalyser) {
      state.micAnalyser.getByteFrequencyData(state.micFreqData);
      source = state.micFreqData;
    }

    for (let i = 0; i < BAR_COUNT; i++) {
      const raw = source ? bandLevel(source, i, BAR_COUNT) : 0;
      // sqrt gives a perceptual curve - quiet sounds are still visible.
      const target = Math.sqrt(raw);
      // Fast attack, slower release, so bars react instantly but don't flicker.
      const rate = target > barLevels[i] ? 0.55 : 0.14;
      barLevels[i] += (target - barLevels[i]) * rate;
      waveBars[i].style.height = (8 + barLevels[i] * 44).toFixed(1) + "px";
    }

    const avg = barLevels.reduce((a, b) => a + b, 0) / BAR_COUNT;
    // A very low idle baseline while a call is connected keeps the orb from
    // looking dead between turns, without reading as "the whole UI moving".
    const baseline = state.active ? 0.045 : 0;
    orbLevel += (Math.max(avg, baseline) - orbLevel) * 0.18;
    if (orbEl) orbEl.style.setProperty("--level", orbLevel.toFixed(3));

    state.visualizerHandle = requestAnimationFrame(visualizerFrame);
  }

  function startVisualizer() {
    if (state.visualizerHandle) return;
    state.visualizerHandle = requestAnimationFrame(visualizerFrame);
  }

  function stopVisualizer() {
    if (state.visualizerHandle) cancelAnimationFrame(state.visualizerHandle);
    state.visualizerHandle = null;
    barLevels.fill(0);
    orbLevel = 0;
    waveBars.forEach((bar) => { bar.style.height = "8px"; });
    if (orbEl) orbEl.style.setProperty("--level", "0");
  }

  // --- Microphone capture and gating ---------------------------------------

  async function startCapture() {
    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,   // stops the agent hearing itself
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const Ctx = window.AudioContext || window.webkitAudioContext;
    // Asking for 16 kHz lets the browser resample for us, which is both faster
    // and more accurate than doing it in JavaScript.
    state.captureCtx = new Ctx({ sampleRate: STT_RATE });
    await state.captureCtx.audioWorklet.addModule("/static/pcm-worklet.js");

    const source = state.captureCtx.createMediaStreamSource(state.micStream);
    state.workletNode = new AudioWorkletNode(state.captureCtx, "pcm-worklet");
    state.workletNode.port.onmessage = (event) => onAudioBlock(event.data);
    source.connect(state.workletNode);
    // Keep the graph alive without routing the mic to the speakers.
    const silent = state.captureCtx.createGain();
    silent.gain.value = 0;
    state.workletNode.connect(silent).connect(state.captureCtx.destination);

    // Tapped for the visualizer only, in parallel with the worklet above.
    state.micAnalyser = state.captureCtx.createAnalyser();
    state.micAnalyser.fftSize = 64;
    state.micAnalyser.smoothingTimeConstant = 0.5;
    state.micFreqData = new Uint8Array(state.micAnalyser.frequencyBinCount);
    source.connect(state.micAnalyser);
  }

  function onAudioBlock({ pcm, rms }) {
    if (!state.active || state.muted) return;

    // Slowly track the room's noise floor so the gate adapts to the caller.
    if (!state.streaming && !state.agentSpeaking) {
      state.noiseFloor = state.noiseFloor * 0.97 + rms * 0.03;
    }

    const threshold = Math.max(BASE_THRESHOLD, state.noiseFloor * 3.5) *
      (state.agentSpeaking ? 2.6 : 1);
    const onset = state.agentSpeaking ? BARGE_ONSET_BLOCKS : SPEECH_ONSET_BLOCKS;
    const loud = rms > threshold;

    if (loud) {
      state.voiced++;
      state.silence = 0;
    } else {
      state.silence++;
      if (state.silence > 3) state.voiced = 0;
    }

    if (!state.streaming) {
      // Hold a short pre-roll so the first word is never clipped.
      state.preroll.push(pcm);
      if (state.preroll.length > PREROLL_BLOCKS) state.preroll.shift();

      if (state.voiced >= onset) {
        if (state.agentSpeaking) bargeIn();
        state.streaming = true;
        state.preroll.forEach(sendAudio);
        state.preroll = [];
      }
      return;
    }

    sendAudio(pcm);

    // Keep sending through the pause so AssemblyAI's endpointer fires, then
    // close the gate again.
    if (state.silence >= HANGOVER_BLOCKS) {
      state.streaming = false;
      state.voiced = 0;
      state.preroll = [];
    }
  }

  function sendAudio(buffer) {
    const socket = state.sttSocket;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(buffer);
    state.sentBlocks++;
  }

  function bargeIn() {
    stopPlayback();
    send(state.agentSocket, { type: "barge_in" });
    setStatus("Listening…", "listening");
  }

  // --- Sockets --------------------------------------------------------------

  function send(socket, payload) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }
  }

  function openSttSocket() {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(state.session.stt_ws_url);
      socket.binaryType = "arraybuffer";
      const failed = setTimeout(() => reject(new Error("stt timeout")), 10000);

      socket.onopen = () => {
        clearTimeout(failed);
        state.sttSocket = socket;
        // AssemblyAI drops idle sessions; our gate creates long idle stretches.
        state.keepAliveHandle = setInterval(() => {
          if (!state.streaming) send(socket, { type: "KeepAlive" });
        }, 10000);
        resolve(socket);
      };

      socket.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch (_) { return; }
        if (data.type === "Turn") onTurn(data);
      };

      socket.onerror = () => { clearTimeout(failed); reject(new Error("stt error")); };
      socket.onclose = () => {
        if (state.active && !state.finished) {
          notify("Speech recognition disconnected.", true);
        }
      };
    });
  }

  function onTurn(data) {
    const text = (data.transcript || "").trim();
    if (!text) return;

    if (!data.end_of_turn) {
      showTranscript(text, "user interim");
      return;
    }
    const finalText = (data.utterance || text).trim();
    if (!finalText) return;

    showTranscript(finalText, "user");
    setStatus("Thinking…", "thinking");
    send(state.agentSocket, { type: "user_turn", text: finalText });
  }

  function openAgentSocket() {
    return new Promise((resolve, reject) => {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const url = `${scheme}://${location.host}/ws/agent?t=` +
        encodeURIComponent(state.session.session_token);
      const socket = new WebSocket(url);
      socket.binaryType = "arraybuffer";
      const failed = setTimeout(() => reject(new Error("agent timeout")), 10000);

      socket.onopen = () => {
        clearTimeout(failed);
        state.agentSocket = socket;
        resolve(socket);
      };
      socket.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          enqueueAudio(event.data);
        } else {
          let data;
          try { data = JSON.parse(event.data); } catch (_) { return; }
          onAgentMessage(data);
        }
      };
      socket.onerror = () => { clearTimeout(failed); reject(new Error("agent error")); };
      socket.onclose = () => {
        if (state.active && !state.finished) endCall("Call ended.");
      };
    });
  }

  function onAgentMessage(data) {
    switch (data.type) {
      case "agent_start":
        setStatus("Speaking…", "speaking");
        break;
      case "agent_text":
        showTranscript(data.text, "agent");
        break;
      case "agent_done":
        setStatus("Listening…", "listening");
        break;
      case "status":
        if (data.state === "listening") setStatus("Listening…", "listening");
        if (data.state === "thinking") setStatus("Thinking…", "thinking");
        break;
      case "ended":
        state.finished = true;
        notify(data.message || "Call ended.");
        setTimeout(() => endCall(data.message), 1200);
        break;
      case "error":
        notify(data.message || "Something went wrong.", true);
        break;
    }
  }

  function reportUsage() {
    send(state.agentSocket, {
      type: "stt_usage",
      seconds: (state.sentBlocks * BLOCK_MS) / 1000,
    });
  }

  // --- Turnstile --------------------------------------------------------
  //
  // Rendered ONCE, up front, in "execute" mode. Re-rendering into the same
  // div on every dial click (the old approach) throws on the second attempt,
  // which silently produced an empty token forever after. Here we render one
  // widget and call turnstile.execute() per attempt instead.
  //
  // If verification keeps failing, open devtools -> Console: the warning
  // below prints Cloudflare's actual error code, which is almost always a
  // domain mismatch between this page's origin and the domain list configured
  // on the Turnstile site key in the Cloudflare dashboard.

  let turnstileWidgetId = null;
  let turnstilePending = null;

  function renderTurnstile() {
    turnstileWidgetId = window.turnstile.render("#turnstile-holder", {
      sitekey: window.__TURNSTILE_SITE_KEY__,
      size: "invisible",
      execution: "execute",
      callback: (token) => {
        if (turnstilePending) { turnstilePending(token); turnstilePending = null; }
      },
      "error-callback": (code) => {
        console.warn("[Turnstile] verification failed, code:", code,
          "- if this persists, check the domain list on this site key in "
          + "the Cloudflare dashboard.");
        if (turnstilePending) { turnstilePending(""); turnstilePending = null; }
      },
      "expired-callback": () => {
        if (turnstilePending) { turnstilePending(""); turnstilePending = null; }
      },
    });
  }

  function initTurnstile() {
    if (!window.__TURNSTILE_SITE_KEY__) return;
    // The CDN script calls window.onTurnstileLoad itself once it is truly
    // ready (see templates/index.html) - that beats guessing with a timer
    // or with turnstile.ready(), which can fire before api.js has finished
    // installing the real implementation.
    if (window.__turnstileLoaded) {
      renderTurnstile();
    } else {
      window.__onTurnstileReady = renderTurnstile;
    }
  }

  async function getTurnstileToken() {
    if (!window.__TURNSTILE_SITE_KEY__) return "";

    // Slow connection: the CDN script may not have finished by the time
    // someone clicks Dial. Give it a couple of seconds rather than silently
    // sending an unverified request (which the server would then reject).
    for (let waited = 0; turnstileWidgetId === null && waited < 3000; waited += 100) {
      await new Promise((r) => setTimeout(r, 100));
    }
    if (turnstileWidgetId === null || !window.turnstile) {
      console.warn("[Turnstile] widget never became ready.");
      return "";
    }
    return new Promise((resolve) => {
      turnstilePending = resolve;
      try {
        window.turnstile.reset(turnstileWidgetId);
        window.turnstile.execute(turnstileWidgetId);
      } catch (err) {
        console.warn("[Turnstile] execute() threw:", err);
        turnstilePending = null;
        resolve("");
        return;
      }
      setTimeout(() => {
        if (turnstilePending === resolve) {
          console.warn("[Turnstile] timed out waiting for a token.");
          turnstilePending = null;
          resolve("");
        }
      }, 8000);
    });
  }

  initTurnstile();

  // --- Call lifecycle -------------------------------------------------------

  async function requestSession() {
    const turnstileToken = await getTurnstileToken();
    const response = await fetch("/api/session/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turnstile_token: turnstileToken }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "Could not start the call.");
    return data;
  }

  async function startCall() {
    btnDial.disabled = true;
    notify("");
    dialScreen.classList.remove("active");
    callScreen.classList.add("active");
    setStatus("Connecting…", "");
    showTranscript("Dialling…", "");

    try {
      state.session = await requestSession();
      state.active = true;
      state.finished = false;
      state.sentBlocks = 0;

      ensurePlayback();
      if (state.playCtx.state === "suspended") await state.playCtx.resume();

      await startCapture();
      await Promise.all([openSttSocket(), openAgentSocket()]);

      startTimer(state.session.max_seconds);
      startVisualizer();
      setStatus("Connected", "");
      send(state.agentSocket, { type: "start" });
    } catch (error) {
      const message = error && error.message
        ? error.message
        : "Could not start the call.";
      notify(message, true);
      endCall(message);
    } finally {
      btnDial.disabled = false;
    }
  }

  function endCall(message) {
    if (!state.active && !state.session) {
      callScreen.classList.remove("active");
      dialScreen.classList.add("active");
      return;
    }
    state.active = false;

    reportUsage();
    send(state.agentSocket, { type: "end" });

    clearInterval(state.timerHandle);
    clearInterval(state.keepAliveHandle);
    stopVisualizer();
    stopPlayback();

    if (state.sttSocket && state.sttSocket.readyState === WebSocket.OPEN) {
      try { state.sttSocket.send(JSON.stringify({ type: "Terminate" })); } catch (_) {}
    }
    [state.sttSocket, state.agentSocket].forEach((socket) => {
      if (socket) { try { socket.close(); } catch (_) {} }
    });
    state.sttSocket = null;
    state.agentSocket = null;

    if (state.workletNode) { try { state.workletNode.disconnect(); } catch (_) {} }
    if (state.captureCtx) { try { state.captureCtx.close(); } catch (_) {} }
    if (state.micStream) state.micStream.getTracks().forEach((t) => t.stop());
    state.workletNode = null;
    state.captureCtx = null;
    state.micStream = null;
    state.micAnalyser = null;
    state.session = null;
    state.preroll = [];
    state.streaming = false;
    state.muted = false;
    btnMute.classList.remove("muted");

    callScreen.classList.remove("active");
    dialScreen.classList.add("active");
    if (message) notify(message);
  }

  // --- Wiring ---------------------------------------------------------------

  btnDial.addEventListener("click", () => {
    if (!navigator.mediaDevices || !window.AudioWorklet) {
      notify("This browser cannot run the voice agent. Try Chrome, Edge or Safari.",
        true);
      return;
    }
    startCall();
  });

  btnEnd.addEventListener("click", () => endCall(""));

  btnMute.addEventListener("click", () => {
    state.muted = !state.muted;
    btnMute.classList.toggle("muted", state.muted);
    if (state.muted) {
      state.streaming = false;
      state.preroll = [];
      setStatus("Muted", "");
    } else {
      setStatus("Listening…", "listening");
    }
  });

  window.addEventListener("beforeunload", () => {
    if (state.active) { reportUsage(); send(state.agentSocket, { type: "end" }); }
  });
})();
