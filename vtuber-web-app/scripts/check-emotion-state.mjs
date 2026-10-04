import assert from 'node:assert/strict';
import { EMOTION_FIELDS, isEmotionUpdatePayload } from '../src/types/emotionState.ts';

const state = Object.fromEntries(EMOTION_FIELDS.map(field => [field, 0.5]));
const valid = { type: 'emotion_update', state, source: 'jev' };
assert.equal(isEmotionUpdatePayload(valid), true);
assert.equal(isEmotionUpdatePayload({ ...valid, state: { ...state, shy: NaN } }), false);
assert.equal(isEmotionUpdatePayload({ ...valid, state: { ...state, shy: true } }), false);
assert.equal(isEmotionUpdatePayload({ ...valid, state: { ...state, extra: 0.2 } }), false);
assert.equal(isEmotionUpdatePayload({ ...valid, state: { shy: 0.5 } }), false);
assert.equal(isEmotionUpdatePayload({ ...valid, source: 'chat' }), false);
console.log('emotion_update contract OK');
