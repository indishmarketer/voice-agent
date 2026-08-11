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
    playCtx: null,
    playGain: null,
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

  // --- Call lifecycle -------------------------------------------------------

  async function requestSession() {
    let turnstileToken = "";
    if (window.__TURNSTILE_SITE_KEY__ && window.turnstile) {
      try {
        turnstileToken = await new Promise((resolve) => {
          window.turnstile.ready(() => {
            window.turnstile.render("#turnstile-holder", {
              sitekey: window.__TURNSTILE_SITE_KEY__,
              callback: resolve,
              "error-callback": () => resolve(""),
              size: "invisible",
            });
          });
          setTimeout(() => resolve(""), 8000);
        });
      } catch (_) { /* fall through, server decides */ }
    }

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
