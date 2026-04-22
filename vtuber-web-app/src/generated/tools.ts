// ============================================
// 自動生成檔案 — 請勿手動修改
// 來源：backend/domain/tools_schema.json
// 命令：node scripts/generate-tools-ts.js
// ============================================

export interface ParamDef {
  key: string;
  backendKey: string;
  label: string;
  min: number;
  max: number;
  step: number;
  emoji: string;
  color: string;
  default: number;
}

export const PARAM_DEFS: ParamDef[] = [
  {
    key: 'headIntensity',
    backendKey: 'head_intensity',
    label: '身體活動幅度',
    min: 0,
    max: 1,
    step: 0.01,
    emoji: '🎭',
    color: '#c4b5fd',
    default: 0,
  },
  {
    key: 'blushLevel',
    backendKey: 'blush_level',
    label: '臉頰狀態',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '🌸',
    color: '#f9a8d4',
    default: 0,
  },
  {
    key: 'eyeLOpen',
    backendKey: 'eye_l_open',
    label: '左眼張開',
    min: 0,
    max: 2,
    step: 0.01,
    emoji: '👁',
    color: '#7dd3fc',
    default: 1,
  },
  {
    key: 'eyeROpen',
    backendKey: 'eye_r_open',
    label: '右眼張開',
    min: 0,
    max: 2,
    step: 0.01,
    emoji: '👁',
    color: '#7dd3fc',
    default: 1,
  },
  {
    key: 'durationSec',
    backendKey: 'duration_sec',
    label: '動作持續秒數',
    min: 2,
    max: 20,
    step: 0.5,
    emoji: '⏱',
    color: '#94a3b8',
    default: 5,
  },
  {
    key: 'mouthForm',
    backendKey: 'mouth_form',
    label: '嘴角形狀',
    min: -2,
    max: 1,
    step: 0.01,
    emoji: '😊',
    color: '#86efac',
    default: 0,
  },
  {
    key: 'browLY',
    backendKey: 'brow_l_y',
    label: '左眉高低',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '⬆',
    color: '#fde68a',
    default: 0,
  },
  {
    key: 'browRY',
    backendKey: 'brow_r_y',
    label: '右眉高低',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '⬆',
    color: '#fde68a',
    default: 0,
  },
  {
    key: 'browLAngle',
    backendKey: 'brow_l_angle',
    label: '左眉角度',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '↗',
    color: '#fde68a',
    default: 0,
  },
  {
    key: 'browRAngle',
    backendKey: 'brow_r_angle',
    label: '右眉角度',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '↗',
    color: '#fde68a',
    default: 0,
  },
  {
    key: 'browLForm',
    backendKey: 'brow_l_form',
    label: '左眉彎曲',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '〜',
    color: '#fde68a',
    default: 0,
  },
  {
    key: 'browRForm',
    backendKey: 'brow_r_form',
    label: '右眉彎曲',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '〜',
    color: '#fde68a',
    default: 0,
  },
  {
    key: 'eyeLSmile',
    backendKey: 'eye_l_smile',
    label: '左眼笑眼',
    min: 0,
    max: 1,
    step: 0.01,
    emoji: '😊',
    color: '#bae6fd',
    default: 0,
  },
  {
    key: 'eyeRSmile',
    backendKey: 'eye_r_smile',
    label: '右眼笑眼',
    min: 0,
    max: 1,
    step: 0.01,
    emoji: '😊',
    color: '#bae6fd',
    default: 0,
  },
  {
    key: 'browLX',
    backendKey: 'brow_l_x',
    label: '左眉水平',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '↔',
    color: '#fef08a',
    default: 0,
  },
  {
    key: 'browRX',
    backendKey: 'brow_r_x',
    label: '右眉水平',
    min: -1,
    max: 1,
    step: 0.01,
    emoji: '↔',
    color: '#fef08a',
    default: 0,
  }
];

export const MISSING_PARAMS: {
  backendKey: string;
  label: string;
  emoji: string;
  reason: string;
}[] = [
  {
    backendKey: 'speaking_rate',
    label: '語音語速',
    emoji: '🗣',
    reason: '後端已透過 TTS 處理，前端 WebSocket → appStore → TTS 鏈路尚未接入此參數',
  }
];

export const DEFAULT_PARAMS: Record<string, number> = {
  headIntensity: 0,
  blushLevel: 0,
  eyeLOpen: 1,
  eyeROpen: 1,
  durationSec: 5,
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
  browRX: 0
};

export const EYE_SYNC_DEFAULT = true;

export const COVERAGE_INFO = { connected: 17, total: 18 };

export const LIVE2D_TOOL_NAME = 'set_ai_behavior';

export const BLINK_CONTROL_TOOL = {
  name: 'blink_control',
  description: '控制 Live2D 模型的眨眼行為。用於強制眨眼、暫停/恢復自動眨眼、調整眨眼頻率。AI 可以主動使用來模擬更自然的互動。',
  actions: ["force_blink","pause","resume","set_interval"],
} as const;
