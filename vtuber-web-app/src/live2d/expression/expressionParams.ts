import type { ExpressionBasePose, ExpressionMicroEventPatch } from '../../types/expressionPlan';

export type ExpressionParamPatch = ExpressionMicroEventPatch;
export type ExpressionOverlayKey = keyof ExpressionParamPatch | 'headIntensity';
export type BasePoseParams = ExpressionBasePose['params'];

export interface ActiveExpressionEvent {
  kind: string;
  patch: ExpressionParamPatch;
  durationMs: number;
  startedAtMs: number;
  returnToBase: boolean;
}

export function clampExpressionOverlayValue(key: ExpressionOverlayKey, value: number): number {
  switch (key) {
    case 'headIntensity':
      return Math.max(0, Math.min(0.95, value));
    case 'breathLevel':
    case 'physicsImpulse':
      return Math.max(0, Math.min(1, value));
    case 'bodyAngleX':
    case 'bodyAngleY':
    case 'bodyAngleZ':
      return Math.max(-1, Math.min(1, value));
    case 'blushLevel':
      return Math.max(-1, Math.min(1, value));
    case 'eyeLOpen':
    case 'eyeROpen':
      return Math.max(0, Math.min(2, value));
    case 'mouthForm':
      return Math.max(-2, Math.min(1, value));
    case 'browLY':
    case 'browRY':
    case 'browLAngle':
    case 'browRAngle':
    case 'browLForm':
    case 'browRForm':
    case 'browLX':
    case 'browRX':
      return Math.max(-1, Math.min(1, value));
    case 'eyeLSmile':
    case 'eyeRSmile':
      return Math.max(0, Math.min(1, value));
  }
}

export function shuffleIndices(count: number): number[] {
  const indices = Array.from({ length: count }, (_, index) => index);
  for (let index = indices.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [indices[index], indices[swapIndex]] = [indices[swapIndex], indices[index]];
  }
  return indices;
}

export function randomBetween(min: number, max: number): number {
  return min + (Math.random() * (max - min));
}
