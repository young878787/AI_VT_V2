/**
 * PCM 降採樣 AudioWorklet：
 * 將麥克風輸入降採樣為 16kHz mono Int16，湊滿 100ms 幀（1600 樣本）後
 * 以 Transferable ArrayBuffer 傳回主執行緒（供 voiceWsService 經 WebSocket 上送）。
 * 後端 ASR/VAD 需 16kHz（見 backend/infrastructure/vad_engine.py）。
 */
class PcmDownsamplerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._targetRate = 16000;
    this._ratio = sampleRate / this._targetRate; // 48000/16000 = 3
    this._frameSamples = 1600; // 100ms @ 16kHz
    this._carry = new Float32Array(0); // 不足一個 ratio 組的殘樣（跨 block 對齊用）
    this._frame = new Float32Array(this._frameSamples);
    this._fill = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length > 0) {
      const down = this._downsample(ch);
      for (let i = 0; i < down.length; i++) {
        this._frame[this._fill++] = down[i];
        if (this._fill === this._frameSamples) this._flush();
      }
    }
    return true;
  }

  _downsample(ch) {
    if (Number.isInteger(this._ratio)) {
      // 整數倍：相鄰 ratio 個樣本平均（品質優於直接抽點）
      const x = new Float32Array(this._carry.length + ch.length);
      x.set(this._carry, 0);
      x.set(ch, this._carry.length);
      const r = this._ratio;
      const n = Math.floor(x.length / r) * r;
      this._carry = x.slice(n);
      const out = new Float32Array(n / r);
      for (let i = 0, j = 0; i < n; i += r, j++) {
        let sum = 0;
        for (let k = 0; k < r; k++) sum += x[i + k];
        out[j] = sum / r;
      }
      return out;
    }
    // 非整數倍：線性內插重採樣（ASR 用途可接受）
    const outLen = Math.floor(ch.length / this._ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const pos = i * this._ratio;
      const i0 = Math.floor(pos);
      const frac = pos - i0;
      const a = ch[i0];
      const b = ch[Math.min(i0 + 1, ch.length - 1)];
      out[i] = a + (b - a) * frac;
    }
    return out;
  }

  _flush() {
    const pcm = new Int16Array(this._fill);
    for (let i = 0; i < this._fill; i++) {
      const s = Math.max(-1, Math.min(1, this._frame[i]));
      pcm[i] = s < 0 ? s * 32768 : s * 32767;
    }
    this._fill = 0;
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
  }
}

registerProcessor('pcm-downsampler', PcmDownsamplerProcessor);
