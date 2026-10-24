interface ParameterModel {
  getParameterCount(): number;
  getParameterId(index: number): { getString(): string };
  getParameterDefaultValue(index: number): number;
  getParameterMinimumValue(index: number): number;
  getParameterMaximumValue(index: number): number;
  getParameterValueByIndex(index: number): number;
  setParameterValueByIndex(index: number, value: number): void;
}

const LIMITS: Record<string, number> = {
  ParamAngleX: 22, ParamAngleY: 16, ParamAngleZ: 23,
  ParamBodyAngleX: 8, ParamBodyAngleY: 7, ParamBodyAngleZ: 9,
  ParamEyeBallX: 0.85, ParamEyeBallY: 1,
};

/** 單一受控通道輸出；快取真實 index，換 owner 時保留當前位置與速度。 */
export class ParameterMixer {
  private readonly channels = new Map<string, {
    index: number; low: number; high: number; rawLow: number; rawHigh: number; neutral: number; x: number; v: number;
  }>();

  constructor(private readonly model: ParameterModel) {
    for (let index = 0; index < model.getParameterCount(); index += 1) {
      const id = model.getParameterId(index).getString();
      const limit = LIMITS[id];
      if (limit === undefined) continue;
      const neutral = model.getParameterDefaultValue(index);
      this.channels.set(id, {
        index, neutral,
        rawLow: model.getParameterMinimumValue(index),
        rawHigh: model.getParameterMaximumValue(index),
        low: Math.max(model.getParameterMinimumValue(index), neutral - limit),
        high: Math.min(model.getParameterMaximumValue(index), neutral + limit),
        x: model.getParameterValueByIndex(index), v: 0,
      });
    }
  }

  normalized(id: string, value: number): number {
    const channel = this.channels.get(id);
    if (!channel) return 0;
    const q = Math.max(-1, Math.min(1, value));
    return channel.neutral + q * (q >= 0
      ? channel.high - channel.neutral : channel.neutral - channel.low);
  }

  has(id: string): boolean {
    return this.channels.has(id);
  }

  write(id: string, target: number, dt: number, immediate = false, autonomous = true): number {
    const channel = this.channels.get(id);
    if (!channel || !Number.isFinite(target)) return 0;
    const low = autonomous ? channel.low : channel.rawLow;
    const high = autonomous ? channel.high : channel.rawHigh;
    const goal = Math.max(low, Math.min(high, target));
    if (immediate) {
      channel.x = goal;
      channel.v = 0;
    } else {
      const omega = id.startsWith('ParamEyeBall') ? 40 : 18;
      const delta = channel.x - goal;
      const j = channel.v + omega * delta;
      const decay = Math.exp(-omega * dt);
      channel.x = goal + (delta + j * dt) * decay;
      channel.v = (channel.v - omega * j * dt) * decay;
      const bounded = Math.max(channel.rawLow, Math.min(channel.rawHigh, channel.x));
      if (bounded !== channel.x) channel.v = 0;
      channel.x = bounded;
    }
    this.model.setParameterValueByIndex(channel.index, channel.x);
    return channel.x;
  }
}
