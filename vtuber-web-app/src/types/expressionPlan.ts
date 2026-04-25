export interface ExpressionBasePose {
  preset: string
  params: {
    headIntensity: number
    blushLevel: number
    eyeSync: boolean
    eyeLOpen: number
    eyeROpen: number
    mouthForm: number
    browLY: number
    browRY: number
    browLAngle: number
    browRAngle: number
    browLForm: number
    browRForm: number
    eyeLSmile: number
    eyeRSmile: number
    browLX: number
    browRX: number
  }
  durationSec: number
}

export interface ExpressionMicroEventPatch {
  blushLevel?: number
  eyeLOpen?: number
  eyeROpen?: number
  mouthForm?: number
  browLY?: number
  browRY?: number
  browLAngle?: number
  browRAngle?: number
  browLForm?: number
  browRForm?: number
  eyeLSmile?: number
  eyeRSmile?: number
  browLX?: number
  browRX?: number
}

export interface ExpressionMicroEvent {
  kind: string
  durationMs: number
  patch: ExpressionMicroEventPatch
  returnToBase: boolean
}

export interface BlinkCommand {
  action: 'force_blink' | 'pause' | 'resume' | 'set_interval'
  durationSec?: number
  intervalMin?: number
  intervalMax?: number
}

export type BlinkAction = BlinkCommand['action']

export interface ExpressionPlanPayload {
  type: 'expression_plan'
  basePose: ExpressionBasePose
  microEvents: ExpressionMicroEvent[]
  sequence?: ExpressionMicroEvent[]
  blinkPlan: {
    style: string
    commands: BlinkCommand[]
  }
  speakingRate: number
  debug?: Record<string, string>
}

const EXPRESSION_MICRO_EVENT_PATCH_KEYS = [
  'blushLevel',
  'eyeLOpen',
  'eyeROpen',
  'mouthForm',
  'browLY',
  'browRY',
  'browLAngle',
  'browRAngle',
  'browLForm',
  'browRForm',
  'eyeLSmile',
  'eyeRSmile',
  'browLX',
  'browRX',
] as const satisfies ReadonlyArray<keyof ExpressionMicroEventPatch>

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function hasExpressionBasePoseParams(value: unknown): value is ExpressionBasePose['params'] {
  if (!isRecord(value)) {
    return false
  }

  return (
    isNumber(value.headIntensity) &&
    isNumber(value.blushLevel) &&
    typeof value.eyeSync === 'boolean' &&
    isNumber(value.eyeLOpen) &&
    isNumber(value.eyeROpen) &&
    isNumber(value.mouthForm) &&
    isNumber(value.browLY) &&
    isNumber(value.browRY) &&
    isNumber(value.browLAngle) &&
    isNumber(value.browRAngle) &&
    isNumber(value.browLForm) &&
    isNumber(value.browRForm) &&
    isNumber(value.eyeLSmile) &&
    isNumber(value.eyeRSmile) &&
    isNumber(value.browLX) &&
    isNumber(value.browRX)
  )
}

function isBlinkCommand(value: unknown): value is BlinkCommand {
  if (!isRecord(value) || !isBlinkAction(value.action)) {
    return false
  }

  if (value.durationSec !== undefined && !isNumber(value.durationSec)) {
    return false
  }
  if (value.intervalMin !== undefined && !isNumber(value.intervalMin)) {
    return false
  }
  if (value.intervalMax !== undefined && !isNumber(value.intervalMax)) {
    return false
  }

  return true
}

function isExpressionMicroEventPatch(value: unknown): value is ExpressionMicroEventPatch {
  if (!isRecord(value)) {
    return false
  }

  return Object.entries(value).every(([key, patchValue]) => (
    EXPRESSION_MICRO_EVENT_PATCH_KEYS.includes(key as keyof ExpressionMicroEventPatch) &&
    isNumber(patchValue)
  ))
}

function isExpressionMicroEvent(value: unknown): value is ExpressionMicroEvent {
  return (
    isRecord(value) &&
    typeof value.kind === 'string' &&
    isExpressionMicroEventPatch(value.patch) &&
    isNumber(value.durationMs) &&
    typeof value.returnToBase === 'boolean'
  )
}

export function isBlinkAction(value: unknown): value is BlinkAction {
  return value === 'force_blink' || value === 'pause' || value === 'resume' || value === 'set_interval'
}

export function isExpressionPlanPayload(value: unknown): value is ExpressionPlanPayload {
  if (!isRecord(value) || value.type !== 'expression_plan') {
    return false
  }

  const basePose = value.basePose
  if (!isRecord(basePose) || !isNumber(basePose.durationSec) || !hasExpressionBasePoseParams(basePose.params)) {
    return false
  }

  if (!Array.isArray(value.microEvents)) {
    return false
  }

  if (!value.microEvents.every(isExpressionMicroEvent)) {
    return false
  }

  if (value.sequence !== undefined && !Array.isArray(value.sequence)) {
    return false
  }

  if (value.sequence !== undefined && !value.sequence.every(isExpressionMicroEvent)) {
    return false
  }

  const blinkPlan = value.blinkPlan
  if (!isRecord(blinkPlan) || typeof blinkPlan.style !== 'string' || !Array.isArray(blinkPlan.commands)) {
    return false
  }

  if (!blinkPlan.commands.every(isBlinkCommand)) {
    return false
  }

  return isNumber(value.speakingRate)
}
