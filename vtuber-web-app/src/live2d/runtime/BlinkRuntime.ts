interface BlinkEvent {
  startedAtMs: number;
  closingMs: number;
  closedMs: number;
  openingMs: number;
  kind: 'auto' | 'second' | 'forced';
  double: boolean;
  pauseAfterMs: number;
}

const DOUBLE_CHANCE = 0.075;
const DOUBLE_COOLDOWN_MS = 8000;
const MAX_FRAME_GAP_MS = 1000;
const clamp = (value: number, minimum: number, maximum: number) => Math.max(minimum, Math.min(maximum, value));
const ease = (value: number) => value * value * (3 - 2 * value);

/** 只產生眼睛開合係數；表情基準及模型參數由呼叫端統一合成。 */
export class BlinkRuntime {
  private intervalMin = 1;
  private intervalMax = 4;
  private nowMs = 0;
  private hasUpdated = false;
  private wasAllowed = true;
  private wasAutomaticEnabled = true;
  private paused = false;
  private pauseUntilMs: number | null = null;
  private nextBlinkAtMs = Infinity;
  private secondAtMs: number | null = null;
  private doubleCooldownUntilMs = -Infinity;
  private event: BlinkEvent | null = null;
  private pendingForcePauseMs: number | null = null;

  constructor(private readonly random: () => number = Math.random) {}

  pause(durationSec = 0): void {
    this.clearEvent();
    this.paused = true;
    this.pauseUntilMs = Number.isFinite(durationSec) && durationSec > 0 ? this.nowMs + durationSec * 1000 : null;
  }

  resume(): void {
    this.clearEvent();
    this.paused = false;
    this.pauseUntilMs = null;
    this.schedule(this.nowMs);
  }

  forceBlink(pauseAfterSec = 0): void {
    // 原生動作或手動眼皮接管時，不排隊補播強制眨眼。
    if (!this.wasAllowed) return;
    this.pendingForcePauseMs = Number.isFinite(pauseAfterSec) ? Math.max(0, pauseAfterSec) * 1000 : 0;
  }

  setBlinkingIntervalRange(minInterval: number, maxInterval: number): void {
    if (!Number.isFinite(minInterval) || !Number.isFinite(maxInterval) || minInterval < 0 || maxInterval < minInterval) return;
    this.intervalMin = minInterval;
    this.intervalMax = maxInterval;
    if (!this.event && this.secondAtMs === null) this.schedule(this.nowMs);
  }

  isPaused(): boolean { return this.paused; }

  /** 雙眨間隙及尚未取樣的強制指令仍佔用眼皮，不能插入自主 wink。 */
  isIdle(): boolean {
    return this.event === null && this.secondAtMs === null && this.pendingForcePauseMs === null;
  }

  getIntervalRange(): { min: number; max: number } {
    return { min: this.intervalMin, max: this.intervalMax };
  }

  update(dtSeconds: number, nowMs: number, allowed = true, automaticEnabled = true): number {
    const dtMs = Number.isFinite(dtSeconds) ? Math.max(0, dtSeconds) * 1000 : 0;
    const now = Number.isFinite(nowMs) ? nowMs : this.nowMs + dtMs;
    const interrupted = this.hasUpdated && (now < this.nowMs || now - this.nowMs > MAX_FRAME_GAP_MS || dtMs > MAX_FRAME_GAP_MS);
    const first = !this.hasUpdated;
    if (this.pauseUntilMs !== null && (first || now < this.nowMs)) {
      this.pauseUntilMs += first ? now : now - this.nowMs;
    }
    this.nowMs = now;
    this.hasUpdated = true;

    if (this.paused && this.pauseUntilMs !== null && now >= this.pauseUntilMs) {
      this.paused = false;
      this.pauseUntilMs = null;
      this.schedule(now);
    }

    if (!allowed) {
      this.clearEvent();
      this.wasAllowed = false;
      this.wasAutomaticEnabled = automaticEnabled;
      return 1;
    }
    if (interrupted || !this.wasAllowed) {
      this.clearEvent();
      this.schedule(now);
      this.wasAllowed = true;
      this.wasAutomaticEnabled = automaticEnabled;
      return 1;
    }
    this.wasAllowed = true;
    if (first) this.schedule(now);

    // 自動效果開關不阻擋明確的單眨指令，但不保留自動雙眨。
    if (!automaticEnabled) {
      if (this.event?.kind !== 'forced') this.event = null;
      this.secondAtMs = null;
      this.nextBlinkAtMs = Infinity;
    } else if (!this.wasAutomaticEnabled) this.schedule(now);
    this.wasAutomaticEnabled = automaticEnabled;

    if (this.pendingForcePauseMs !== null) {
      const pauseAfterMs = this.pendingForcePauseMs;
      this.clearEvent();
      this.event = this.createEvent(now, 'forced', pauseAfterMs);
    }

    // 強制單眨可在暫停中完成，完成後仍保留原暫停狀態。
    if (this.paused && this.event?.kind !== 'forced') return 1;

    // 跨越階段邊界時保留實際結束時間，避免眨眼節奏依 frame rate 漂移。
    for (let transitions = 0; transitions < 8; transitions += 1) {
      if (!this.event) {
        if (this.secondAtMs !== null) {
          if (now < this.secondAtMs) return 1;
          this.event = this.createEvent(this.secondAtMs, 'second');
          this.secondAtMs = null;
        } else {
          if (this.paused || !automaticEnabled || now < this.nextBlinkAtMs) return 1;
          this.event = this.createEvent(this.nextBlinkAtMs, 'auto');
        }
      }

      const event = this.event;
      const elapsed = Math.max(0, now - event.startedAtMs);
      if (elapsed < event.closingMs) return 1 - ease(elapsed / event.closingMs);
      if (elapsed < event.closingMs + event.closedMs) return 0;
      const openingElapsed = elapsed - event.closingMs - event.closedMs;
      if (openingElapsed < event.openingMs) return ease(openingElapsed / event.openingMs);

      const endedAt = event.startedAtMs + event.closingMs + event.closedMs + event.openingMs;
      this.event = null;
      if (event.kind === 'forced' && event.pauseAfterMs > 0) {
        this.paused = true;
        this.pauseUntilMs = endedAt + event.pauseAfterMs;
        this.nextBlinkAtMs = Infinity;
        return 1;
      }
      if (event.double) this.secondAtMs = endedAt + this.between(100, 180);
      else {
        if (event.kind === 'second') this.doubleCooldownUntilMs = endedAt + DOUBLE_COOLDOWN_MS;
        if (automaticEnabled) this.schedule(endedAt);
        else this.nextBlinkAtMs = Infinity;
      }
    }
    // 零間隔搭配極低更新頻率也不補播無限個過期事件。
    this.clearEvent();
    this.schedule(now);
    return 1;
  }

  private createEvent(startedAtMs: number, kind: BlinkEvent['kind'], pauseAfterMs = 0): BlinkEvent {
    return {
      startedAtMs,
      closingMs: this.between(70, 120),
      closedMs: this.between(20, 60),
      openingMs: this.between(120, 200),
      kind,
      double: kind === 'auto' && startedAtMs >= this.doubleCooldownUntilMs && this.between(0, 1) < DOUBLE_CHANCE,
      pauseAfterMs,
    };
  }

  private schedule(fromMs: number): void {
    this.nextBlinkAtMs = fromMs + this.between(this.intervalMin, this.intervalMax) * 1000;
  }

  private clearEvent(): void {
    this.event = null;
    this.secondAtMs = null;
    this.pendingForcePauseMs = null;
    this.nextBlinkAtMs = Infinity;
  }

  private between(minimum: number, maximum: number): number {
    const value = this.random();
    return minimum + (Number.isFinite(value) ? clamp(value, 0, 1) : 0.5) * (maximum - minimum);
  }
}
