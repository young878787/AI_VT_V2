export const AMBIENT_AXES = [
  'headX', 'headY', 'headZ', 'bodyX', 'bodyY', 'bodyZ', 'gazeX', 'gazeY',
] as const;

export type AmbientAxis = typeof AMBIENT_AXES[number];
export type AmbientTarget = Record<AmbientAxis, number>;
export type AmbientState = 'idle' | 'listening';

export interface AmbientPlan {
  type: 'ambient_plan';
  sequence: number;
  state: AmbientState;
  moveMs: number;
  target: AmbientTarget;
  breath: { periodSec: number; amplitude: number; rootAmplitude: number };
  hop?: { height: number; durationMs: number };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function inRange(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum;
}

export function isAmbientPlan(value: unknown): value is AmbientPlan {
  if (!isRecord(value) || value.type !== 'ambient_plan') return false;
  if (!Number.isSafeInteger(value.sequence) || (value.sequence as number) <= 0) return false;
  if (value.state !== 'idle' && value.state !== 'listening') return false;
  if (!inRange(value.moveMs, 300, 3000) || !isRecord(value.target) || !isRecord(value.breath)) return false;
  if (Object.keys(value.target).length !== AMBIENT_AXES.length ||
      !AMBIENT_AXES.every(axis => inRange((value.target as Record<string, unknown>)[axis], -1, 1))) return false;
  const breath = value.breath;
  if (!inRange(breath.periodSec, 3, 8) || !inRange(breath.amplitude, 0, 1) ||
      !inRange(breath.rootAmplitude, 0, 0.08)) return false;
  if (value.hop !== undefined && (!isRecord(value.hop) ||
      !inRange(value.hop.height, 0, 0.12) || !inRange(value.hop.durationMs, 400, 1600))) return false;
  return true;
}
