import { AMBIENT_AXES, isAmbientPlan } from '../../types/ambientPlan';
import type { AmbientAxis, AmbientPlan, AmbientTarget } from '../../types/ambientPlan';

export interface AmbientSample extends AmbientTarget {
  breath: number;
  /** 相對 viewport 高度，向上為正；每幀從構圖基準套用，不能累加。 */
  rootY: number;
  /** 目前呼吸／整段跳動的峰值預算，供渲染器先縮幅；不屬於 wire payload。 */
  rootPeak: number;
}

interface Spring { value: number; velocity: number }
interface HopSegment { startedAt: number; duration: number; from: number; velocity: number; to: number }

const TAU = Math.PI * 2;
const BASIC_BREATH = { periodSec: 4.8, amplitude: 0.25, rootAmplitude: 0.006 };
const clamp = (value: number, minimum: number, maximum: number) => Math.max(minimum, Math.min(maximum, value));
const spring = (value = 0): Spring => ({ value, velocity: 0 });

/** 臨界阻尼解析解；相同經過時間不受 30／60／120 Hz 分割影響。 */
function advance(state: Spring, target: number, omega: number, dt: number): number {
  const offset = state.value - target;
  const change = (state.velocity + omega * offset) * dt;
  const decay = Math.exp(-omega * dt);
  state.value = target + (offset + change) * decay;
  state.velocity = (state.velocity - omega * change) * decay;
  return state.value;
}

/** 頭頂安全餘量使用 viewport 比例；未知構圖時不准向上移出畫面。 */
export function clampRootY(rootY: number, topMarginRatio: number, minTopMargin = 0.02): number {
  if (!Number.isFinite(rootY) || !Number.isFinite(topMarginRatio) || !Number.isFinite(minTopMargin)) return 0;
  return clamp(rootY, 0, Math.min(0.2, Math.max(0, topMarginRatio - Math.max(0, minTopMargin))));
}

// 五次曲線保留接管當下的位置與速度；起訖加速度為零。
function sampleSegment(segment: HopSegment, nowMs: number): Spring {
  const t = clamp((nowMs - segment.startedAt) / segment.duration, 0, 1);
  if (t >= 1) return spring(segment.to);
  const durationSec = segment.duration / 1000;
  const velocity = segment.velocity * durationSec;
  const delta = segment.to - segment.from;
  const c3 = 10 * delta - 6 * velocity;
  const c4 = -15 * delta + 8 * velocity;
  const c5 = 6 * delta - 3 * velocity;
  const value = segment.from + velocity * t + c3 * t ** 3 + c4 * t ** 4 + c5 * t ** 5;
  const speed = (velocity + 3 * c3 * t ** 2 + 4 * c4 * t ** 3 + 5 * c5 * t ** 4) / durationSec;
  if (value < 0 || value > 0.12) return spring(clamp(value, 0, 0.12));
  return { value, velocity: speed };
}

/** 只執行 Python 計畫；不挑選姿勢、不使用亂數、不寫入 UI store。 */
export class AmbientMotion {
  private sequence = 0;
  private target = Object.fromEntries(AMBIENT_AXES.map(axis => [axis, 0])) as AmbientTarget;
  private axes = Object.fromEntries(AMBIENT_AXES.map(axis => [axis, spring()])) as Record<AmbientAxis, Spring>;
  private moveMs = 1000;
  private breathTarget = { ...BASIC_BREATH };
  private period = spring(BASIC_BREATH.periodSec);
  private amplitude = spring(BASIC_BREATH.amplitude);
  private rootAmplitude = spring(BASIC_BREATH.rootAmplitude);
  private phase = 0;
  private bodyGain = spring(1);
  private gazeGain = spring(1);
  private enableGain = spring(1);
  private hopSegments: HopSegment[] = [];
  private wasEnabled = true;
  private lastNowMs = 0;
  private hasUpdated = false;

  apply(plan: AmbientPlan, nowMs: number): boolean {
    if (!isAmbientPlan(plan) || !Number.isFinite(nowMs) || plan.sequence <= this.sequence) return false;
    this.sequence = plan.sequence;
    this.moveMs = plan.moveMs;
    this.target = { ...plan.target };
    this.breathTarget = { ...plan.breath };
    if (plan.state === 'listening') this.cancelHop(nowMs);
    else if (plan.hop && this.wasEnabled) {
      const current = this.sampleHop(nowMs);
      const rise = plan.hop.durationMs * 0.45;
      this.hopSegments = [
        { startedAt: nowMs, duration: rise, from: current.value, velocity: current.velocity, to: plan.hop.height },
        { startedAt: nowMs + rise, duration: plan.hop.durationMs - rise, from: plan.hop.height, velocity: 0, to: 0 },
      ];
    }
    return true;
  }

  disconnect(nowMs: number): void {
    const now = Number.isFinite(nowMs) ? nowMs : this.lastNowMs;
    this.cancelHop(now);
    this.sequence = 0;
    for (const axis of AMBIENT_AXES) this.target[axis] = this.axes[axis].value;
    this.breathTarget = { ...BASIC_BREATH };
  }

  reset(): void {
    this.sequence = 0;
    this.moveMs = 1000;
    for (const axis of AMBIENT_AXES) {
      this.target[axis] = 0;
      this.axes[axis] = spring();
    }
    this.hopSegments = [];
    this.breathTarget = { ...BASIC_BREATH };
    this.period = spring(BASIC_BREATH.periodSec);
    this.amplitude = spring(BASIC_BREATH.amplitude);
    this.rootAmplitude = spring(BASIC_BREATH.rootAmplitude);
    this.bodyGain = spring(1);
    this.gazeGain = spring(1);
    this.enableGain = spring(1);
    this.phase = 0;
    this.wasEnabled = true;
    this.lastNowMs = 0;
    this.hasUpdated = false;
  }

  update(dtSeconds: number, nowMs: number, enabled: boolean, bodyWeight = 1, gazeWeight = 1): AmbientSample {
    const dt = Number.isFinite(dtSeconds) ? clamp(dtSeconds, 0, 0.1) : 0;
    const now = Number.isFinite(nowMs) ? nowMs : this.lastNowMs;
    // 背景分頁恢復時從最後顯示的高度收尾，不瞬間落地或補播過期跳動。
    if (this.hasUpdated && now - this.lastNowMs > 250 && this.hopSegments.length) {
      this.finishHop(this.sampleHop(this.lastNowMs), now);
    }
    this.lastNowMs = now;
    this.hasUpdated = true;
    if (this.wasEnabled && !enabled) this.cancelHop(now);
    this.wasEnabled = enabled;
    const body = advance(this.bodyGain, Number.isFinite(bodyWeight) ? clamp(bodyWeight, 0, 1) : 0, 24, dt);
    const gaze = advance(this.gazeGain, Number.isFinite(gazeWeight) ? clamp(gazeWeight, 0, 1) : 0, 24, dt);
    const gain = advance(this.enableGain, enabled ? 1 : 0, 18, dt);
    const result = {} as AmbientSample;
    for (const axis of AMBIENT_AXES) {
      const moveSec = axis.startsWith('gaze') ? 0.2 : this.moveMs / 1000;
      const value = advance(this.axes[axis], enabled ? this.target[axis] : 0, 4.8 / moveSec, dt);
      result[axis] = clamp(value, -1, 1) * gain * (axis.startsWith('gaze') ? gaze : body);
    }
    // 週期、幅度可更新，相位始終延續；停用也不重啟呼吸週期。
    const previousPeriod = this.period.value;
    const midpoint = { ...this.period };
    const midpointPeriod = advance(midpoint, this.breathTarget.periodSec, 5, dt / 2);
    const period = advance(this.period, this.breathTarget.periodSec, 5, dt);
    const amplitude = advance(this.amplitude, this.breathTarget.amplitude, 5, dt);
    const rootAmplitude = advance(this.rootAmplitude, this.breathTarget.rootAmplitude, 5, dt);
    this.phase = (this.phase + TAU * dt * (1 / previousPeriod + 4 / midpointPeriod + 1 / period) / 6) % TAU;
    const wave = (1 - Math.cos(this.phase)) / 2;
    result.breath = clamp(wave * amplitude * gain, 0, 1);
    result.rootY = clamp((wave * rootAmplitude + this.sampleHop(now).value) * body * gain, 0, 0.2);
    const hopPeak = this.hopSegments.reduce((peak, segment) => Math.max(peak,
      Math.min(0.12, Math.max(segment.from, segment.to) + Math.max(0, segment.velocity) * segment.duration / 5000)), 0);
    result.rootPeak = Math.max(rootAmplitude, this.breathTarget.rootAmplitude) + hopPeak;
    return result;
  }

  private sampleHop(nowMs: number): Spring {
    while (this.hopSegments.length && nowMs >= this.hopSegments[0].startedAt + this.hopSegments[0].duration) {
      this.hopSegments.shift();
    }
    return this.hopSegments.length ? sampleSegment(this.hopSegments[0], nowMs) : spring();
  }

  private cancelHop(nowMs: number): void {
    if (!this.hopSegments.length) return;
    const current = this.sampleHop(this.hasUpdated && nowMs - this.lastNowMs > 250 ? this.lastNowMs : nowMs);
    this.finishHop(current, nowMs);
  }

  private finishHop(current: Spring, nowMs: number): void {
    this.hopSegments = current.value > 0 || current.velocity > 0
      ? [{ startedAt: nowMs, duration: 450, from: current.value, velocity: current.velocity, to: 0 }]
      : [];
  }
}
