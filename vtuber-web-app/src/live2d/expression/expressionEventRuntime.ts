import {
  clampExpressionOverlayValue,
  type ActiveExpressionEvent,
  type BasePoseParams,
  type ExpressionParamPatch,
} from './expressionParams';
import { copyBasePoseParams } from './expressionState';

export interface ExpressionEventRuntimeResult {
  activeEvents: ActiveExpressionEvent[];
  targets: BasePoseParams;
}

function applyOverlayValue(
  key: keyof ExpressionParamPatch,
  target: number,
  patchValue: number | undefined,
  fade: number,
): number {
  if (typeof patchValue !== 'number') {
    return target;
  }
  const clampedPatchValue = clampExpressionOverlayValue(key, patchValue);
  return clampExpressionOverlayValue(key, target + (clampedPatchValue - target) * fade);
}

export function applyActiveExpressionEvents(
  targets: BasePoseParams,
  activeEvents: ActiveExpressionEvent[],
  nowMs: number,
): ExpressionEventRuntimeResult {
  const nextTargets = copyBasePoseParams(targets);
  const nextActiveEvents = activeEvents.filter((event) => nowMs - event.startedAtMs < event.durationMs);

  for (const event of nextActiveEvents) {
    const durationMs = Math.max(1, event.durationMs);
    const progress = Math.min(1, (nowMs - event.startedAtMs) / durationMs);
    const fade = event.returnToBase ? 1 - progress : 1;

    nextTargets.blushLevel = applyOverlayValue('blushLevel', nextTargets.blushLevel, event.patch.blushLevel, fade);
    nextTargets.bodyAngleX = applyOverlayValue('bodyAngleX', nextTargets.bodyAngleX, event.patch.bodyAngleX, fade);
    nextTargets.bodyAngleY = applyOverlayValue('bodyAngleY', nextTargets.bodyAngleY, event.patch.bodyAngleY, fade);
    nextTargets.bodyAngleZ = applyOverlayValue('bodyAngleZ', nextTargets.bodyAngleZ, event.patch.bodyAngleZ, fade);
    nextTargets.breathLevel = applyOverlayValue('breathLevel', nextTargets.breathLevel, event.patch.breathLevel, fade);
    nextTargets.physicsImpulse = applyOverlayValue('physicsImpulse', nextTargets.physicsImpulse, event.patch.physicsImpulse, fade);
    nextTargets.eyeLOpen = applyOverlayValue('eyeLOpen', nextTargets.eyeLOpen, event.patch.eyeLOpen, fade);
    nextTargets.eyeROpen = applyOverlayValue('eyeROpen', nextTargets.eyeROpen, event.patch.eyeROpen, fade);
    nextTargets.mouthForm = applyOverlayValue('mouthForm', nextTargets.mouthForm, event.patch.mouthForm, fade);
    nextTargets.browLY = applyOverlayValue('browLY', nextTargets.browLY, event.patch.browLY, fade);
    nextTargets.browRY = applyOverlayValue('browRY', nextTargets.browRY, event.patch.browRY, fade);
    nextTargets.browLAngle = applyOverlayValue('browLAngle', nextTargets.browLAngle, event.patch.browLAngle, fade);
    nextTargets.browRAngle = applyOverlayValue('browRAngle', nextTargets.browRAngle, event.patch.browRAngle, fade);
    nextTargets.browLForm = applyOverlayValue('browLForm', nextTargets.browLForm, event.patch.browLForm, fade);
    nextTargets.browRForm = applyOverlayValue('browRForm', nextTargets.browRForm, event.patch.browRForm, fade);
    nextTargets.eyeLSmile = applyOverlayValue('eyeLSmile', nextTargets.eyeLSmile, event.patch.eyeLSmile, fade);
    nextTargets.eyeRSmile = applyOverlayValue('eyeRSmile', nextTargets.eyeRSmile, event.patch.eyeRSmile, fade);
    nextTargets.browLX = applyOverlayValue('browLX', nextTargets.browLX, event.patch.browLX, fade);
    nextTargets.browRX = applyOverlayValue('browRX', nextTargets.browRX, event.patch.browRX, fade);
  }

  return {
    activeEvents: nextActiveEvents,
    targets: nextTargets,
  };
}
