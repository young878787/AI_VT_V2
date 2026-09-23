export const EMOTION_FIELDS = [
  'shy',
  'pleased',
  'genuinely_angry',
  'sad_or_hurt',
  'masking_positive_feeling',
  'wants_continue_interaction',
] as const;

export type EmotionField = typeof EMOTION_FIELDS[number];
export type EmotionState = Record<EmotionField, number>;
export type EmotionSource = 'jev' | 'previous_fallback' | 'neutral_fallback';

export interface EmotionUpdatePayload {
  type: 'emotion_update';
  state: EmotionState;
  source: EmotionSource;
}

export function isEmotionUpdatePayload(value: unknown): value is EmotionUpdatePayload {
  if (typeof value !== 'object' || value === null) return false;
  const payload = value as Record<string, unknown>;
  if (payload.type !== 'emotion_update') return false;
  if (typeof payload.source !== 'string' || !['jev', 'previous_fallback', 'neutral_fallback'].includes(payload.source)) return false;
  if (typeof payload.state !== 'object' || payload.state === null || Array.isArray(payload.state)) return false;
  const state = payload.state as Record<string, unknown>;
  if (Object.keys(state).length !== EMOTION_FIELDS.length) return false;
  return EMOTION_FIELDS.every(field =>
    typeof state[field] === 'number' && Number.isFinite(state[field]) && state[field] >= 0 && state[field] <= 1
  );
}
