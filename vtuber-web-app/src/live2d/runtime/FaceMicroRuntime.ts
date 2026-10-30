export interface FaceMicroContext {
  enabled: boolean;
  mouthAllowed: boolean;
  pupilsAllowed: boolean;
  listening: boolean;
}

export type FaceMicroKind = 'smile' | 'flat' | 'pout';

export interface FaceMicroSample {
  mouthForm: number;
  mouthPout: number;
  mouthAngle: number;
  eyeBallForm: number;
}

interface MouthEvent {
  kind: FaceMicroKind;
  startedAtMs: number;
  inMs: number;
  holdMs: number;
  outMs: number;
  form: number;
  pout: number;
  angle: number;
}

// 嘴型小事件的間隔；尖嘴（Pout＋Angle）較搶眼，另設冷卻。
const EVENT_GAP_MS = [4000, 9000] as const;
const POUT_COOLDOWN_MS = 40000;
const MOUTH_FORM = { smile: [0.12, 0.28], flat: [0.1, 0.22] } as const;
const POUT = { amount: [0.55, 0.9], angle: [25, 50], holdMs: [700, 1500] } as const;
const MOUTH_RELEASE_TAU_MS = 120;
const PUPIL = { amplitude: 0.3, slowHz: [0.06, 0.12], fastHz: [0.17, 0.29], slowShare: 0.6, tauMs: 300 } as const;
const MAX_FRAME_GAP_MS = 1000;
const neutral = (): FaceMicroSample => ({ mouthForm: 0, mouthPout: 0, mouthAngle: 0, eyeBallForm: 0 });
const clamp = (value: number, minimum: number, maximum: number) => Math.max(minimum, Math.min(maximum, value));
const ease = (value: number) => value * value * (3 - 2 * value);

function eventWeight(event: MouthEvent, nowMs: number): number {
  const elapsed = Math.max(0, nowMs - event.startedAtMs);
  if (elapsed < event.inMs) return ease(elapsed / event.inMs);
  if (elapsed < event.inMs + event.holdMs) return 1;
  return 1 - ease(clamp((elapsed - event.inMs - event.holdMs) / event.outMs, 0, 1));
}

/**
 * 嘴與瞳孔的 idle 微動：只回傳相對表情基準的偏移。
 * 嘴型一次一個事件，被說話／表情接手時快速淡出；瞳孔是不依賴事件的慢速縮放。
 */
export class FaceMicroRuntime {
  private nowMs = 0;
  private hasUpdated = false;
  private wasEnabled = false;
  private nextEventAtMs = Infinity;
  private poutAllowedAtMs = -Infinity;
  private event: MouthEvent | null = null;
  private mouthGain = 0;
  private pupilGain = 0;
  private readonly pupilWave: { slowHz: number; fastHz: number; slowPhase: number; fastPhase: number };

  constructor(private readonly random: () => number = Math.random) {
    this.pupilWave = {
      slowHz: this.between(...PUPIL.slowHz), fastHz: this.between(...PUPIL.fastHz),
      slowPhase: this.between(0, Math.PI * 2), fastPhase: this.between(0, Math.PI * 2),
    };
  }

  reset(): void {
    this.nowMs = 0;
    this.hasUpdated = false;
    this.wasEnabled = false;
    this.nextEventAtMs = Infinity;
    this.poutAllowedAtMs = -Infinity;
    this.event = null;
    this.mouthGain = 0;
    this.pupilGain = 0;
  }

  update(dtSeconds: number, nowMs: number, context: FaceMicroContext): FaceMicroSample {
    const dtMs = Number.isFinite(dtSeconds) ? Math.max(0, dtSeconds) * 1000 : 0;
    const now = Number.isFinite(nowMs) ? nowMs : this.nowMs + dtMs;
    const interrupted = this.hasUpdated && (now < this.nowMs || now - this.nowMs > MAX_FRAME_GAP_MS || dtMs > MAX_FRAME_GAP_MS);
    if (now < this.nowMs) this.poutAllowedAtMs += now - this.nowMs;
    this.nowMs = now;
    this.hasUpdated = true;

    if (!context.enabled) {
      this.event = null;
      this.nextEventAtMs = Infinity;
      this.wasEnabled = false;
      this.mouthGain = 0;
      this.pupilGain = 0;
      return neutral();
    }
    if (!this.wasEnabled || interrupted) {
      this.event = null;
      this.mouthGain = 0;
      this.pupilGain = 0;
      this.nextEventAtMs = now + this.between(...EVENT_GAP_MS);
      this.wasEnabled = true;
      return neutral();
    }

    this.mouthGain = this.follow(this.mouthGain, context.mouthAllowed ? 1 : 0, dtMs, MOUTH_RELEASE_TAU_MS);
    this.pupilGain = this.follow(this.pupilGain, context.pupilsAllowed ? 1 : 0, dtMs, PUPIL.tauMs);

    if (this.event && now >= this.event.startedAtMs + this.event.inMs + this.event.holdMs + this.event.outMs) {
      this.nextEventAtMs = now + this.between(...EVENT_GAP_MS);
      this.event = null;
    }
    if (this.event && !context.mouthAllowed && this.mouthGain < 0.02) {
      // 被說話或表情接手：淡出後丟掉事件，交還後等一個完整間隔才再排。
      this.event = null;
      this.nextEventAtMs = now + this.between(...EVENT_GAP_MS);
    }
    if (!this.event && context.mouthAllowed && now >= this.nextEventAtMs) {
      this.event = this.createEvent(this.nextEventAtMs, context);
      if (this.event) {
        if (this.event.kind === 'pout') this.poutAllowedAtMs = this.nextEventAtMs + POUT_COOLDOWN_MS;
      } else this.nextEventAtMs += this.between(...EVENT_GAP_MS);
    }

    const sample = neutral();
    if (this.event) {
      const weight = eventWeight(this.event, now) * this.mouthGain;
      sample.mouthForm = this.event.form * weight;
      sample.mouthPout = this.event.pout * weight;
      sample.mouthAngle = this.event.angle * weight;
    }
    const wave = this.pupilWave;
    const seconds = now / 1000;
    sample.eyeBallForm = PUPIL.amplitude * this.pupilGain * (
      PUPIL.slowShare * Math.sin(Math.PI * 2 * wave.slowHz * seconds + wave.slowPhase)
      + (1 - PUPIL.slowShare) * Math.sin(Math.PI * 2 * wave.fastHz * seconds + wave.fastPhase));
    return sample;
  }

  private createEvent(startedAtMs: number, context: FaceMicroContext): MouthEvent | null {
    const pick = this.between(0, 1);
    let kind: FaceMicroKind = pick < 0.4 ? 'smile' : pick < 0.75 ? 'flat' : 'pout';
    if (kind === 'pout' && (context.listening || startedAtMs < this.poutAllowedAtMs)) kind = 'flat';
    const event: MouthEvent = {
      kind, startedAtMs, form: 0, pout: 0, angle: 0,
      inMs: this.between(600, 1100),
      holdMs: kind === 'pout' ? this.between(...POUT.holdMs) : this.between(900, 2200),
      outMs: this.between(700, 1300),
    };
    if (kind === 'pout') {
      event.pout = this.between(...POUT.amount);
      event.angle = this.between(...POUT.angle);
    } else {
      const [minimum, maximum] = MOUTH_FORM[kind];
      event.form = (kind === 'smile' ? 1 : -1) * this.between(minimum, maximum);
    }
    return event;
  }

  private follow(current: number, target: number, dtMs: number, tauMs: number): number {
    return current + (target - current) * (1 - Math.exp(-dtMs / tauMs));
  }

  private between(minimum: number, maximum: number): number {
    const value = this.random();
    return minimum + (Number.isFinite(value) ? clamp(value, 0, 1) : 0.5) * (maximum - minimum);
  }
}
