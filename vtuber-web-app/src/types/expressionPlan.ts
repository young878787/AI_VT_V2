export type BodyMotionStyle =
  | 'calm_sway'
  | 'bright_bounce'
  | 'playful_swing'
  | 'small_sad_bob'
  | 'heavy_slow_sink'
  | 'locked_tense'
  | 'shy_side_sway'
  | 'quick_recoil'
  | 'uneasy_shift'

export interface BodyMotionProfile {
  style: BodyMotionStyle
  speed: number
  swayScale: number
  bobScale: number
  twistScale: number
  breathScale: number
  headScale: number
}

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
    bodyAngleX: number
    bodyAngleY: number
    bodyAngleZ: number
    breathLevel: number
    physicsImpulse: number
  }
  bodyMotionProfile?: BodyMotionProfile
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
  bodyAngleX?: number
  bodyAngleY?: number
  bodyAngleZ?: number
  breathLevel?: number
  physicsImpulse?: number
}

export interface ExpressionMicroEvent {
  kind: string
  durationMs: number
  fadeInMs?: number
  fadeOutMs?: number
  patch: ExpressionMicroEventPatch
  returnToBase: boolean
}

export interface ExpressionIdleAmbientState {
  kind: 'ambient_idle_breath' | 'ambient_idle_look_around' | 'ambient_idle_active_shift'
  params: ExpressionBasePose['params']
}

export interface ExpressionIdleAmbientPlan {
  states: ExpressionIdleAmbientState[]
}

export interface ExpressionIdlePlan {
  name: 'happy_idle' | 'crying_idle' | 'angry_glare_idle' | 'shy_idle' | 'gloomy_idle'
  mode: 'loop'
  enterAfterMs: number
  loopIntervalMs: number
  ambientEnterAfterMs?: number
  ambientSwitchIntervalMs?: number
  interruptible: boolean
  source?: {
    actionEnterAfterMs?: number
    speakingEnterAfterMs?: number
    postSpeechHoldMs?: number
  }
  settlePose: ExpressionBasePose
  loopEvents: ExpressionMicroEvent[]
  ambientPlan?: ExpressionIdleAmbientPlan
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
  'bodyAngleX',
  'bodyAngleY',
  'bodyAngleZ',
  'breathLevel',
  'physicsImpulse',
] as const satisfies ReadonlyArray<keyof ExpressionMicroEventPatch>

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function isNonNegativeNumber(value: unknown): value is number {
  return isNumber(value) && value >= 0
}

function isBodyMotionStyle(value: unknown): value is BodyMotionStyle {
  return (
    value === 'calm_sway' ||
    value === 'bright_bounce' ||
    value === 'playful_swing' ||
    value === 'small_sad_bob' ||
    value === 'heavy_slow_sink' ||
    value === 'locked_tense' ||
    value === 'shy_side_sway' ||
    value === 'quick_recoil' ||
    value === 'uneasy_shift'
  )
}

function isBodyMotionProfile(value: unknown): value is BodyMotionProfile {
  return (
    isRecord(value) &&
    isBodyMotionStyle(value.style) &&
    isNumber(value.speed) &&
    isNumber(value.swayScale) &&
    isNumber(value.bobScale) &&
    isNumber(value.twistScale) &&
    isNumber(value.breathScale) &&
    isNumber(value.headScale)
  )
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
    isNumber(value.browRX) &&
    isNumber(value.bodyAngleX) &&
    isNumber(value.bodyAngleY) &&
    isNumber(value.bodyAngleZ) &&
    isNumber(value.breathLevel) &&
    isNumber(value.physicsImpulse)
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
    isNonNegativeNumber(value.durationMs) &&
    (value.fadeInMs === undefined || isNonNegativeNumber(value.fadeInMs)) &&
    (value.fadeOutMs === undefined || isNonNegativeNumber(value.fadeOutMs)) &&
    typeof value.returnToBase === 'boolean'
  )
}

function isExpressionIdleAmbientState(value: unknown): value is ExpressionIdleAmbientState {
  return (
    isRecord(value) &&
    (
      value.kind === 'ambient_idle_breath' ||
      value.kind === 'ambient_idle_look_around' ||
      value.kind === 'ambient_idle_active_shift'
    ) &&
    hasExpressionBasePoseParams(value.params)
  )
}

function isExpressionIdleAmbientPlan(value: unknown): value is ExpressionIdleAmbientPlan {
  return (
    isRecord(value) &&
    Array.isArray(value.states) &&
    value.states.length === 3 &&
    value.states.every(isExpressionIdleAmbientState)
  )
}

function isExpressionBasePose(value: unknown): value is ExpressionBasePose {
  return (
    isRecord(value) &&
    typeof value.preset === 'string' &&
    isNumber(value.durationSec) &&
    hasExpressionBasePoseParams(value.params) &&
    (value.bodyMotionProfile === undefined || isBodyMotionProfile(value.bodyMotionProfile))
  )
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
    (
      value.ambientEnterAfterMs === undefined ||
      isNumber(value.ambientEnterAfterMs)
    ) &&
    (
      value.ambientSwitchIntervalMs === undefined ||
      isNumber(value.ambientSwitchIntervalMs)
    ) &&
    typeof value.interruptible === 'boolean' &&
    (
      value.source === undefined ||
      (
        isRecord(value.source) &&
        (value.source.actionEnterAfterMs === undefined || isNumber(value.source.actionEnterAfterMs)) &&
        (value.source.speakingEnterAfterMs === undefined || isNumber(value.source.speakingEnterAfterMs)) &&
        (value.source.postSpeechHoldMs === undefined || isNumber(value.source.postSpeechHoldMs))
      )
    ) &&
    isExpressionBasePose(value.settlePose) &&
    Array.isArray(value.loopEvents) &&
    value.loopEvents.every(isExpressionMicroEvent) &&
    (
      value.ambientPlan === undefined ||
      (
        isExpressionIdleAmbientPlan(value.ambientPlan) &&
        isNumber(value.ambientEnterAfterMs) &&
        isNumber(value.ambientSwitchIntervalMs)
      )
    )
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
