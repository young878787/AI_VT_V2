import { AMBIENT_AXES, isAmbientPlan } from '../../types/ambientPlan';
import type { AmbientAxis, AmbientPlan, AmbientTarget } from '../../types/ambientPlan';
import { LIMITS } from './ParameterMixer';

export interface AmbientSample extends AmbientTarget {
  breath: number;
  /** 相對 viewport 高度，向上為正；每幀從構圖基準套用，不能累加。 */
  rootY: number;
  /** 目前呼吸／整段跳動的峰值預算，供渲染器先縮幅；不屬於 wire payload。 */
  rootPeak: number;
}

interface Spring { value: number; velocity: number }
/** skew：時間扭曲（正值起步快、負值起步慢）；velocity 已預先除以 (1 + skew)，所以起始速度仍是接管當下的速度。
 *  after：這一段走完（停在過衝點）後接的回彈段；sag：到位後逐漸沉回的量（與 to 同號）。 */
interface Segment {
  startedAt: number; duration: number; from: number; velocity: number; to: number;
  previous?: Segment; skew?: number; sag?: number; after?: Segment;
}
type GazeAxis = 'gazeX' | 'gazeY';
type PoseAxis = Exclude<AmbientAxis, GazeAxis>;

const TAU = Math.PI * 2;
const BASIC_BREATH = { periodSec: 4.8, amplitude: 0.25, rootAmplitude: 0.006 };
const clamp = (value: number, minimum: number, maximum: number) => Math.max(minimum, Math.min(maximum, value));
const spring = (value = 0): Spring => ({ value, velocity: 0 });
const POSE_AXES = AMBIENT_AXES.filter((axis): axis is PoseAxis => !axis.startsWith('gaze'));
const still = (value = 0): Segment => ({ startedAt: 0, duration: 1, from: value, velocity: 0, to: value });
// 眼睛先動，頭、身體依序晚起動；延遲期間沿用前一段曲線，位置與速度都連續。
const ONSET_LAG_MS = { head: 80, body: 200 };
const GAZE_AXES: readonly GazeAxis[] = ['gazeX', 'gazeY'];
// 掃視時間隨距離增加（正規化視線距離）；眼睛幾乎瞬間到位，不走頭身的慢曲線。
const SACCADE_MS = { base: 35, perUnit: 50, min: 40, max: 80 };
// 注視期間的微掃視：短促的小跳動，間隔 0.35～1.5 秒；Y 行程較小所以縮幅。
export const FIXATION_JITTER = 0.14;
const FIXATION = { minRadius: 0.05, yScale: 0.7, moveMs: 35, gapMs: [350, 1500] as const };
// 頭部移動分兩種：出發（起步快、衝過目標約 5% 再緩緩回來）與歸位（往中性：起步慢、只微彈約 3%）。
// 過衝是明確的兩段軌跡，所以大小與移動時間無關；身體不過衝，頭比身體明顯。
type HeadMode = 'depart' | 'return';
const HEAD_MODES: Record<HeadMode, { skew: number; overshoot: number; settleMs: number }> = {
  depart: { skew: 0.25, overshoot: 0.055, settleMs: 520 },
  return: { skew: -0.2, overshoot: 0.03, settleMs: 420 },
};
const MIN_BOUNCE = 0.02;
// 歪頭（headZ）專用：幅度越大越慢、峰值角速度有上限、歸位比出發更慵懶；
// 持住大角度時慢慢沉回一點，並加大微動，不像釘在那個角度。
const ROLL = { maxDegPerSec: 18, stretch: 0.5, full: 0.6, returnStretch: 1.4, holdGain: 1.5, holdFull: 0.4 };
const SAG = { fraction: 0.15, full: 0.4, tauMs: 600 };
// 呼吸帶動頭、身體的上下起伏（正規化，乘上呼吸幅度）。
const BREATH_LIFT: Partial<Record<PoseAxis, number>> = { headY: 0.06, bodyY: 0.08 };

// 確定性漂移：同一個 seed 得到同一條連續曲線，沒有週期；seed 由載入端提供。
// 多層帶限雜訊（Hz, 相對權重）；頭身各軸的峰值幅度為正規化值。
const SWAY_LAYERS = [[0.23, 1], [0.55, 0.8], [1.05, 0.4]] as const;
const SWAY_WEIGHT = SWAY_LAYERS.reduce((sum, [, weight]) => sum + weight, 0);
const SWAY_AMPLITUDE: Record<PoseAxis, number> = {
  headX: 0.13, headY: 0.15, headZ: 0.13, bodyX: 0.14, bodyY: 0.12, bodyZ: 0.13,
};
// 身體的漂移以此延遲傳給頭部對應的軸，頭比身體慢半拍。
const BODY_TO_HEAD = { gain: 0.35, lagSec: 0.18 };
// 歪頭時臉跟著轉：AngleZ 只是整顆頭旋轉，臉型不變會像平面貼圖在轉。把頭 Z 的曲線值（含回彈與沉回）
// 耦合到 X（同向轉臉）與 Y（歪向任一側都略抬）；符號與幅度純屬手感，不對就改這裡。
export const TILT_FACE = { x: 0.45, y: 0.35 };
const SWAY_ENVELOPE_HZ = 0.043;

function hashUnit(seed: number, channel: number, cell: number): number {
  let h = (seed ^ Math.imul(channel + 1, 0x9e3779b1) ^ Math.imul(cell | 0, 0x85ebca6b)) >>> 0;
  h = Math.imul(h ^ (h >>> 16), 0x7feb352d);
  h = Math.imul(h ^ (h >>> 15), 0x846ca68b);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

/** 一維梯度雜訊（五次淡化，二階連續），輸出約 -1～1。 */
function gradientNoise(seed: number, channel: number, x: number): number {
  const cell = Math.floor(x);
  const t = x - cell;
  const fade = t * t * t * (t * (t * 6 - 15) + 10);
  const g0 = hashUnit(seed, channel, cell) * 2 - 1;
  const g1 = hashUnit(seed, channel, cell + 1) * 2 - 1;
  return 2 * (g0 * t * (1 - fade) + g1 * (t - 1) * fade);
}

function createRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 臨界阻尼解析解；相同經過時間不受 30／60／120 Hz 分割影響。 */
function advance(state: Spring, target: number, omega: number, dt: number): number {
  const offset = state.value - target;
  const change = (state.velocity + omega * offset) * dt;
  const decay = Math.exp(-omega * dt);
  state.value = target + (offset + change) * decay;
  state.velocity = (state.velocity - omega * change) * decay;
  return state.value;
}

/** 歪頭移動時間：幅度越大越慢，歸位更慵懶，並讓峰值角速度（約 2 倍平均速度）不超過上限。 */
function rollDuration(baseMs: number, amplitude: number, returning: boolean): number {
  const stretched = baseMs * (1 + ROLL.stretch * clamp(amplitude / ROLL.full, 0, 1));
  const duration = Math.max(stretched, 2000 * amplitude * LIMITS.ParamAngleZ / ROLL.maxDegPerSec);
  return returning ? duration * ROLL.returnStretch : duration;
}

/** 頭頂安全餘量使用 viewport 比例；未知構圖時不准向上移出畫面。 */
export function clampRootY(rootY: number, topMarginRatio: number, minTopMargin = 0.02): number {
  if (!Number.isFinite(rootY) || !Number.isFinite(topMarginRatio) || !Number.isFinite(minTopMargin)) return 0;
  return clamp(rootY, 0, Math.min(0.2, Math.max(0, topMarginRatio - Math.max(0, minTopMargin))));
}

// 五次曲線保留接管當下的位置與速度；起訖加速度為零，速度呈鐘形。
function sampleQuintic(segment: Segment, nowMs: number): Spring {
  if (nowMs < segment.startedAt && segment.previous) return sampleQuintic(segment.previous, nowMs);
  const progress = clamp((nowMs - segment.startedAt) / segment.duration, 0, 1);
  if (progress >= 1) return spring(segment.to);
  // 時間扭曲 u(t) = t + k t (1 - t)：端點不變、u' 恆正；起訖速度為零的曲線仍是零。
  const skew = segment.skew ?? 0;
  const t = progress + skew * progress * (1 - progress);
  const warp = 1 + skew * (1 - 2 * progress);
  const durationSec = segment.duration / 1000;
  const velocity = segment.velocity * durationSec;
  const delta = segment.to - segment.from;
  const c3 = 10 * delta - 6 * velocity;
  const c4 = -15 * delta + 8 * velocity;
  const c5 = 6 * delta - 3 * velocity;
  const value = segment.from + velocity * t + c3 * t ** 3 + c4 * t ** 4 + c5 * t ** 5;
  const speed = (velocity + 3 * c3 * t ** 2 + 4 * c4 * t ** 3 + 5 * c5 * t ** 4) / durationSec * warp;
  return { value, velocity: speed };
}

/** 加上到位後的沉回；(1 - (1 + x) e^-x) 起始速度為零，接上五次曲線不會產生速度跳變。 */
function samplePose(segment: Segment, nowMs: number): Spring {
  if (nowMs < segment.startedAt && segment.previous) return samplePose(segment.previous, nowMs);
  if (segment.after && nowMs >= segment.after.startedAt) return samplePose(segment.after, nowMs);
  const state = sampleQuintic(segment, nowMs);
  const elapsed = nowMs - segment.startedAt - segment.duration;
  if (!segment.sag || elapsed <= 0) return state;
  const x = elapsed / SAG.tauMs;
  const decay = Math.exp(-x);
  return { value: state.value - segment.sag * (1 - (1 + x) * decay),
    velocity: state.velocity - segment.sag * x * decay / (SAG.tauMs / 1000) };
}

function sampleSegment(segment: Segment, nowMs: number): Spring {
  const state = sampleQuintic(segment, nowMs);
  if (state.value < 0 || state.value > 0.12) return spring(clamp(state.value, 0, 0.12));
  return state;
}

/** 只執行 Python 計畫；不挑選姿勢、不使用亂數、不寫入 UI store。 */
export class AmbientMotion {
  private sequence = 0;
  private target = Object.fromEntries(AMBIENT_AXES.map(axis => [axis, 0])) as AmbientTarget;
  private poses = Object.fromEntries(POSE_AXES.map(axis => [axis, still()])) as Record<PoseAxis, Segment>;
  private gazes: Record<GazeAxis, Segment> = { gazeX: still(), gazeY: still() };
  private fixation: Record<GazeAxis, Segment> = { gazeX: still(), gazeY: still() };
  private fixationAtMs: number | null = null;
  private random: () => number;
  private driftSec = 0;
  private moveMs = 1000;
  private breathTarget = { ...BASIC_BREATH };
  private period = spring(BASIC_BREATH.periodSec);
  private amplitude = spring(BASIC_BREATH.amplitude);
  private rootAmplitude = spring(BASIC_BREATH.rootAmplitude);
  private phase = 0;
  private bodyGain = spring(1);
  private gazeGain = spring(1);
  private enableGain = spring(1);
  private hopSegments: Segment[] = [];
  private wasEnabled = true;
  private lastNowMs = 0;
  private hasUpdated = false;

  /** seed 決定整條漂移與微掃視曲線；同一個 seed 逐幀相同，載入端每次換新。 */
  constructor(private readonly seed = 0x2f6e2b1) {
    this.random = createRandom(seed);
  }

  apply(plan: AmbientPlan, nowMs: number): boolean {
    if (!isAmbientPlan(plan) || !Number.isFinite(nowMs) || plan.sequence <= this.sequence) return false;
    this.sequence = plan.sequence;
    this.moveMs = plan.moveMs;
    // 只換眼神或 hop 的計畫不重啟頭身軌跡；目標有變的軸才重排。
    const changed = POSE_AXES.filter(axis => plan.target[axis] !== this.target[axis]);
    const gazeChanged = GAZE_AXES.some(axis => plan.target[axis] !== this.target[axis]);
    this.target = { ...plan.target };
    this.breathTarget = { ...plan.breath };
    this.scheduleFixation(nowMs);
    if (this.wasEnabled) {
      this.retarget(nowMs, true, changed, true);
      if (gazeChanged) this.retargetGaze(nowMs, true);
    }
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
    for (const axis of POSE_AXES) this.target[axis] = samplePose(this.poses[axis], this.sampledAt(now)).value;
    for (const axis of GAZE_AXES) this.target[axis] = sampleQuintic(this.gazes[axis], this.sampledAt(now)).value;
    if (this.wasEnabled) {
      this.retarget(now, true);
      this.retargetGaze(now, true);
    }
    this.breathTarget = { ...BASIC_BREATH };
  }

  reset(): void {
    this.sequence = 0;
    this.moveMs = 1000;
    for (const axis of AMBIENT_AXES) this.target[axis] = 0;
    for (const axis of POSE_AXES) this.poses[axis] = still();
    this.gazes = { gazeX: still(), gazeY: still() };
    this.fixation = { gazeX: still(), gazeY: still() };
    this.fixationAtMs = null;
    this.random = createRandom(this.seed);
    this.driftSec = 0;
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
    if (this.hasUpdated && now - this.lastNowMs > 250) {
      if (this.hopSegments.length) this.finishHop(this.sampleHop(this.lastNowMs), now);
      this.retarget(now, this.wasEnabled);
      this.retargetGaze(now, this.wasEnabled);
      this.fixationAtMs = null;
    }
    this.scheduleFixation(now);
    this.lastNowMs = now;
    this.hasUpdated = true;
    if (this.wasEnabled && !enabled) this.cancelHop(now);
    if (this.wasEnabled !== enabled) {
      this.retarget(now, enabled);
      this.retargetGaze(now, enabled);
    }
    this.wasEnabled = enabled;
    this.driftSec += dt;
    const body = advance(this.bodyGain, Number.isFinite(bodyWeight) ? clamp(bodyWeight, 0, 1) : 0, 24, dt);
    const gaze = advance(this.gazeGain, Number.isFinite(gazeWeight) ? clamp(gazeWeight, 0, 1) : 0, 24, dt);
    const gain = advance(this.enableGain, enabled ? 1 : 0, 18, dt);
    const result = {} as AmbientSample;
    // 週期、幅度可更新，相位始終延續；停用也不重啟呼吸週期。
    const previousPeriod = this.period.value;
    const midpoint = { ...this.period };
    const midpointPeriod = advance(midpoint, this.breathTarget.periodSec, 5, dt / 2);
    const period = advance(this.period, this.breathTarget.periodSec, 5, dt);
    const amplitude = advance(this.amplitude, this.breathTarget.amplitude, 5, dt);
    const rootAmplitude = advance(this.rootAmplitude, this.breathTarget.rootAmplitude, 5, dt);
    this.phase = (this.phase + TAU * dt * (1 / previousPeriod + 4 / midpointPeriod + 1 / period) / 6) % TAU;
    const wave = (1 - Math.cos(this.phase)) / 2;
    // 眼球走短促的掃視，注視時疊加微掃視；頭身走五次曲線，頭再經欠阻尼跟隨，身體較慢停穩。
    this.advanceFixation(now);
    for (const axis of GAZE_AXES) {
      const value = sampleQuintic(this.gazes[axis], now).value + sampleQuintic(this.fixation[axis], now).value;
      result[axis] = clamp(value, -1, 1) * gain * gaze;
    }
    const tilt = samplePose(this.poses.headZ, now).value;
    for (const axis of POSE_AXES) {
      let value = samplePose(this.poses[axis], now).value;
      if (axis === 'headX') value += TILT_FACE.x * tilt;
      else if (axis === 'headY') value += TILT_FACE.y * Math.abs(tilt);
      // 歪頭大角度持住時微動加大，避免凍結在那個角度。
      const hold = axis === 'headZ' ? 1 + ROLL.holdGain * clamp(Math.abs(value) / ROLL.holdFull, 0, 1) : 1;
      value += this.sway(axis, this.driftSec) * hold + (BREATH_LIFT[axis] ?? 0) * (wave - 0.5) * amplitude;
      result[axis] = clamp(value, -1, 1) * gain * body;
    }
    result.breath = clamp(wave * amplitude * gain, 0, 1);
    result.rootY = clamp((wave * rootAmplitude + this.sampleHop(now).value) * body * gain, 0, 0.2);
    const hopPeak = this.hopSegments.reduce((peak, segment) => Math.max(peak,
      Math.min(0.12, Math.max(segment.from, segment.to) + Math.max(0, segment.velocity) * segment.duration / 5000)), 0);
    result.rootPeak = Math.max(rootAmplitude, this.breathTarget.rootAmplitude) + hopPeak;
    return result;
  }

  /** 背景分頁恢復時從最後顯示的位置接續，不跳到牆鐘時間的進度。 */
  private sampledAt(nowMs: number): number {
    return this.hasUpdated && nowMs - this.lastNowMs > 250 ? this.lastNowMs : nowMs;
  }

  /** 持續的低頻漂移；停住時也不會靜止。頭部另外接收延遲後的身體漂移。 */
  private sway(axis: PoseAxis, seconds: number): number {
    const own = this.swayOwn(axis, seconds);
    if (!axis.startsWith('head')) return own;
    return own + BODY_TO_HEAD.gain * this.swayOwn(axis.replace('head', 'body') as PoseAxis, seconds - BODY_TO_HEAD.lagSec);
  }

  private swayOwn(axis: PoseAxis, seconds: number): number {
    const index = POSE_AXES.indexOf(axis);
    let sum = 0;
    SWAY_LAYERS.forEach(([hz, weight], layer) => {
      const channel = index * 8 + layer;
      sum += weight * gradientNoise(this.seed, channel, seconds * hz + hashUnit(this.seed, channel, 0x5eed) * 64);
    });
    // 幅度本身緩慢起伏，避免全程等強度的機械感。
    const envelope = 0.75 + 0.35 * gradientNoise(this.seed, 99, seconds * SWAY_ENVELOPE_HZ + index * 7.3);
    return SWAY_AMPLITUDE[axis] * clamp(envelope, 0.45, 1.1) * sum / SWAY_WEIGHT;
  }

  // 眼睛只有一個目標，兩軸同時到位；時間依距離，與頭身 moveMs 無關。
  private retargetGaze(nowMs: number, enabled: boolean): void {
    const sampledAt = this.sampledAt(nowMs);
    const from = GAZE_AXES.map(axis => sampleQuintic(this.gazes[axis], sampledAt).value);
    const to = GAZE_AXES.map(axis => enabled ? this.target[axis] : 0);
    const distance = Math.hypot(to[0] - from[0], to[1] - from[1]);
    const duration = clamp(SACCADE_MS.base + SACCADE_MS.perUnit * distance, SACCADE_MS.min, SACCADE_MS.max);
    GAZE_AXES.forEach((axis, index) => {
      this.gazes[axis] = { startedAt: nowMs, duration, from: from[index], velocity: 0, to: to[index] };
    });
  }

  private nextFixationGap(): number {
    return FIXATION.gapMs[0] + this.random() * (FIXATION.gapMs[1] - FIXATION.gapMs[0]);
  }

  private scheduleFixation(nowMs: number): void {
    if (this.fixationAtMs === null) this.fixationAtMs = nowMs + this.nextFixationGap();
  }

  // 排程時間不依 frame 取樣，所以 30／60／120 Hz 得到同一串微掃視。
  private advanceFixation(nowMs: number): void {
    if (this.fixationAtMs === null || nowMs < this.fixationAtMs) return;
    const startedAt = this.fixationAtMs;
    const angle = this.random() * TAU;
    const radius = FIXATION.minRadius + this.random() * (FIXATION_JITTER - FIXATION.minRadius);
    const to = { gazeX: Math.cos(angle) * radius, gazeY: Math.sin(angle) * radius * FIXATION.yScale };
    for (const axis of GAZE_AXES) {
      this.fixation[axis] = { startedAt, duration: FIXATION.moveMs, velocity: 0,
        from: sampleQuintic(this.fixation[axis], startedAt).value, to: to[axis] };
    }
    this.fixationAtMs = startedAt + this.nextFixationGap();
    if (this.fixationAtMs < nowMs) this.fixationAtMs = nowMs + this.nextFixationGap();
  }

  // 新目標從目前位置與速度起算；停用時收回中性位置。
  private retarget(nowMs: number, enabled: boolean, axes: readonly PoseAxis[] = POSE_AXES, staggered = false): void {
    const sampledAt = this.sampledAt(nowMs);
    // 背景恢復從最後畫面接續，不延遲起動，避免延遲期間顯示牆鐘進度。
    const lagged = staggered && sampledAt === nowMs;
    for (const axis of axes) {
      const body = axis.startsWith('body');
      const previous = this.poses[axis];
      const startedAt = nowMs + (lagged ? ONSET_LAG_MS[body ? 'body' : 'head'] : 0);
      const current = samplePose(previous, lagged ? startedAt : sampledAt);
      if (previous.startedAt <= nowMs) previous.previous = undefined;
      const to = enabled ? this.target[axis] : 0;
      const next: Segment = {
        startedAt, duration: this.moveMs * (body ? 1.45 : 1),
        from: current.value, velocity: current.velocity, to,
        previous: lagged ? previous : undefined,
      };
      if (!body) {
        // 往中性（幅度變小且不跨過 0）算歸位，其餘算出發；兩者曲線與回彈不同。
        const returning = Math.abs(to) < Math.abs(current.value) - 0.03
          && (to * current.value >= 0 || Math.abs(to) < 0.05);
        const { skew, overshoot, settleMs } = HEAD_MODES[returning ? 'return' : 'depart'];
        const distance = Math.abs(to - current.value);
        next.skew = skew;
        next.velocity = current.velocity / (1 + skew);
        if (axis === 'headZ') next.duration = rollDuration(next.duration, distance, returning);
        const sag = axis === 'headZ' && !returning ? SAG.fraction * clamp(Math.abs(to) / SAG.full, 0, 1) * to : undefined;
        if (distance >= MIN_BOUNCE) {
          // 先衝到目標之外一點，再從零速度緩緩回到目標；沉回接在回彈之後。
          next.to = to + Math.sign(to - current.value) * overshoot * distance;
          next.after = { startedAt: startedAt + next.duration, duration: settleMs,
            from: next.to, velocity: 0, to, sag };
        } else next.sag = sag;
      }
      this.poses[axis] = next;
    }
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
