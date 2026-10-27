export interface FaceIdleContext {
  enabled: boolean;
  eyesAllowed: boolean;
  browsAllowed: boolean;
  listening: boolean;
  blinkPaused: boolean;
  blinkReady: boolean;
  eyeLOpen: number;
  eyeROpen: number;
}

export type FaceIdleKind = 'still' | 'relax' | 'half' | 'deep' | 'asymmetric' | 'brow' | 'winkLeft' | 'winkRight';

export interface FaceIdleSample {
  eyeL: number;
  eyeR: number;
  browL: number;
  browR: number;
  winking: boolean;
  kind: FaceIdleKind;
}

interface FaceEvent {
  kind: FaceIdleKind;
  startedAtMs: number;
  closingMs: number;
  holdMs: number;
  openingMs: number;
  eyeL: number;
  eyeR: number;
  browL: number;
  browR: number;
  eyes: boolean;
  brows: boolean;
}

const WINK_COOLDOWN_MS = 45000;
// 事件之間的間隔；臉部細節維持常見但不密集。
const EVENT_GAP_MS = [3000, 7000] as const;
const MAX_FRAME_GAP_MS = 1000;
const neutral = (): FaceIdleSample => ({ eyeL: 1, eyeR: 1, browL: 0, browR: 0, winking: false, kind: 'still' });
const clamp = (value: number, minimum: number, maximum: number) => Math.max(minimum, Math.min(maximum, value));
const ease = (value: number) => value * value * (3 - 2 * value);
const isWink = (kind: FaceIdleKind) => kind === 'winkLeft' || kind === 'winkRight';
const eyeBase = (value: number) => Number.isFinite(value) ? clamp(value, 0, 1) : 0;

function eventWeight(event: FaceEvent, nowMs: number): number {
  const elapsed = Math.max(0, nowMs - event.startedAtMs);
  if (elapsed < event.closingMs) return ease(elapsed / event.closingMs);
  if (elapsed < event.closingMs + event.holdMs) return 1;
  return 1 - ease(clamp((elapsed - event.closingMs - event.holdMs) / event.openingMs, 0, 1));
}

/** 自主臉部只持有單一事件；回傳相對表情基準的乘數／偏移，不累加模型參數。 */
export class FaceIdleRuntime {
  private nowMs = 0;
  private hasUpdated = false;
  private wasEnabled = false;
  private nextEventAtMs = Infinity;
  private eyesResumeAtMs = -Infinity;
  private winkAllowedAtMs: number | null = null;
  private lastWink: 'left' | 'right' | null = null;
  private event: FaceEvent | null = null;

  constructor(private readonly random: () => number = Math.random) {}

  reset(): void {
    this.nowMs = 0;
    this.hasUpdated = false;
    this.wasEnabled = false;
    this.nextEventAtMs = Infinity;
    this.eyesResumeAtMs = -Infinity;
    this.winkAllowedAtMs = null;
    this.lastWink = null;
    this.event = null;
  }

  cancelEyes(nowMs: number): void {
    const now = Number.isFinite(nowMs) ? nowMs : this.nowMs;
    // 明確眨眼／暫停後，至少等一個完整新間隔才再排自主眼皮。
    this.eyesResumeAtMs = now + this.between(...EVENT_GAP_MS);
    if (this.event) {
      this.event.eyes = false;
      if (!this.event.brows) this.event = null;
    }
    if (!this.event) this.nextEventAtMs = this.eyesResumeAtMs;
  }

  update(dtSeconds: number, nowMs: number, context: FaceIdleContext): FaceIdleSample {
    const dtMs = Number.isFinite(dtSeconds) ? Math.max(0, dtSeconds) * 1000 : 0;
    const now = Number.isFinite(nowMs) ? nowMs : this.nowMs + dtMs;
    const interrupted = this.hasUpdated && (now < this.nowMs || now - this.nowMs > MAX_FRAME_GAP_MS || dtMs > MAX_FRAME_GAP_MS);
    if (now < this.nowMs) {
      const shift = now - this.nowMs;
      this.eyesResumeAtMs += shift;
      if (this.winkAllowedAtMs !== null) this.winkAllowedAtMs += shift;
    }
    this.nowMs = now;
    this.hasUpdated = true;

    if (!context.enabled) {
      this.event = null;
      this.nextEventAtMs = Infinity;
      this.wasEnabled = false;
      return neutral();
    }
    if (!this.wasEnabled || interrupted) {
      this.event = null;
      this.nextEventAtMs = now + this.between(...EVENT_GAP_MS);
      if (this.winkAllowedAtMs === null) this.winkAllowedAtMs = now + WINK_COOLDOWN_MS;
      this.wasEnabled = true;
      return neutral();
    }

    const eyesAllowed = context.eyesAllowed && !context.blinkPaused;
    if (this.event) {
      if (this.event.eyes && (!eyesAllowed || (isWink(this.event.kind) && context.listening))) this.cancelEyes(now);
      if (this.event && !context.browsAllowed) this.event.brows = false;
      if (this.event && !this.event.eyes && !this.event.brows) {
        this.event = null;
        this.nextEventAtMs = now + this.between(...EVENT_GAP_MS);
      }
    }

    if (this.event) {
      const endedAt = this.event.startedAtMs + this.event.closingMs + this.event.holdMs + this.event.openingMs;
      if (now >= endedAt) {
        this.event = null;
        this.nextEventAtMs = endedAt + this.between(...EVENT_GAP_MS);
      }
    }
    if (this.event?.eyes && (context.listening || Math.min(eyeBase(context.eyeLOpen), eyeBase(context.eyeROpen)) < 0.75)
      && (this.event.kind === 'half' || this.event.kind === 'deep' || this.event.kind === 'asymmetric')) {
      // 狀態切換從當下值收尾一次，避免瞬間睜開或每幀重新開始回復。
      const weight = eventWeight(this.event, now);
      this.event.eyeL = 1 + (this.event.eyeL - 1) * weight;
      this.event.eyeR = 1 + (this.event.eyeR - 1) * weight;
      this.event.browL *= weight;
      this.event.browR *= weight;
      this.event.startedAtMs = now;
      this.event.closingMs = 0;
      this.event.holdMs = 0;
      this.event.openingMs = 500;
      this.event.kind = 'relax';
    }
    if (!this.event && now >= this.nextEventAtMs) {
      const startedAt = this.nextEventAtMs;
      this.event = this.createEvent(startedAt, context, eyesAllowed && now >= this.eyesResumeAtMs);
      if (!this.event) this.nextEventAtMs = startedAt + this.between(...EVENT_GAP_MS);
    }
    if (!this.event) return neutral();

    const event = this.event;
    const weight = eventWeight(event, now);
    return {
      eyeL: event.eyes ? 1 + (event.eyeL - 1) * weight : 1,
      eyeR: event.eyes ? 1 + (event.eyeR - 1) * weight : 1,
      browL: event.brows ? event.browL * weight : 0,
      browR: event.brows ? event.browR * weight : 0,
      winking: event.eyes && isWink(event.kind),
      kind: event.eyes ? event.kind : event.brows ? 'brow' : 'still',
    };
  }

  private createEvent(startedAtMs: number, context: FaceIdleContext, eyesAllowed: boolean): FaceEvent | null {
    const pick = this.between(0, 1);
    let kind: FaceIdleKind = pick < 0.12 ? 'still' : pick < 0.42 ? 'relax' : pick < 0.62 ? 'half'
      : pick < 0.7 ? 'deep' : pick < 0.82 ? 'asymmetric' : pick < 0.92 ? 'brow' : 'winkLeft';
    if (kind === 'still') return null;

    const leftBase = eyeBase(context.eyeLOpen);
    const rightBase = eyeBase(context.eyeROpen);
    const winkReady = eyesAllowed && context.blinkReady && !context.listening && !context.blinkPaused
      && leftBase >= 0.78 && rightBase >= 0.78 && Math.abs(leftBase - rightBase) < 0.08
      && this.winkAllowedAtMs !== null && startedAtMs >= this.winkAllowedAtMs;
    if (isWink(kind)) {
      if (!winkReady) kind = 'relax';
      else {
        const leftChance = this.lastWink === 'left' ? 0.25 : this.lastWink === 'right' ? 0.75 : 0.5;
        kind = this.between(0, 1) < leftChance ? 'winkLeft' : 'winkRight';
      }
    }
    // 半閉眼表情或聆聽中只加淺放鬆，保留原表情的非對稱開度。
    const lowBaseline = Math.min(leftBase, rightBase) < 0.75;
    const shallow = lowBaseline || context.listening;
    if (shallow && (kind === 'half' || kind === 'deep' || kind === 'asymmetric')) kind = 'relax';

    let eyeL = 1;
    let eyeR = 1;
    if (kind === 'relax') eyeL = eyeR = this.between(shallow ? 0.90 : 0.84, shallow ? 0.96 : 0.94);
    else if (kind === 'half') eyeL = eyeR = this.between(0.78, 0.88);
    else if (kind === 'deep') eyeL = eyeR = this.between(0.64, 0.75);
    else if (kind === 'asymmetric') {
      const relaxed = this.between(0.74, 0.88);
      const open = this.between(0.92, 1);
      if (this.between(0, 1) < 0.5) { eyeL = relaxed; eyeR = open; }
      else { eyeL = open; eyeR = relaxed; }
    } else if (kind === 'winkLeft') eyeL = 0;
    else if (kind === 'winkRight') eyeR = 0;

    const winking = isWink(kind);
    const event: FaceEvent = {
      kind, startedAtMs,
      closingMs: this.between(winking ? 100 : 700, winking ? 180 : 1400),
      holdMs: this.between(winking ? 120 : 600, winking ? 250 : kind === 'deep' ? 1200 : 2000),
      openingMs: this.between(winking ? 200 : 800, winking ? 350 : 1600),
      eyeL, eyeR, browL: 0, browR: 0,
      eyes: eyesAllowed && kind !== 'brow',
      brows: false,
    };
    if (context.browsAllowed && (kind === 'brow' || this.between(0, 1) < 0.35)) {
      const amount = winking ? this.between(0.05, 0.09) : this.between(0.06, 0.14);
      if (winking) {
        if (kind === 'winkLeft') event.browL = amount;
        else event.browR = amount;
      } else {
        const delta = this.between(0, 1) < 0.5 ? -amount : amount;
        const side = this.between(0, 1);
        if (side < 0.8) event.browL = delta;
        if (side >= 0.2) event.browR = delta;
      }
      event.brows = true;
    }
    if (!event.eyes && !event.brows) return null;
    if (winking && event.eyes) {
      this.lastWink = kind === 'winkLeft' ? 'left' : 'right';
      this.winkAllowedAtMs = startedAtMs + event.closingMs + event.holdMs + event.openingMs + WINK_COOLDOWN_MS;
    }
    return event;
  }

  private between(minimum: number, maximum: number): number {
    const value = this.random();
    return minimum + (Number.isFinite(value) ? clamp(value, 0, 1) : 0.5) * (maximum - minimum);
  }
}
