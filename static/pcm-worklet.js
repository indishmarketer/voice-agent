/**
 * Captures microphone audio, converts it to 16-bit PCM and reports loudness.
 *
 * The main thread decides whether a block is actually sent to AssemblyAI. Doing
 * the gating there rather than here keeps the audio thread cheap and lets the
 * gate react to whether the agent is currently talking.
 */
const BLOCK = 800; // 50 ms at 16 kHz

class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Float32Array(BLOCK);
    this._offset = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const channel = input[0];

    for (let i = 0; i < channel.length; i++) {
      this._buffer[this._offset++] = channel[i];
      if (this._offset === BLOCK) {
        this._emit();
        this._offset = 0;
      }
    }
    return true;
  }

  _emit() {
    let sumSquares = 0;
    const pcm = new Int16Array(BLOCK);
    for (let i = 0; i < BLOCK; i++) {
      const sample = Math.max(-1, Math.min(1, this._buffer[i]));
      sumSquares += sample * sample;
      pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }
    const rms = Math.sqrt(sumSquares / BLOCK);
    this.port.postMessage({ pcm: pcm.buffer, rms }, [pcm.buffer]);
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
