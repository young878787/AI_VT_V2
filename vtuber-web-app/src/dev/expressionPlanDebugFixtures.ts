import type {
  BodyMotionProfile,
  BodyMotionStyle,
  ExpressionBasePose,
  ExpressionIdleAmbientState,
  ExpressionIdlePlan,
  ExpressionMicroEvent,
  ExpressionMotionPlan,
  ExpressionPlanPayload,
} from '../types/expressionPlan';

export type DebugExpressionKind =
  | 'happy'
  | 'playful'
  | 'teasing'
  | 'angry'
  | 'sad'
  | 'gloomy'
  | 'shy'
  | 'surprised'
  | 'conflicted';

export type DebugExpressionIntensity = 'soft' | 'normal' | 'strong';
export type DebugMotionKind =
  | 'buoyant_bounce'
  | 'side_sway_bounce'
  | 'lean_in_pop'
  | 'swing_tease'
  | 'locked_glare'
  | 'tuck_side_sway';

export interface DebugExpressionOptions {
  includeIdle: boolean;
  includeAmbient: boolean;
  includeMicroEvents: boolean;
  intensity: DebugExpressionIntensity;
}

export interface DebugExpressionPreset {
  kind: DebugExpressionKind;
  label: string;
  preset: string;
  motionStyle: BodyMotionStyle;
  idleName: ExpressionIdlePlan['name'];
  accent: string;
}

export interface DebugMotionPreset {
  kind: DebugMotionKind;
  label: string;
  expressionKind: DebugExpressionKind;
  theme: string;
  variant: string;
  accent: string;
}

export interface DebugExpressionBackendIntent {
  primary_emotion: DebugExpressionKind
  emotion: DebugExpressionKind
  performance_mode: string
  intensity: number
  energy: number
  playfulness: number
  warmth: number
  dominance: number
  arc: string
  hold_ms: number
  speaking_rate: number
  spoken_text: string
  blink_style: string
  motion_theme?: string
  motion_variant?: string
  topic_guard: {
    source_theme: string
    must_preserve_theme: boolean
  }
}

interface DebugPresetConfig extends DebugExpressionPreset {
  basePatch: Partial<ExpressionBasePose['params']>;
  motionProfile: BodyMotionProfile;
  microEvents: ExpressionMicroEvent[];
}

const BACKEND_INTENT_RULES: Record<DebugExpressionKind, Omit<DebugExpressionBackendIntent, 'spoken_text' | 'topic_guard'>> = {
  happy: {
    primary_emotion: 'happy',
    emotion: 'happy',
    performance_mode: 'bright_talk',
    intensity: 0.62,
    energy: 0.72,
    playfulness: 0.45,
    warmth: 0.82,
    dominance: 0.34,
    arc: 'steady',
    hold_ms: 1800,
    speaking_rate: 1.04,
    blink_style: 'normal',
  },
  playful: {
    primary_emotion: 'playful',
    emotion: 'playful',
    performance_mode: 'goofy_face',
    intensity: 0.78,
    energy: 0.82,
    playfulness: 0.9,
    warmth: 0.62,
    dominance: 0.42,
    arc: 'pop_then_settle',
    hold_ms: 1700,
    speaking_rate: 1.1,
    blink_style: 'teasing_pause',
  },
  teasing: {
    primary_emotion: 'teasing',
    emotion: 'teasing',
    performance_mode: 'smug',
    intensity: 0.58,
    energy: 0.54,
    playfulness: 0.72,
    warmth: 0.42,
    dominance: 0.58,
    arc: 'steady',
    hold_ms: 1900,
    speaking_rate: 1,
    blink_style: 'teasing_pause',
  },
  angry: {
    primary_emotion: 'angry',
    emotion: 'angry',
    performance_mode: 'meltdown',
    intensity: 0.86,
    energy: 0.76,
    playfulness: 0.06,
    warmth: 0.12,
    dominance: 0.76,
    arc: 'glare_then_flatten',
    hold_ms: 2100,
    speaking_rate: 1.02,
    blink_style: 'focused_pause',
  },
  sad: {
    primary_emotion: 'sad',
    emotion: 'sad',
    performance_mode: 'tense_hold',
    intensity: 0.72,
    energy: 0.28,
    playfulness: 0.04,
    warmth: 0.3,
    dominance: 0.16,
    arc: 'shrink_then_recover',
    hold_ms: 2300,
    speaking_rate: 0.9,
    blink_style: 'sleepy_slow',
  },
  gloomy: {
    primary_emotion: 'gloomy',
    emotion: 'gloomy',
    performance_mode: 'deadpan',
    intensity: 0.64,
    energy: 0.18,
    playfulness: 0.02,
    warmth: 0.12,
    dominance: 0.28,
    arc: 'steady',
    hold_ms: 2400,
    speaking_rate: 0.88,
    blink_style: 'sleepy_slow',
  },
  shy: {
    primary_emotion: 'shy',
    emotion: 'shy',
    performance_mode: 'awkward',
    intensity: 0.58,
    energy: 0.42,
    playfulness: 0.18,
    warmth: 0.58,
    dominance: 0.16,
    arc: 'shrink_then_recover',
    hold_ms: 2100,
    speaking_rate: 0.96,
    blink_style: 'shy_fast',
  },
  surprised: {
    primary_emotion: 'surprised',
    emotion: 'surprised',
    performance_mode: 'shock_recoil',
    intensity: 0.82,
    energy: 0.86,
    playfulness: 0.2,
    warmth: 0.32,
    dominance: 0.28,
    arc: 'pop_then_settle',
    hold_ms: 1500,
    speaking_rate: 1.08,
    blink_style: 'surprised_hold',
  },
  conflicted: {
    primary_emotion: 'conflicted',
    emotion: 'conflicted',
    performance_mode: 'volatile',
    intensity: 0.68,
    energy: 0.58,
    playfulness: 0.12,
    warmth: 0.24,
    dominance: 0.46,
    arc: 'steady',
    hold_ms: 2200,
    speaking_rate: 0.98,
    blink_style: 'focused_pause',
  },
};

const INTENSITY_SCALE: Record<DebugExpressionIntensity, number> = {
  soft: 0.72,
  normal: 1,
  strong: 1.28,
};

export const MOTION_DEBUG_PRESETS: DebugMotionPreset[] = [
  {
    kind: 'buoyant_bounce',
    label: '開心彈跳',
    expressionKind: 'happy',
    theme: 'happy_bright_talk',
    variant: 'buoyant_bounce',
    accent: '#22c55e',
  },
  {
    kind: 'side_sway_bounce',
    label: '左右彈擺',
    expressionKind: 'happy',
    theme: 'happy_bright_talk',
    variant: 'side_sway_bounce',
    accent: '#06b6d4',
  },
  {
    kind: 'lean_in_pop',
    label: '前傾回彈',
    expressionKind: 'happy',
    theme: 'happy_bright_talk',
    variant: 'lean_in_pop',
    accent: '#84cc16',
  },
  {
    kind: 'swing_tease',
    label: '調皮擺頭',
    expressionKind: 'playful',
    theme: 'playful_tease',
    variant: 'swing_tease',
    accent: '#a855f7',
  },
  {
    kind: 'locked_glare',
    label: '鎖定瞪視',
    expressionKind: 'angry',
    theme: 'angry_tension',
    variant: 'locked_glare',
    accent: '#ef4444',
  },
  {
    kind: 'tuck_side_sway',
    label: '害羞側縮',
    expressionKind: 'shy',
    theme: 'shy_tucked',
    variant: 'tuck_side_sway',
    accent: '#f472b6',
  },
];

const NEUTRAL_PARAMS: ExpressionBasePose['params'] = {
  headIntensity: 0,
  blushLevel: 0,
  eyeSync: true,
  eyeLOpen: 1,
  eyeROpen: 1,
  mouthForm: 0,
  browLY: 0,
  browRY: 0,
  browLAngle: 0,
  browRAngle: 0,
  browLForm: 0,
  browRForm: 0,
  eyeLSmile: 0,
  eyeRSmile: 0,
  browLX: 0,
  browRX: 0,
  bodyAngleX: 0,
  bodyAngleY: 0,
  bodyAngleZ: 0,
  breathLevel: 0.35,
  physicsImpulse: 0.12,
};

const DEFAULT_MOTION_PROFILE: BodyMotionProfile = {
  style: 'calm_sway',
  speed: 1.12,
  swayScale: 1.08,
  bobScale: 1.08,
  twistScale: 1.04,
  breathScale: 1.04,
  headScale: 1.08,
};

const DEBUG_PRESET_CONFIGS: DebugPresetConfig[] = [
  {
    kind: 'happy',
    label: '開心',
    preset: 'happy_bright_talk',
    motionStyle: 'bright_bounce',
    idleName: 'happy_idle',
    accent: '#22c55e',
    basePatch: {
      headIntensity: 0.62,
      blushLevel: 0.12,
      mouthForm: 0.42,
      eyeLSmile: 0.7,
      eyeRSmile: 0.7,
      bodyAngleX: 0.28,
      bodyAngleY: 0.12,
      bodyAngleZ: -0.08,
      breathLevel: 0.72,
      physicsImpulse: 0.68,
    },
    motionProfile: {
      style: 'bright_bounce',
      speed: 1.46,
      swayScale: 1.64,
      bobScale: 1.58,
      twistScale: 1.08,
      breathScale: 1.22,
      headScale: 1.28,
    },
    microEvents: [
      {
        kind: 'debug_happy_lift',
        durationMs: 820,
        fadeInMs: 160,
        fadeOutMs: 260,
        patch: { mouthForm: 0.58, bodyAngleX: 0.44, physicsImpulse: 0.86 },
        returnToBase: true,
      },
    ],
  },
  {
    kind: 'playful',
    label: '活潑',
    preset: 'playful_goofy_face',
    motionStyle: 'playful_swing',
    idleName: 'happy_idle',
    accent: '#a855f7',
    basePatch: {
      headIntensity: 0.7,
      blushLevel: 0.08,
      mouthForm: 0.36,
      browLX: -0.14,
      browRX: 0.14,
      eyeLSmile: 0.64,
      eyeRSmile: 0.48,
      bodyAngleX: 0.36,
      bodyAngleY: 0.18,
      bodyAngleZ: -0.18,
      breathLevel: 0.74,
      physicsImpulse: 0.78,
    },
    motionProfile: {
      style: 'playful_swing',
      speed: 1.58,
      swayScale: 1.88,
      bobScale: 1.56,
      twistScale: 1.42,
      breathScale: 1.2,
      headScale: 1.38,
    },
    microEvents: [
      {
        kind: 'debug_playful_swing',
        durationMs: 960,
        fadeInMs: 180,
        fadeOutMs: 300,
        patch: { bodyAngleX: -0.48, bodyAngleZ: 0.3, physicsImpulse: 0.94 },
        returnToBase: true,
      },
    ],
  },
  {
    kind: 'teasing',
    label: '調皮',
    preset: 'teasing_smug',
    motionStyle: 'playful_swing',
    idleName: 'happy_idle',
    accent: '#ec4899',
    basePatch: {
      headIntensity: 0.46,
      blushLevel: 0.04,
      mouthForm: 0.24,
      browLY: 0.14,
      browRY: -0.02,
      browLAngle: 0.16,
      browRAngle: -0.16,
      eyeLOpen: 0.82,
      eyeROpen: 0.92,
      eyeLSmile: 0.42,
      eyeRSmile: 0.34,
      bodyAngleX: 0.18,
      bodyAngleY: 0.06,
      bodyAngleZ: -0.22,
      breathLevel: 0.58,
      physicsImpulse: 0.52,
    },
    motionProfile: {
      style: 'playful_swing',
      speed: 1.38,
      swayScale: 1.54,
      bobScale: 1.24,
      twistScale: 1.46,
      breathScale: 1.1,
      headScale: 1.18,
    },
    microEvents: [
      {
        kind: 'debug_teasing_tilt',
        durationMs: 760,
        fadeInMs: 140,
        fadeOutMs: 260,
        patch: { browLX: -0.24, browRX: 0.24, bodyAngleZ: -0.38 },
        returnToBase: true,
      },
    ],
  },
  {
    kind: 'angry',
    label: '生氣',
    preset: 'angry_meltdown',
    motionStyle: 'locked_tense',
    idleName: 'angry_glare_idle',
    accent: '#ef4444',
    basePatch: {
      headIntensity: 0.38,
      blushLevel: -0.72,
      mouthForm: -0.22,
      browLY: -0.36,
      browRY: -0.36,
      browLAngle: -0.34,
      browRAngle: 0.34,
      browLForm: -0.18,
      browRForm: -0.18,
      eyeLOpen: 1,
      eyeROpen: 0.88,
      bodyAngleX: 0.22,
      bodyAngleY: -0.22,
      bodyAngleZ: 0.24,
      breathLevel: 0.68,
      physicsImpulse: 0.82,
    },
    motionProfile: {
      style: 'locked_tense',
      speed: 1.2,
      swayScale: 0.72,
      bobScale: 0.92,
      twistScale: 1.78,
      breathScale: 1.08,
      headScale: 1.02,
    },
    microEvents: [
      {
        kind: 'debug_angry_snap',
        durationMs: 620,
        fadeInMs: 80,
        fadeOutMs: 220,
        patch: { eyeLOpen: 1, eyeROpen: 1, bodyAngleZ: 0.42, physicsImpulse: 1 },
        returnToBase: true,
      },
    ],
  },
  {
    kind: 'sad',
    label: '難過',
    preset: 'sad_tense_hold',
    motionStyle: 'small_sad_bob',
    idleName: 'crying_idle',
    accent: '#60a5fa',
    basePatch: {
      headIntensity: 0.16,
      blushLevel: -0.38,
      mouthForm: -0.18,
      browLY: -0.22,
      browRY: -0.22,
      browLAngle: -0.14,
      browRAngle: 0.14,
      eyeLOpen: 0.68,
      eyeROpen: 0.68,
      bodyAngleX: -0.16,
      bodyAngleY: -0.34,
      bodyAngleZ: -0.08,
      breathLevel: 0.42,
      physicsImpulse: 0.32,
    },
    motionProfile: {
      style: 'small_sad_bob',
      speed: 0.9,
      swayScale: 0.34,
      bobScale: 2.22,
      twistScale: 0.34,
      breathScale: 0.9,
      headScale: 0.9,
    },
    microEvents: [
      {
        kind: 'debug_sad_dip',
        durationMs: 980,
        fadeInMs: 260,
        fadeOutMs: 320,
        patch: { eyeLOpen: 0.54, eyeROpen: 0.54, bodyAngleY: -0.48 },
        returnToBase: true,
      },
    ],
  },
  {
    kind: 'gloomy',
    label: '陰沉',
    preset: 'gloomy_deadpan',
    motionStyle: 'heavy_slow_sink',
    idleName: 'gloomy_idle',
    accent: '#64748b',
    basePatch: {
      headIntensity: 0.1,
      blushLevel: -0.24,
      mouthForm: -0.1,
      browLY: -0.16,
      browRY: -0.16,
      eyeLOpen: 0.58,
      eyeROpen: 0.58,
      bodyAngleX: -0.22,
      bodyAngleY: -0.24,
      bodyAngleZ: 0.04,
      breathLevel: 0.34,
      physicsImpulse: 0.22,
    },
    motionProfile: {
      style: 'heavy_slow_sink',
      speed: 0.72,
      swayScale: 0.3,
      bobScale: 1.34,
      twistScale: 0.32,
      breathScale: 0.74,
      headScale: 0.6,
    },
    microEvents: [
      {
        kind: 'debug_gloomy_sink',
        durationMs: 1200,
        fadeInMs: 320,
        fadeOutMs: 340,
        patch: { bodyAngleY: -0.4, eyeLOpen: 0.5, eyeROpen: 0.5 },
        returnToBase: true,
      },
    ],
  },
  {
    kind: 'shy',
    label: '害羞',
    preset: 'shy_tucked',
    motionStyle: 'shy_side_sway',
    idleName: 'shy_idle',
    accent: '#f472b6',
    basePatch: {
      headIntensity: 0.24,
      blushLevel: 0.36,
      mouthForm: 0.06,
      browLY: 0.1,
      browRY: 0.1,
      eyeLOpen: 0.72,
      eyeROpen: 0.72,
      eyeLSmile: 0.18,
      eyeRSmile: 0.18,
      bodyAngleX: -0.18,
      bodyAngleY: 0.18,
      bodyAngleZ: -0.22,
      breathLevel: 0.62,
      physicsImpulse: 0.44,
    },
    motionProfile: {
      style: 'shy_side_sway',
      speed: 1.08,
      swayScale: 0.96,
      bobScale: 1.2,
      twistScale: 1,
      breathScale: 1.08,
      headScale: 0.96,
    },
    microEvents: [
      {
        kind: 'debug_shy_tuck',
        durationMs: 900,
        fadeInMs: 200,
        fadeOutMs: 280,
        patch: { blushLevel: 0.46, bodyAngleZ: -0.34, eyeLOpen: 0.62, eyeROpen: 0.62 },
        returnToBase: true,
      },
    ],
  },
  {
    kind: 'surprised',
    label: '驚訝',
    preset: 'surprised_open',
    motionStyle: 'quick_recoil',
    idleName: 'happy_idle',
    accent: '#f59e0b',
    basePatch: {
      headIntensity: 0.66,
      blushLevel: 0.02,
      mouthForm: 0.5,
      browLY: 0.26,
      browRY: 0.26,
      eyeLOpen: 1,
      eyeROpen: 1,
      bodyAngleX: 0.24,
      bodyAngleY: 0.28,
      bodyAngleZ: 0.08,
      breathLevel: 0.82,
      physicsImpulse: 0.88,
    },
    motionProfile: {
      style: 'quick_recoil',
      speed: 1.66,
      swayScale: 1.04,
      bobScale: 1.94,
      twistScale: 1.08,
      breathScale: 1.42,
      headScale: 1.42,
    },
    microEvents: [
      {
        kind: 'debug_surprised_recoil',
        durationMs: 520,
        fadeInMs: 70,
        fadeOutMs: 220,
        patch: { bodyAngleY: 0.46, mouthForm: 0.68, physicsImpulse: 1 },
        returnToBase: true,
      },
    ],
  },
  {
    kind: 'conflicted',
    label: '矛盾',
    preset: 'conflicted_volatile',
    motionStyle: 'uneasy_shift',
    idleName: 'angry_glare_idle',
    accent: '#14b8a6',
    basePatch: {
      headIntensity: 0.36,
      blushLevel: -0.22,
      mouthForm: -0.04,
      browLY: -0.18,
      browRY: -0.08,
      browLAngle: -0.18,
      browRAngle: 0.1,
      eyeLOpen: 0.9,
      eyeROpen: 0.8,
      bodyAngleX: 0.08,
      bodyAngleY: 0.16,
      bodyAngleZ: -0.28,
      breathLevel: 0.64,
      physicsImpulse: 0.62,
    },
    motionProfile: {
      style: 'uneasy_shift',
      speed: 1.18,
      swayScale: 1.06,
      bobScale: 1.08,
      twistScale: 1.34,
      breathScale: 1.12,
      headScale: 1.02,
    },
    microEvents: [
      {
        kind: 'debug_conflicted_shift',
        durationMs: 860,
        fadeInMs: 160,
        fadeOutMs: 320,
        patch: { bodyAngleX: -0.26, bodyAngleZ: 0.22, eyeROpen: 0.68 },
        returnToBase: true,
      },
    ],
  },
];

export const EXPRESSION_DEBUG_PRESETS: DebugExpressionPreset[] = DEBUG_PRESET_CONFIGS.map(
  ({ kind, label, preset, motionStyle, idleName, accent }) => ({
    kind,
    label,
    preset,
    motionStyle,
    idleName,
    accent,
  }),
);

export const DEFAULT_DEBUG_EXPRESSION_OPTIONS: DebugExpressionOptions = {
  includeIdle: true,
  includeAmbient: true,
  includeMicroEvents: true,
  intensity: 'normal',
};

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

function randomBetween(minimum: number, maximum: number): number {
  return minimum + (Math.random() * (maximum - minimum));
}

function randomInt(minimum: number, maximum: number): number {
  return Math.floor(randomBetween(minimum, maximum + 1));
}

function scaled(value: number, scale: number): number {
  if (value === 0) {
    return 0;
  }
  return value * scale;
}

function cloneParams(params: ExpressionBasePose['params']): ExpressionBasePose['params'] {
  return { ...params };
}

function scaleParams(
  params: ExpressionBasePose['params'],
  scale: number,
): ExpressionBasePose['params'] {
  return {
    ...params,
    headIntensity: clamp(scaled(params.headIntensity, scale), 0, 1),
    blushLevel: clamp(scaled(params.blushLevel, scale), -1, 1),
    mouthForm: clamp(scaled(params.mouthForm, scale), -1, 1),
    browLY: clamp(scaled(params.browLY, scale), -1, 1),
    browRY: clamp(scaled(params.browRY, scale), -1, 1),
    browLAngle: clamp(scaled(params.browLAngle, scale), -1, 1),
    browRAngle: clamp(scaled(params.browRAngle, scale), -1, 1),
    browLForm: clamp(scaled(params.browLForm, scale), -1, 1),
    browRForm: clamp(scaled(params.browRForm, scale), -1, 1),
    eyeLSmile: clamp(scaled(params.eyeLSmile, scale), 0, 1),
    eyeRSmile: clamp(scaled(params.eyeRSmile, scale), 0, 1),
    browLX: clamp(scaled(params.browLX, scale), -1, 1),
    browRX: clamp(scaled(params.browRX, scale), -1, 1),
    bodyAngleX: clamp(scaled(params.bodyAngleX, scale), -1, 1),
    bodyAngleY: clamp(scaled(params.bodyAngleY, scale), -1, 1),
    bodyAngleZ: clamp(scaled(params.bodyAngleZ, scale), -1, 1),
    breathLevel: clamp(params.breathLevel * (0.9 + (scale * 0.12)), 0, 1),
    physicsImpulse: clamp(params.physicsImpulse * scale, 0, 1),
  };
}

function jitterParams(params: ExpressionBasePose['params']): ExpressionBasePose['params'] {
  return {
    ...params,
    bodyAngleX: clamp(params.bodyAngleX + randomBetween(-0.08, 0.08), -1, 1),
    bodyAngleY: clamp(params.bodyAngleY + randomBetween(-0.05, 0.05), -1, 1),
    bodyAngleZ: clamp(params.bodyAngleZ + randomBetween(-0.08, 0.08), -1, 1),
    breathLevel: clamp(params.breathLevel + randomBetween(-0.04, 0.05), 0, 1),
    physicsImpulse: clamp(params.physicsImpulse + randomBetween(-0.08, 0.08), 0, 1),
  };
}

function buildBasePose(
  config: DebugPresetConfig,
  options: DebugExpressionOptions,
): ExpressionBasePose {
  const scale = INTENSITY_SCALE[options.intensity];
  const params = jitterParams(scaleParams({
    ...NEUTRAL_PARAMS,
    ...config.basePatch,
  }, scale));

  const profileScale = options.intensity === 'strong' ? 1.08 : options.intensity === 'soft' ? 0.94 : 1;
  const motionProfile: BodyMotionProfile = {
    ...config.motionProfile,
    speed: config.motionProfile.speed * profileScale,
    swayScale: config.motionProfile.swayScale * profileScale,
    bobScale: config.motionProfile.bobScale * profileScale,
    twistScale: config.motionProfile.twistScale * profileScale,
    breathScale: config.motionProfile.breathScale,
    headScale: config.motionProfile.headScale * profileScale,
  };

  return {
    preset: config.preset,
    params,
    bodyMotionProfile: motionProfile,
    durationSec: randomBetween(3.8, 5.2),
  };
}

function buildMotionPlan(
  preset: DebugMotionPreset | undefined,
  options: DebugExpressionOptions,
): ExpressionMotionPlan {
  const scale = INTENSITY_SCALE[options.intensity];
  const theme = preset?.theme ?? 'happy_bright_talk';
  const variant = preset?.variant ?? 'buoyant_bounce';
  const baseByVariant: Record<string, Omit<ExpressionMotionPlan, 'theme' | 'variant' | 'phaseSeed'>> = {
    buoyant_bounce: {
      durationMs: 4600,
      blendInMs: 420,
      blendOutMs: 820,
      body: { sway: 1.14, bob: 1.62, twist: 0.92, spring: 0.72 },
      head: { yaw: 0.92, pitch: 1.18, roll: 1.06, lagMs: 110 },
    },
    side_sway_bounce: {
      durationMs: 5200,
      blendInMs: 480,
      blendOutMs: 900,
      body: { sway: 1.46, bob: 1.32, twist: 1.08, spring: 0.48 },
      head: { yaw: 1.12, pitch: 0.92, roll: 1.22, lagMs: 150 },
    },
    lean_in_pop: {
      durationMs: 4200,
      blendInMs: 360,
      blendOutMs: 760,
      body: { sway: 0.94, bob: 1.48, twist: 0.82, spring: 0.9 },
      head: { yaw: 0.84, pitch: 1.38, roll: 0.86, lagMs: 90 },
    },
    swing_tease: {
      durationMs: 5600,
      blendInMs: 460,
      blendOutMs: 880,
      body: { sway: 1.58, bob: 1.16, twist: 1.34, spring: 0.5 },
      head: { yaw: 1.18, pitch: 0.76, roll: 1.42, lagMs: 170 },
    },
    locked_glare: {
      durationMs: 5200,
      blendInMs: 280,
      blendOutMs: 720,
      body: { sway: 0.58, bob: 0.72, twist: 1.54, spring: 0.16 },
      head: { yaw: 0.64, pitch: 0.62, roll: 0.82, lagMs: 40 },
    },
    tuck_side_sway: {
      durationMs: 5400,
      blendInMs: 520,
      blendOutMs: 920,
      body: { sway: 1.02, bob: 1.24, twist: 1.08, spring: 0.34 },
      head: { yaw: 0.86, pitch: 0.92, roll: 1.22, lagMs: 170 },
    },
  };
  const base = baseByVariant[variant] ?? baseByVariant.buoyant_bounce;
  const multiplier = options.intensity === 'strong' ? 1.08 : options.intensity === 'soft' ? 0.92 : 1;

  return {
    theme,
    variant,
    phaseSeed: randomBetween(0, Math.PI * 2),
    durationMs: Math.round(base.durationMs * (0.96 + (scale * 0.06))),
    blendInMs: base.blendInMs,
    blendOutMs: base.blendOutMs,
    body: {
      sway: base.body.sway * multiplier,
      bob: base.body.bob * multiplier,
      twist: base.body.twist * multiplier,
      spring: base.body.spring,
    },
    head: {
      yaw: base.head.yaw * multiplier,
      pitch: base.head.pitch * multiplier,
      roll: base.head.roll * multiplier,
      lagMs: base.head.lagMs,
    },
  };
}

function scaleMicroEventPatch(
  event: ExpressionMicroEvent,
  scale: number,
): ExpressionMicroEvent {
  const patch = Object.fromEntries(
    Object.entries(event.patch).map(([key, value]) => [key, clamp(value * scale, -1, 1)]),
  ) as ExpressionMicroEvent['patch'];

  return {
    ...event,
    durationMs: Math.round(event.durationMs * randomBetween(0.92, 1.12)),
    patch,
  };
}

function buildAmbientState(
  kind: 'ambient_idle_breath' | 'ambient_idle_look_around' | 'ambient_idle_active_shift',
  baseParams: ExpressionBasePose['params'],
): ExpressionIdleAmbientState {
  const params = cloneParams(baseParams);

  if (kind === 'ambient_idle_breath') {
    params.breathLevel = clamp(params.breathLevel + 0.06, 0, 1);
    params.physicsImpulse = clamp(params.physicsImpulse * 0.72, 0, 1);
  } else if (kind === 'ambient_idle_look_around') {
    params.headIntensity = clamp(params.headIntensity + 0.05, 0, 1);
    params.bodyAngleX = clamp(params.bodyAngleX + 0.08, -1, 1);
    params.bodyAngleZ = clamp(params.bodyAngleZ - 0.05, -1, 1);
  } else {
    params.bodyAngleX = clamp(params.bodyAngleX - 0.08, -1, 1);
    params.bodyAngleY = clamp(params.bodyAngleY + 0.04, -1, 1);
    params.physicsImpulse = clamp(params.physicsImpulse + 0.08, 0, 1);
  }

  return { kind, params };
}

function buildIdlePlan(
  config: DebugPresetConfig,
  basePose: ExpressionBasePose,
  options: DebugExpressionOptions,
): ExpressionIdlePlan {
  const settleParams = {
    ...basePose.params,
    headIntensity: clamp(basePose.params.headIntensity * 0.42, 0, 1),
    bodyAngleX: clamp(basePose.params.bodyAngleX * 0.42, -1, 1),
    bodyAngleY: clamp(basePose.params.bodyAngleY * 0.62, -1, 1),
    bodyAngleZ: clamp(basePose.params.bodyAngleZ * 0.42, -1, 1),
    physicsImpulse: clamp(basePose.params.physicsImpulse * 0.36, 0, 1),
  };

  const settlePose: ExpressionBasePose = {
    preset: config.idleName,
    params: settleParams,
    bodyMotionProfile: {
      ...DEFAULT_MOTION_PROFILE,
      style: config.motionStyle,
      speed: Math.max(0.65, config.motionProfile.speed * 0.72),
      swayScale: Math.max(0.3, config.motionProfile.swayScale * 0.48),
      bobScale: Math.max(0.4, config.motionProfile.bobScale * 0.52),
      twistScale: Math.max(0.3, config.motionProfile.twistScale * 0.46),
      breathScale: config.motionProfile.breathScale,
      headScale: Math.max(0.3, config.motionProfile.headScale * 0.5),
    },
    durationSec: 12,
  };

  const ambientPlan = options.includeAmbient
    ? {
        states: [
          buildAmbientState('ambient_idle_breath', settleParams),
          buildAmbientState('ambient_idle_look_around', settleParams),
          buildAmbientState('ambient_idle_active_shift', settleParams),
        ],
      }
    : undefined;

  return {
    name: config.idleName,
    mode: 'loop',
    enterAfterMs: randomInt(6000, 10000),
    loopIntervalMs: randomInt(1800, 3200),
    ambientEnterAfterMs: options.includeAmbient ? randomInt(11000, 15000) : undefined,
    ambientSwitchIntervalMs: options.includeAmbient ? randomInt(5200, 7800) : undefined,
    interruptible: true,
    source: {
      actionEnterAfterMs: Math.round(basePose.durationSec * 1000),
      speakingEnterAfterMs: randomInt(2200, 4200),
      postSpeechHoldMs: randomInt(6000, 10000),
    },
    settlePose,
    loopEvents: [
      {
        kind: `debug_${config.idleName}_loop`,
        durationMs: randomInt(620, 940),
        fadeInMs: 180,
        fadeOutMs: 260,
        patch: {
          bodyAngleX: clamp(settleParams.bodyAngleX + randomBetween(-0.12, 0.12), -1, 1),
          bodyAngleY: clamp(settleParams.bodyAngleY + randomBetween(-0.06, 0.08), -1, 1),
          breathLevel: clamp(settleParams.breathLevel + 0.08, 0, 1),
          physicsImpulse: clamp(settleParams.physicsImpulse + 0.1, 0, 1),
        },
        returnToBase: true,
      },
    ],
    ambientPlan,
  };
}

function getConfig(kind: DebugExpressionKind): DebugPresetConfig {
  const config = DEBUG_PRESET_CONFIGS.find((preset) => preset.kind === kind);
  if (!config) {
    throw new Error(`Unknown debug expression kind: ${kind}`);
  }
  return config;
}

function getMotionPreset(kind: DebugMotionKind): DebugMotionPreset {
  const preset = MOTION_DEBUG_PRESETS.find((item) => item.kind === kind);
  if (!preset) {
    throw new Error(`Unknown debug motion kind: ${kind}`);
  }
  return preset;
}

function getDefaultMotionPreset(kind: DebugExpressionKind): DebugMotionPreset | undefined {
  return MOTION_DEBUG_PRESETS.find((preset) => preset.expressionKind === kind);
}

export function getRandomDebugExpressionKind(): DebugExpressionKind {
  const index = Math.floor(Math.random() * DEBUG_PRESET_CONFIGS.length);
  return DEBUG_PRESET_CONFIGS[index].kind;
}

export function createBackendDebugExpressionIntent(
  kind: DebugExpressionKind,
  options: DebugExpressionOptions = DEFAULT_DEBUG_EXPRESSION_OPTIONS,
): DebugExpressionBackendIntent {
  const rule = BACKEND_INTENT_RULES[kind];
  const scale = INTENSITY_SCALE[options.intensity];
  const label = getConfig(kind).label;

  return {
    ...rule,
    intensity: clamp(rule.intensity * scale, 0, 1),
    energy: clamp(rule.energy * (0.9 + (scale * 0.12)), 0, 1),
    playfulness: clamp(rule.playfulness * scale, 0, 1),
    warmth: clamp(rule.warmth * (0.92 + (scale * 0.08)), 0, 1),
    dominance: clamp(rule.dominance * (0.94 + (scale * 0.08)), 0, 1),
    hold_ms: Math.round(rule.hold_ms * (options.intensity === 'strong' ? 1.12 : options.intensity === 'soft' ? 0.88 : 1)),
    speaking_rate: options.intensity === 'strong'
      ? Math.min(1.18, rule.speaking_rate + 0.06)
      : options.intensity === 'soft'
        ? Math.max(0.84, rule.speaking_rate - 0.05)
        : rule.speaking_rate,
    spoken_text: `前端測試 ${label} 動作，請保留目前主要表情一段時間。`,
    topic_guard: {
      source_theme: 'daily_talk',
      must_preserve_theme: true,
    },
  };
}

export function createBackendDebugMotionIntent(
  kind: DebugMotionKind,
  options: DebugExpressionOptions = DEFAULT_DEBUG_EXPRESSION_OPTIONS,
): DebugExpressionBackendIntent {
  const motionPreset = getMotionPreset(kind);
  return {
    ...createBackendDebugExpressionIntent(motionPreset.expressionKind, options),
    motion_theme: motionPreset.theme,
    motion_variant: motionPreset.variant,
    spoken_text: `前端測試 ${motionPreset.label} motionPlan，請保留主要表情並讓動作連續。`,
  };
}

export function createDebugExpressionPlan(
  kind: DebugExpressionKind,
  options: DebugExpressionOptions = DEFAULT_DEBUG_EXPRESSION_OPTIONS,
  motionPreset: DebugMotionPreset | undefined = getDefaultMotionPreset(kind),
): ExpressionPlanPayload {
  const config = getConfig(kind);
  const basePose = buildBasePose(config, options);
  const scale = INTENSITY_SCALE[options.intensity];
  const microEvents = options.includeMicroEvents
    ? config.microEvents.map((event) => scaleMicroEventPatch(event, scale))
    : [];

  return {
    type: 'expression_plan',
    basePose,
    microEvents,
    sequence: options.includeMicroEvents ? microEvents.slice(0, 1) : [],
    motionPlan: buildMotionPlan(motionPreset, options),
    idlePlan: options.includeIdle ? buildIdlePlan(config, basePose, options) : undefined,
    blinkPlan: {
      style: kind,
      commands: [
        { action: 'resume' },
        {
          action: 'set_interval',
          intervalMin: options.intensity === 'strong' ? 0.72 : 0.9,
          intervalMax: options.intensity === 'soft' ? 2.2 : 1.6,
        },
      ],
    },
    speakingRate: options.intensity === 'strong' ? 1.12 : options.intensity === 'soft' ? 0.92 : 1,
    timingHints: {
      debugGeneratedAt: Date.now(),
      basePoseDurationMs: Math.round(basePose.durationSec * 1000),
    },
    modelHints: {
      modelName: 'frontend-debug',
      preset: config.preset,
      bodyMotionProfile: config.motionStyle,
      motionTheme: motionPreset?.theme ?? 'none',
      motionVariant: motionPreset?.variant ?? 'none',
    },
    debug: {
      source: 'frontend_expression_plan_debug',
      intentEmotion: kind,
      selectedBasePreset: config.preset,
      bodyMotionProfile: config.motionStyle,
      motionTheme: motionPreset?.theme ?? 'none',
      motionVariant: motionPreset?.variant ?? 'none',
      idlePlan: config.idleName,
      intensity: options.intensity,
    },
  };
}

export function createDebugMotionExpressionPlan(
  kind: DebugMotionKind,
  options: DebugExpressionOptions = DEFAULT_DEBUG_EXPRESSION_OPTIONS,
): ExpressionPlanPayload {
  const motionPreset = getMotionPreset(kind);
  return createDebugExpressionPlan(motionPreset.expressionKind, options, motionPreset);
}

export function createNeutralExpressionPlan(): ExpressionPlanPayload {
  const basePose: ExpressionBasePose = {
    preset: 'debug_neutral',
    params: cloneParams(NEUTRAL_PARAMS),
    bodyMotionProfile: DEFAULT_MOTION_PROFILE,
    durationSec: 1.4,
  };

  return {
    type: 'expression_plan',
    basePose,
    microEvents: [],
    sequence: [],
    blinkPlan: {
      style: 'neutral',
      commands: [
        { action: 'resume' },
        { action: 'set_interval', intervalMin: 1.1, intervalMax: 2.4 },
      ],
    },
    speakingRate: 1,
    timingHints: {
      debugGeneratedAt: Date.now(),
      basePoseDurationMs: 1400,
    },
    modelHints: {
      modelName: 'frontend-debug',
      preset: 'debug_neutral',
      bodyMotionProfile: DEFAULT_MOTION_PROFILE.style,
    },
    debug: {
      source: 'frontend_expression_plan_debug',
      intentEmotion: 'neutral',
      selectedBasePreset: 'debug_neutral',
      bodyMotionProfile: DEFAULT_MOTION_PROFILE.style,
      idlePlan: 'none',
    },
  };
}
