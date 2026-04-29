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

export interface ExpressionIdlePlan {
  name: 'happy_idle' | 'crying_idle' | 'angry_glare_idle' | 'shy_idle' | 'gloomy_idle'
  mode: 'loop'
  enterAfterMs: number
  loopIntervalMs: number
  interruptible: boolean
  source?: {
    actionEnterAfterMs?: number
    speakingEnterAfterMs?: number
  }
  settlePose: ExpressionBasePose
  loopEvents: ExpressionMicroEvent[]
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
  sequence: ExpressionMicroEvent[]
  idlePlan?: ExpressionIdlePlan
  blinkPlan: {
    style: string
    commands: BlinkCommand[]
  }
  speakingRate: number
  timingHints?: Record<string, number>
  modelHints?: Record<string, string | number | boolean>
  debug?: Record<string, string | number | boolean>
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
  if (value.action === 'set_interval') {
    if (value.intervalMin === undefined || value.intervalMax === undefined) {
      return false
    }
    if (value.intervalMin > value.intervalMax) {
      return false
    }
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

function isExpressionBasePose(value: unknown): value is ExpressionBasePose {
  return isRecord(value) && typeof value.preset === 'string' && isNumber(value.durationSec) && hasExpressionBasePoseParams(value.params)
}

function isExpressionIdlePlan(value: unknown): value is ExpressionIdlePlan {
  if (!isRecord(value)) {
    return false
  }

  if (
    value.name !== 'happy_idle' &&
    value.name !== 'crying_idle' &&
    value.name !== 'angry_glare_idle' &&
    value.name !== 'shy_idle' &&
    value.name !== 'gloomy_idle'
  ) {
    return false
  }

  return (
    value.mode === 'loop' &&
    isNumber(value.enterAfterMs) &&
    isNumber(value.loopIntervalMs) &&
    typeof value.interruptible === 'boolean' &&
    (
      value.source === undefined ||
      (
        isRecord(value.source) &&
        (value.source.actionEnterAfterMs === undefined || isNumber(value.source.actionEnterAfterMs)) &&
        (value.source.speakingEnterAfterMs === undefined || isNumber(value.source.speakingEnterAfterMs))
      )
    ) &&
    isExpressionBasePose(value.settlePose) &&
    Array.isArray(value.loopEvents) &&
    value.loopEvents.every(isExpressionMicroEvent)
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
  if (!isExpressionBasePose(basePose)) {
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

  if (!Array.isArray(value.sequence)) {
    return false
  }

  if (!value.sequence.every(isExpressionMicroEvent)) {
    return false
  }

  if (value.idlePlan !== undefined && !isExpressionIdlePlan(value.idlePlan)) {
    return false
  }

  if (value.timingHints !== undefined) {
    if (!isRecord(value.timingHints)) {
      return false
    }
    if (!Object.values(value.timingHints).every(isNumber)) {
      return false
    }
  }

  if (value.modelHints !== undefined) {
    if (!isRecord(value.modelHints)) {
      return false
    }
    if (!Object.values(value.modelHints).every((hintValue) => (
      typeof hintValue === 'string' || typeof hintValue === 'number' || typeof hintValue === 'boolean'
    ))) {
      return false
    }
  }

  if (value.debug !== undefined) {
    if (!isRecord(value.debug)) {
      return false
    }
    if (!Object.values(value.debug).every((debugValue) => (
      typeof debugValue === 'string' || typeof debugValue === 'number' || typeof debugValue === 'boolean'
    ))) {
      return false
    }
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
