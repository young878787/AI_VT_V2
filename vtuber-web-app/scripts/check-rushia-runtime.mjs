import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// 執行原始 TypeScript；只替換瀏覽器／WebGL 邊界，不複製被驗證的邏輯。
function createLoader(overrides = {}, globals = {}) {
  const cache = new Map();
  const load = (relativePath) => {
    const filename = path.resolve(root, relativePath);
    if (cache.has(filename)) return cache.get(filename).exports;
    const module = { exports: {} };
    cache.set(filename, module);
    // Vite 的 import.meta.env 在 Node CJS 測試中由空白環境取代。
    const source = fs.readFileSync(filename, 'utf8').replaceAll('import.meta.env', '__testImportMeta.env');
    const code = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
      fileName: filename,
    }).outputText;
    vm.runInNewContext(code, {
      module,
      exports: module.exports,
      console,
      __testImportMeta: { env: {} },
      ...globals,
      require: (specifier) => {
        if (Object.hasOwn(overrides, specifier)) return overrides[specifier];
        if (!specifier.startsWith('.')) throw new Error(`Missing test boundary: ${specifier}`);
        return load(path.relative(root, path.resolve(path.dirname(filename), `${specifier}.ts`)));
      },
    }, { filename });
    return module.exports;
  };
  return load;
}

function near(actual, expected, label, tolerance = 1e-8) {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: expected ${expected}, got ${actual}`);
}

const load = createLoader();
const { resolveHalfBodyFraming } = load('src/live2d/modelFraming.ts');
const { applyActiveExpressionEvents } = load('src/live2d/expression/expressionEventRuntime.ts');
const { createNeutralTargetParams } = load('src/live2d/expression/expressionState.ts');
const defines = load('src/live2d/LAppDefine.ts');
const fixedModel = defines.getDefaultModel();
assert.equal(defines.getModelJsonPath(fixedModel), '/Resources/Rushia/RushiaHD.model3.json');
assert.equal(defines.AvailableModels, undefined, 'the frontend must have no selectable model list');

// 縮放只改變取景大小；頭頂仍位於畫面頂部內側，且 Retina 不改構圖。
const viewports = [[1280, 800], [768, 1024], [390, 400], [375, 700]];
for (const [width, height] of viewports) {
  const normal = resolveHalfBodyFraming(width, height);
  const retina = resolveHalfBodyFraming(width * 2, height * 2);
  near(retina.scale, normal.scale, 'pixel density preserves scale');
  near(retina.y, normal.y, 'pixel density preserves position');
  let previousScale = 0;
  for (const zoom of [0.75, 1, 1.25]) {
    const framing = resolveHalfBodyFraming(width, height, zoom);
    assert.ok(framing.scale > previousScale, 'zoom must increase the model size');
    near(framing.scale / normal.scale, zoom, 'zoom is relative to responsive framing');
    // Rushia 頭頂校準點為模型座標 y=0.98；NDC 的 1 為畫面頂部。
    const headTopNdc = 0.98 * framing.scale + framing.y;
    const topMargin = (1 - headTopNdc) / 2;
    assert.ok(topMargin >= 0.05 && topMargin <= 0.09, 'head needs a stable visible margin');
    previousScale = framing.scale;
  }
  near(resolveHalfBodyFraming(width, height, 100).scale,
    resolveHalfBodyFraming(width, height, 1.25).scale, 'upper zoom limit');
  near(resolveHalfBodyFraming(width, height, 0).scale,
    resolveHalfBodyFraming(width, height, 0.75).scale, 'lower zoom limit');
}
assert.ok(resolveHalfBodyFraming(375, 700).scale < resolveHalfBodyFraming(1280, 800).scale,
  'portrait framing must leave room for the shoulders');
for (const size of [[0, 0], [0, 400], [400, 0]]) {
  const framing = resolveHalfBodyFraming(...size);
  assert.ok(Number.isFinite(framing.scale) && Number.isFinite(framing.y), 'unmeasured canvas stays finite');
}

// 驚訝張嘴應淡入、保持、淡出回到原表情，不能在結束後殘留。
const base = { ...createNeutralTargetParams(), mouthOpenBias: 0.1, eyeROpen: 0.85 };
const mouthEvent = {
  kind: 'reaction', patch: { mouthOpenBias: 0.8 }, startedAtMs: 500,
  durationMs: 1000, fadeInMs: 200, fadeOutMs: 300, returnToBase: true,
};
const mouthSamples = [[400, 0.1], [500, 0.1], [600, 0.45], [900, 0.8], [1350, 0.45], [1500, 0.1]];
for (const [time, expected] of mouthSamples) {
  const result = applyActiveExpressionEvents(base, [mouthEvent], time);
  near(result.targets.mouthOpenBias, expected, `mouth envelope at ${time}ms`);
}
assert.equal(applyActiveExpressionEvents(base, [mouthEvent], 400).activeEvents.length, 1,
  'future events remain queued');
assert.equal(applyActiveExpressionEvents(base, [mouthEvent], 1500).activeEvents.length, 0,
  'finished events are removed');
near(base.mouthOpenBias, 0.1, 'sampling must not mutate the base pose');

const eyeEvent = {
  kind: 'wink', patch: { eyeLOpen: -0.2, eyeROpen: 1.4, mouthOpenBias: 3 },
  startedAtMs: 0, durationMs: 1000, returnToBase: false,
};
const clamped = applyActiveExpressionEvents(base, [eyeEvent], 500).targets;
near(clamped.eyeLOpen, 0, 'closed eye lower bound');
near(clamped.eyeROpen, 1, 'Rushia open eye upper bound');
near(clamped.mouthOpenBias, 1, 'mouth upper bound');
const wink = { ...eyeEvent, patch: { eyeLOpen: 0 } };
near(applyActiveExpressionEvents(base, [wink], 500).targets.eyeROpen, 0.85,
  'a wink must not overwrite the other eye');
near(applyActiveExpressionEvents(base, [wink], 1000).targets.eyeLOpen, 1,
  'the wink must release back to the current base pose');

// 直接測試 LAppModel 與真實眨眼器的合成；不初始化 WebGL renderer。
const sdkNames = [
  'cubismmodelsettingjson', 'math/cubismmatrix44', 'math/cubismmodelmatrix',
  'cubismdefaultparameterid', 'live2dcubismframework', 'effect/cubismeyeblink',
  'effect/cubismbreath', 'motion/cubismmotionqueuemanager', 'motion/acubismmotion',
  'motion/cubismmotion',
];
let modelNowMs = 0;
const modelLoad = createLoader({
  ...Object.fromEntries(sdkNames.map(name => [`@framework/${name}`, {}])),
  '@framework/model/cubismusermodel': { CubismUserModel: class {} },
  './LAppPal': {}, './LAppTextureManager': {}, './LAppDelegate': {},
}, { performance: { now: () => modelNowMs } });
const { LAppModel } = modelLoad('src/live2d/LAppModel.ts');
const { CubismEyeBlink } = load('src/live2d/framework/effect/cubismeyeblink.ts');
const model = Object.create(LAppModel.prototype);
const parameters = new Map([['left', 0.5], ['right', 0.3]]);
model._model = {
  setParameterValueById: (id, value) => parameters.set(id, value),
  getParameterValueById: id => parameters.get(id),
};
model._idParamEyeLOpen = 'left';
model._idParamEyeROpen = 'right';
model._aiEyeBaseLOpen = 0.5;
model._aiEyeBaseROpen = 0.3;
model._eyeBlink = CubismEyeBlink.create();
model._eyeBlink.setParameterIds(['left', 'right']);
model._eyeBlink.pause();
for (let frame = 0; frame < 30; frame += 1) {
  model.updateEyeBlink(1 / 60);
  near(parameters.get('left'), 0.5, 'paused blink preserves half-closed left eye');
  near(parameters.get('right'), 0.3, 'paused blink preserves asymmetric right eye');
}
model._eyeBlink.resume();
model._eyeBlink.forceBlink();
let minimumLeft = 1;
for (let frame = 0; frame < 45; frame += 1) {
  model.updateEyeBlink(1 / 60);
  minimumLeft = Math.min(minimumLeft, parameters.get('left'));
  near(parameters.get('left') / 0.5, parameters.get('right') / 0.3,
    'blink keeps the expression eye ratio');
}
assert.ok(minimumLeft < 0.05, 'resumed blink must visibly close the eyes');
near(parameters.get('left'), 0.5, 'completed blink restores the left expression');
near(parameters.get('right'), 0.3, 'completed blink restores the right expression');

// 完整 LAppModel 更新流程；SDK 傳輸／曲線求值為替身，合成順序使用實際 runtime。
let nativeDeltaSec = 1 / 60;
const nativeLoad = createLoader({
  ...Object.fromEntries(sdkNames.map(name => [`@framework/${name}`, {}])),
  '@framework/model/cubismusermodel': { CubismUserModel: class {} },
  '@framework/math/cubismmodelmatrix': { CubismModelMatrix: class {} },
  '@framework/cubismdefaultparameterid': { CubismDefaultParameterId: new Proxy({}, { get: (_, id) => id }) },
  '@framework/live2dcubismframework': { CubismFramework: { getIdManager: () => ({ getId: id => id }) } },
  '@framework/motion/cubismmotionqueuemanager': { InvalidMotionQueueEntryHandleValue: -1 },
  '@framework/motion/cubismmotion': { CubismMotion: { create(buffer) {
    const data = JSON.parse(new TextDecoder().decode(buffer));
    return { data, setFadeInTime() {}, setFadeOutTime() {}, setEffectIds() {}, setLoop(loop) { this.loop = loop; },
      getDuration: () => data.Meta.Duration };
  } } },
  './LAppPal': { LAppPal: { getDeltaTime: () => nativeDeltaSec, log() {}, printWarning() {} } },
  './LAppTextureManager': { LAppTextureManager: class {} }, './LAppDelegate': {},
}, {
  performance: { now: () => modelNowMs }, TextDecoder,
  fetch: async file => ({ ok: !file.includes('missing'), status: 404,
    arrayBuffer: async () => new TextEncoder().encode(JSON.stringify(file.includes('exp3')
      ? { Parameters: [{ Id: 'ParamBrowLY' }] }
      : { Meta: { Duration: 3.6, Loop: file.includes('loop') }, Curves: [
        { Target: 'Parameter', Id: 'ParamEyeLOpen' }, { Target: 'Parameter', Id: 'ParamBrowLY' },
        { Target: 'Parameter', Id: 'ParamMouthOpenY' }, { Target: 'Parameter', Id: 'ParamAngleZ' },
        { Target: 'Parameter', Id: 'ParamEyeBallX' }, { Target: 'Parameter', Id: 'ParamEyeBallY' },
        { Target: 'Parameter', Id: 'ParamAngleX' }, { Target: 'Parameter', Id: 'ParamAngleY' },
      ] })).buffer }),
});
const { LAppModel: NativeModel } = nativeLoad('src/live2d/LAppModel.ts');
const nativeModel = new NativeModel();
const nativeValues = new Map();
let savedNativeValues = new Map();
const nativeIds = ['ParamEyeLOpen', 'ParamBrowLY', 'ParamMouthOpenY', 'ParamAngleZ',
  'ParamEyeBallX', 'ParamEyeBallY', 'ParamAngleX', 'ParamAngleY'];
const defaultValue = id => id.includes('Eye') && id.includes('Open') ? 1 : 0;
nativeModel._model = {
  setParameterValueById: (id, value) => nativeValues.set(id, value),
  getParameterValueById: id => nativeValues.get(id) ?? defaultValue(id),
  addParameterValueById: (id, value) => nativeValues.set(id, (nativeValues.get(id) ?? defaultValue(id)) + value),
  getParameterIndex: id => nativeIds.indexOf(id), getParameterCount: () => nativeIds.length,
  getParameterDefaultValue: index => defaultValue(nativeIds[index]),
  saveParameters() { savedNativeValues = new Map(nativeValues); },
  loadParameters() { nativeValues.clear(); for (const [id, value] of savedNativeValues) nativeValues.set(id, value); },
  update() {},
};
let currentEntry = null;
let currentMotion = null;
let queueTime = 0;
nativeModel._motionManager = {
  startMotionPriority(motion) { currentMotion = motion; currentEntry = { finished: false,
    setIsFinished(value) { this.finished = value; }, getStateTime: () => queueTime, getStartTime: () => 0 };
    queueTime = 0; return currentEntry; },
  isFinished: () => !currentEntry || currentEntry.finished,
  isFinishedByHandle: handle => !handle || handle.finished,
  getCubismMotionQueueEntry: handle => handle,
  updateMotion(_, delta) { queueTime += delta;
    for (const [id, value] of [['ParamEyeLOpen', 0.15], ['ParamBrowLY', 0.7], ['ParamMouthOpenY', 0.8], ['ParamAngleZ', 9],
      ['ParamEyeBallX', -0.65], ['ParamEyeBallY', 0.35], ['ParamAngleX', -11], ['ParamAngleY', 4]])
      nativeValues.set(id, value);
    if (!currentMotion.loop && queueTime >= currentMotion.data.Meta.Duration) currentEntry.finished = true;
  },
};
nativeModel._dragManager = { update() {}, getX: () => 0, getY: () => 0 };
nativeModel._modelSetting = {
  getJson: () => ({ getRoot: () => ({ getValueByString: () => ({ getValueByString: () => ({
    getValueByString: () => ({ getValueByIndex: index => ({ getValueByString: () => ({
      isString: () => index === 1,
      getRawString: () => ' 思考 · 專注點頭 ',
    }) }) }),
  }) }) }) }),
  getMotionGroupCount: () => 1, getMotionGroupName: () => 'Action', getMotionCount: () => 3,
  getMotionFileName: (_, index) => ['missing.motion3.json', 'nod.motion3.json', 'loop.motion3.json'][index],
  getMotionFadeInTimeValue: () => -1, getMotionFadeOutTimeValue: () => -1,
  getExpressionCount: () => 1, getExpressionName: () => 'happy', getExpressionFileName: () => 'happy.exp3.json',
};
nativeModel.loadExpression = () => ({ expression: true });
nativeModel._modelHomeDir = '/assets/';
await nativeModel.loadMotions();
await nativeModel.loadExpressions();
const nativeCatalog = nativeModel.getNativeMotionCatalog();
assert.equal(nativeCatalog[1].name, '思考 · 專注點頭', 'native labels use the authored manifest name');
assert.equal(nativeCatalog[2].name, 'loop', 'unnamed native motions retain their filename label');
assert.deepEqual(Array.from(nativeCatalog, entry => [entry.index, entry.status]), [[0, 'failed'], [1, 'loaded'], [2, 'loaded']],
  'failed loading retains manifest index and remains visible');
assert.equal(nativeModel.getMotionCount('Action'), 3);
assert.equal(nativeModel.startMotion('Action', 0, 3), -1, 'failed manifest slot cannot play the next asset');
assert.equal(nativeModel.getNativeExpressionCatalog()[0].status, 'loaded');
nativeModel.setAutoMotionSuspended(false);
nativeModel.startMotion('Action', 2, 1);
assert.equal(nativeModel.getNativeActionState(), null, 'automatic Idle never owns preview parameters');
assert.equal(currentMotion.loop, true, 'manifest loop is explicitly applied to the SDK runtime');
nativeModel.setAutoMotionSuspended(true);
assert.equal(currentEntry.finished, true, 'preview stops an already running automatic motion');
assert.equal(nativeModel.startMotion('Action', 2, 1), -1, 'automatic timers cannot restart motion during preview');
nativeModel._nativeReleaseValues.clear();
nativeModel._dragManager = { update() {}, getX: () => 1, getY: () => -1 };
nativeModel.applyBasePose({ params: { ...createNeutralTargetParams(), eyeBallX: 0.8, eyeBallY: -0.7 }, durationSec: 10 });
nativeModel.startMotion('Action', 1, 3);
let physicsFrames = 0;
nativeModel._physics = { evaluate() { physicsFrames++; nativeValues.set('hair', 0.4); } };
nativeModel._breath = { updateParameters() { nativeValues.set('ParamAngleZ', -5); } };
nativeModel.setAutoEffectsEnabled(true);
for (let frame = 0; frame < 24; frame++) { modelNowMs += nativeDeltaSec * 1000; nativeModel.update(); }
near(nativeValues.get('ParamEyeLOpen'), 0.15, 'native eye curve survives procedural blink composition');
near(nativeValues.get('ParamBrowLY'), 0.7, 'native brow curve survives expression composition');
near(nativeValues.get('ParamMouthOpenY'), 0.8, 'native mouth curve works without TTS');
near(nativeValues.get('ParamAngleZ'), 9, 'native pose survives breathing before physics');
near(nativeValues.get('ParamEyeBallX'), -0.65, 'Force-owned horizontal gaze survives opposite pointer and intent');
near(nativeValues.get('ParamEyeBallY'), 0.35, 'Force-owned vertical gaze survives opposite pointer and intent');
near(nativeValues.get('ParamAngleX'), -11, 'Force-owned head yaw survives the procedural gaze-follow layer');
near(nativeValues.get('ParamAngleY'), 4, 'Force-owned head pitch survives the procedural gaze-follow layer');
assert.ok(physicsFrames > 0, 'physics keeps updating during preview');
nativeModel.setSpeaking(true);
nativeModel.setLipSyncValue(0.35);
nativeModel.update();
near(nativeValues.get('ParamMouthOpenY'), 0.35, 'TTS overrides the native mouth opening');
nativeModel._nativeParamOverrides.set('ParamBrowLY', { value: -0.4, lastSetAt: modelNowMs });
nativeModel.update();
near(nativeValues.get('ParamBrowLY'), -0.4, 'manual override has final parameter ownership');
nativeModel._nativeParamOverrides.clear();
nativeModel.setSpeaking(false);
nativeModel.setLipSyncValue(0);
nativeModel.update();
nativeModel.stopNativeMotion();
nativeDeltaSec = 0.1;
nativeModel.update();
assert.ok(nativeValues.get('ParamAngleZ') > 0 && nativeValues.get('ParamAngleZ') < 9,
  'cancellation smoothly releases toward the underlying breathing pose');
for (let frame = 0; frame < 5; frame++) nativeModel.update();
near(nativeValues.get('ParamMouthOpenY'), 0, 'cancelled mouth curve leaves no residue');
assert.equal(nativeModel.getNativePlaybackState().status, 'cancelled');
assert.equal(nativeModel._nativeReleaseValues.size, 0, 'the release layer is removed after blending');
nativeModel.cancelExpressionAction();
nativeModel._dragManager = { update() {}, getX: () => 0, getY: () => 0 };
nativeDeltaSec = 1 / 60;

let expressionValue = 0.4;
const expressionEntries = [];
nativeModel._expressionManager = {
  startMotionPriority() { const entry = { setIsFinished() {}, release() {} }; expressionEntries.push(entry); return entry; },
  getCubismMotionQueueEntries: () => expressionEntries,
  getCubismMotionQueueEntry: handle => handle,
  updateMotion() { nativeValues.set('ParamBrowLY', (nativeValues.get('ParamBrowLY') ?? 0) + expressionValue); },
};
assert.equal(nativeModel.setExpression('happy'), true);
for (let frame = 0; frame < 60; frame++) nativeModel.update();
near(nativeValues.get('ParamBrowLY'), 0.4, 'native Add expression is applied to the current base without accumulation');
nativeModel._expressions.set('sleepy', { expression: true });
nativeModel._nativeExpressionCatalog.push({ id: 'sleepy', parameters: ['ParamBrowLY'], status: 'loaded' });
expressionValue = -0.5;
assert.equal(nativeModel.setExpression('sleepy'), true);
nativeModel.update();
assert.ok(nativeValues.get('ParamBrowLY') > 0.35, 'expression switching starts from the last rendered pose');
for (let frame = 0; frame < 24; frame++) nativeModel.update();
near(nativeValues.get('ParamBrowLY'), -0.5, 'expression crossfade reaches the new target within 300 ms');
assert.equal(expressionEntries.length, 1, 'replaced expression entries cannot keep modifying the new preview');
nativeModel.clearNativeExpression();
for (let frame = 0; frame < 24; frame++) nativeModel.update();
near(nativeValues.get('ParamBrowLY'), 0, 'cleared native expression returns to the procedural baseline');
assert.equal(expressionEntries.length, 0);

// 眼球與頭部使用完整 update()/applyBasePose()/cancel 流程，只替換 Core 參數邊界。
function gazeHarness() {
  const gazeModel = new NativeModel();
  const values = new Map();
  let savedValues = new Map();
  let pointer = { x: 0, y: 0 };
  gazeModel._model = {
    setParameterValueById: (id, value) => values.set(id, value),
    getParameterValueById: id => values.get(id) ?? defaultValue(id),
    addParameterValueById: (id, value) => values.set(id, (values.get(id) ?? defaultValue(id)) + value),
    loadParameters() { values.clear(); for (const [id, value] of savedValues) values.set(id, value); },
    saveParameters() { savedValues = new Map(values); }, update() {},
  };
  gazeModel._motionManager = { isFinished: () => true };
  gazeModel._dragManager = { update() {}, getX: () => pointer.x, getY: () => pointer.y };
  const advance = (frames, delta = 1 / 60) => {
    nativeDeltaSec = delta;
    for (let index = 0; index < frames; index++) { modelNowMs += delta * 1000; gazeModel.update(); }
    nativeDeltaSec = 1 / 60;
  };
  return { model: gazeModel, values, advance,
    pointer(x, y) { pointer = { x, y }; },
    intent(x, y) { gazeModel.applyBasePose({ params: { ...createNeutralTargetParams(), eyeBallX: x, eyeBallY: y }, durationSec: 10 }); },
  };
}
{
  const h = gazeHarness();
  // 待機的輕微方向偏移不能使滑鼠向右時仍看向左側。
  h.intent(-0.2, 0);
  h.pointer(1, 0);
  h.advance(60);
  assert.ok(h.values.get('ParamEyeBallX') > 0.4, 'a right target remains visibly right despite a quiet left glance');
  h.pointer(-1, 0);
  h.advance(60);
  assert.ok(h.values.get('ParamEyeBallX') < -0.7, 'a left target yields a visible left gaze');
  h.intent(0, 0);
  h.pointer(0, 1);
  h.advance(60);
  assert.ok(h.values.get('ParamEyeBallY') > 0.5, 'an upper target produces clear upward gaze during an active plan');
  h.pointer(0, -1);
  h.advance(60);
  assert.ok(h.values.get('ParamEyeBallY') < -0.5, 'a lower target produces clear downward gaze');
  h.model.setEyeTrackingEnabled(false);
  h.intent(-0.7, 0.4);
  h.pointer(1, -1);
  h.advance(60);
  near(h.values.get('ParamEyeBallX'), -0.7, 'disabling pointer tracking preserves the authored horizontal target', 1e-6);
  near(h.values.get('ParamEyeBallY'), 0.4, 'disabling pointer tracking preserves the authored vertical target', 1e-6);
  h.model.setEyeTrackingEnabled(true);
  h.advance(60);
  near(h.values.get('ParamEyeBallX'), -0.7, 'strong authored attention owns direction despite an opposing pointer', 1e-6);
}
{
  const h = gazeHarness();
  h.pointer(1, -1);
  h.advance(120);
  const eyeX = h.values.get('ParamEyeBallX');
  const eyeY = h.values.get('ParamEyeBallY');
  const headX = h.values.get('ParamAngleX');
  h.advance(120);
  near(h.values.get('ParamEyeBallX'), eyeX, 'constant pointer input cannot accumulate horizontal gaze');
  near(h.values.get('ParamEyeBallY'), eyeY, 'constant pointer input cannot accumulate vertical gaze');
  near(h.values.get('ParamAngleX'), headX, 'constant pointer input cannot accumulate head rotation');
  h.pointer(0, 0);
  h.advance(1);
  near(h.values.get('ParamEyeBallX'), 0, 'returning the pointer to center clears the previous gaze');
  near(h.values.get('ParamEyeBallY'), 0, 'neutral input overwrites the previous vertical gaze');
}
{
  const h = gazeHarness();
  h.pointer(1, 1);
  h.model.applyEyeMotionPlan({ style: 'dizzy_dart', intensity: 1, amplitudeX: 1, amplitudeY: 1,
    durationMs: 6000, blendInMs: 50, blendOutMs: 100, frequencyHz: 0.7, phaseSeed: 0.3 });
  let reachedLimit = false;
  for (let frame = 0; frame < 120; frame++) {
    h.advance(1);
    for (const id of ['ParamEyeBallX', 'ParamEyeBallY']) {
      const value = h.values.get(id);
      assert.ok(Number.isFinite(value) && value >= -1 && value <= 1, 'pointer plus eye-motion offset remains within moc3 bounds');
      reachedLimit ||= Math.abs(value) === 1;
    }
  }
  assert.equal(reachedLimit, true, 'bounds are tested with a composition that actually reaches the limit');
  near(h.values.get('ParamAngleX'), 6, 'eye micro-motion does not introduce head jitter');
  near(h.values.get('ParamAngleY'), 4, 'head motion follows attention rather than the saccade offset');
}
for (const fps of [30, 60]) {
  const h = gazeHarness();
  h.model.setEyeTrackingEnabled(false);
  h.intent(0.7, -0.4);
  h.advance(fps / 10, 1 / fps);
  const eyeProgress = h.values.get('ParamEyeBallX') / 0.7;
  const headProgress = h.values.get('ParamAngleX') / (0.7 * 14);
  assert.ok(eyeProgress > 0.8 && headProgress > 0 && headProgress < 0.4,
    `eyes acquire attention before the head follows at ${fps} FPS`);
  assert.ok(eyeProgress > headProgress * 2, 'the eyes and head must not drift toward the target as one block');
  h.advance(fps, 1 / fps);
  const beforeCancel = h.values.get('ParamAngleX');
  h.model.cancelExpressionAction();
  h.advance(1, 1 / fps);
  assert.ok(h.values.get('ParamEyeBallX') > 0 && h.values.get('ParamEyeBallX') < 0.7, 'intent release moves the eyes toward center');
  assert.ok(h.values.get('ParamAngleX') > 0 && h.values.get('ParamAngleX') >= beforeCancel * 0.85,
    'head release remains smooth after the eyes start returning');
  h.advance(fps * 2, 1 / fps);
  near(h.values.get('ParamEyeBallX'), 0, 'released attention leaves no eye residue', 1e-6);
  near(h.values.get('ParamAngleX'), 0, 'released attention eventually centers the head', 0.002);
  near(h.values.get('ParamAngleY'), 0, 'released attention eventually centers head pitch', 0.002);
}

class FakeClock {
  now = 10000;
  nextId = 1;
  jobs = new Map();
  setTimeout = (callback, delay) => {
    const id = this.nextId++;
    this.jobs.set(id, { callback, at: this.now + delay });
    return id;
  };
  clearTimeout = id => this.jobs.delete(id);
  advance(duration) {
    const until = this.now + duration;
    let iterations = 0;
    while (this.jobs.size) {
      const [id, job] = [...this.jobs].sort((a, b) => a[1].at - b[1].at)[0];
      if (job.at > until) break;
      assert.ok(iterations++ < 1000, 'timer loop must settle');
      this.jobs.delete(id);
      this.now = job.at;
      job.callback();
    }
    this.now = until;
  }
}

function schedulerHarness(nativeModel = null) {
  const clock = new FakeClock();
  const reports = [];
  const applied = [];
  const listeners = [];
  let state = {
    isSpeaking: false,
    setAiTyping() {},
    setCompressing() {},
    appendChatMessage() {},
    setBlinkControl() {},
    setExpressionPlan(plan) {
      modelNowMs = clock.now;
      model._activeExpressionEvents = [];
      model.enqueueSequence(plan.sequence);
      applied.push({ plan, events: [...model._activeExpressionEvents] });
    },
  };
  const useAppStore = {
    getState: () => state,
    subscribe: listener => listeners.push(listener),
  };
  let id = 0;
  const schedulerLoad = createLoader({
    '../store/appStore': { useAppStore },
    '../live2d/LAppLive2DManager': {
      LAppLive2DManager: { getInstance: () => ({ getActiveModel: () => nativeModel ?? ({ cancelExpressionAction() {} }) }) },
    },
  }, {
    performance: { now: () => clock.now },
    crypto: { randomUUID: () => `test_action_${id++}` },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  const { actionScheduler: scheduler } = schedulerLoad('src/services/actionScheduler.ts');
  scheduler.setReporter((status, action) => reports.push({ status, turnId: action.turnId, at: clock.now }));
  return {
    scheduler, clock, applied, reports, useAppStore,
    speak(value) {
      const previous = state;
      state = { ...state, isSpeaking: value };
      listeners.forEach(listener => listener(state, previous));
    },
  };
}

const makePlan = (overrides = {}) => ({
  basePose: { preset: 'calm_soft', durationSec: 0.2, params: createNeutralTargetParams() },
  microEvents: [], sequence: [], ...overrides,
});
const sequence = [
  { ...mouthEvent, durationMs: 1000, fadeInMs: 100, fadeOutMs: 300 },
  { ...mouthEvent, durationMs: 700, fadeInMs: 200, fadeOutMs: 100 },
  { ...mouthEvent, durationMs: 500, fadeInMs: 250, fadeOutMs: 100 },
];
for (const steps of [sequence, [
  { ...mouthEvent, durationMs: 1000, fadeInMs: 100, fadeOutMs: 800 },
  { ...mouthEvent, durationMs: 100, fadeInMs: 800, fadeOutMs: 50 },
]]) {
  const h = schedulerHarness();
  h.scheduler.submit(makePlan({ sequence: steps }), 'chat', 'overlap');
  const renderedEnd = Math.max(...h.applied[0].events.map(event => event.startedAtMs + event.durationMs));
  h.clock.advance(renderedEnd - h.clock.now - 1);
  assert.equal(h.reports.filter(event => event.status === 'finished').length, 0,
    'the scheduler must wait until the rendered sequence completes');
  h.clock.advance(1);
  assert.equal(h.reports.at(-1).status, 'finished', 'overlapping fades must not create excess waiting');
}
{
  const h = schedulerHarness();
  h.scheduler.submit(makePlan({
    microEvents: [{ ...mouthEvent, durationMs: 12000 }],
    idlePlan: { enterAfterMs: 12500 },
  }), 'chat', 'long');
  h.clock.advance(10000);
  assert.equal(h.reports.at(-1).status, 'started', 'long responses must survive the former 10s limit');
  h.clock.advance(2500);
  assert.equal(h.reports.at(-1).status, 'finished', 'the idle entry boundary completes the action');
}
{
  const h = schedulerHarness();
  h.speak(true);
  h.scheduler.submit(makePlan(), 'chat', 'speech');
  h.clock.advance(1000);
  assert.equal(h.reports.at(-1).status, 'started', 'playing speech keeps the chat action alive');
  h.speak(false);
  assert.equal(h.reports.at(-1).status, 'finished', 'speech ending releases the finished action');
}
{
  const h = schedulerHarness();
  h.speak(true);
  h.scheduler.submit(makePlan(), 'chat', 'cancelled-speech');
  h.clock.advance(1000);
  h.scheduler.cancel();
  h.speak(false);
  assert.deepEqual(h.reports.map(event => event.status), ['started', 'cancelled'],
    'a later audio end must not finish an already cancelled action');
}
{
  const h = schedulerHarness();
  const shock = makePlan({ basePose: { ...makePlan().basePose, preset: 'shock_recoil', durationSec: 0.8 } });
  h.scheduler.submit(shock, 'chat', 'shock');
  h.scheduler.submit(makePlan(), 'chat', 'queued-old');
  h.clock.advance(600);
  h.scheduler.submit(makePlan(), 'chat', 'latest');
  h.clock.advance(5000);
  assert.deepEqual(h.reports.filter(event => event.status === 'started').map(event => event.turnId),
    ['shock', 'latest'], 'the latest input must discard an older pending expression');
}
{
  const h = schedulerHarness();
  h.scheduler.manualControl(3000);
  h.scheduler.submit(makePlan(), 'chat', 'queued');
  h.scheduler.cancel();
  h.clock.advance(10000);
  assert.equal(h.applied.length, 0, 'cancel removes pending expressions and manual-control timers');
  const shock = makePlan({ basePose: { ...makePlan().basePose, preset: 'shock_recoil' } });
  h.scheduler.submit(shock, 'chat', 'initial-shock');
  h.clock.advance(600);
  h.scheduler.submit(shock, 'chat', 'cooldown-shock');
  h.scheduler.cancel();
  h.clock.advance(10000);
  assert.equal(h.applied.length, 1, 'cancel must also prevent cooldown actions from returning');
}
{
  const h = schedulerHarness();
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 1;
    constructor() { sockets.push(this); }
  }
  const wsLoad = createLoader({
    '../store/appStore': { useAppStore: h.useAppStore },
    '../audio/TTSPlayer': { TTSPlayer: { getInstance: () => ({ stop: () => h.speak(false) }) } },
    './actionScheduler': { actionScheduler: h.scheduler },
  }, {
    WebSocket: FakeWebSocket,
    setTimeout: h.clock.setTimeout,
    console: { log() {}, warn() {}, error() {} },
  });
  const { wsService } = wsLoad('src/services/wsService.ts');
  wsService.connect();
  h.speak(true);
  h.scheduler.submit(makePlan({
    basePose: { ...makePlan().basePose, preset: 'shock_recoil', durationSec: 0.8 },
  }), 'chat', 'active-disconnect');
  h.scheduler.submit(makePlan(), 'chat', 'pending-disconnect');
  h.clock.advance(1000);
  sockets[0].readyState = 3;
  sockets[0].onclose();
  assert.deepEqual(h.reports.map(event => [event.status, event.turnId]), [
    ['started', 'active-disconnect'], ['cancelled', 'active-disconnect'],
  ], 'the actual WebSocket close callback must cancel before stopping speech can start pending work');
  assert.equal(h.applied.length, 1, 'disconnect must never apply the queued expression');
}

// 原生保護由 renderer 的真實完成狀態解除，長動作與 loop 不受 3 秒截斷。
function nativeSchedulerHarness(loop = false) {
  let nativeState = null;
  let suspended = false;
  let cancelled = 0;
  let blink = { paused: true, intervalMin: 2.5, intervalMax: 4.5 };
  const nativeModel = {
    getNativeMotionCatalog: () => [{ group: 'Shake', index: 2, name: 'shake', status: 'loaded', durationSec: 3.6, loop }],
    getNativeExpressionCatalog: () => [{ id: 'sleepy', status: 'loaded' }],
    getNativePlaybackState: () => nativeState,
    isAutoMotionSuspended: () => suspended,
    setAutoMotionSuspended: value => { suspended = value; },
    getBlinkState: () => ({ ...blink }),
    setBlinkInterval(min, max) { blink = { ...blink, intervalMin: min, intervalMax: max }; },
    pauseAutoBlink() { blink.paused = true; }, resumeAutoBlink() { blink.paused = false; },
    startMotion() { nativeState = { status: 'playing', elapsedSec: 0 }; },
    setExpression: () => true,
    cancelExpressionAction() { cancelled++; blink.paused = false; nativeState = { status: 'cancelled', elapsedSec: 1 }; },
  };
  const harness = schedulerHarness(nativeModel);
  return { ...harness, suspended: () => suspended, cancelled: () => cancelled, blink: () => blink,
    finish: () => { nativeState = { status: 'finished', elapsedSec: 3.6 }; } };
}
{
  const h = nativeSchedulerHarness();
  const notifications = [];
  const unsubscribe = h.scheduler.subscribePlaybackState(state => notifications.push(state.status));
  assert.equal(h.scheduler.playNativeMotion('Shake', 2), true);
  assert.equal(h.suspended(), true, 'preview isolates both automatic Idle entrances');
  h.scheduler.submit(makePlan(), 'chat', 'native-pending');
  h.clock.advance(3300);
  assert.equal(h.applied.length, 0, 'a motion longer than 3 seconds keeps chat queued');
  assert.equal(h.scheduler.getPlaybackState().kind, 'native-motion', 'queued chat cannot relabel the playing native preview');
  h.finish();
  h.clock.advance(40);
  assert.equal(h.applied.length, 1, 'renderer completion releases the pending plan');
  h.clock.advance(300);
  assert.equal(h.suspended(), false, 'automatic motion resumes after the release blend');
  assert.ok(notifications.includes('finished'), 'native completion is observable separately from compilation');
  unsubscribe();
}
{
  const h = nativeSchedulerHarness(true);
  assert.equal(h.scheduler.playNativeMotion('Shake', 2), true);
  h.clock.advance(15000);
  assert.equal(h.scheduler.getPlaybackState().status, 'playing', 'loop keeps protection until explicit stop');
  h.scheduler.stopPreview();
  assert.equal(h.scheduler.getPlaybackState().status, 'cancelled');
  h.clock.advance(300);
  assert.equal(h.suspended(), false);
  assert.equal(h.blink().paused, true, 'native preview restores a pause captured before cancellation');
  near(h.blink().intervalMin, 2.5, 'native preview restores the previous blink interval');
  assert.equal(h.clock.jobs.size, 0, 'cancel must remove native polling and release timers');
  assert.equal(h.scheduler.playNativeMotion('Missing', 0), false);
  assert.equal(h.scheduler.getPlaybackState().status, 'failed', 'missing native item produces a visible failure');
  assert.equal(h.scheduler.playNativeExpression('sleepy'), true);
  assert.equal(h.scheduler.getPlaybackState().durationMs, undefined, 'held expression must not claim a clip duration');
  h.scheduler.clearNativeExpression();
  h.clock.advance(300);
  assert.equal(h.suspended(), false);
}
{
  const h = nativeSchedulerHarness();
  const revision = h.scheduler.getPreviewRevision();
  h.scheduler.submit(makePlan({ idlePlan: { enterAfterMs: 500, settlePose: { durationSec: 12 } } }), 'debug');
  assert.ok(h.scheduler.getPreviewRevision() > revision, 'debug submission invalidates older asynchronous compilation');
  h.clock.advance(15000);
  const playback = h.scheduler.getPlaybackState();
  assert.equal(playback.status, 'idle', 'debug loop does not report finished while it keeps playing');
  assert.equal(playback.loop, true);
  assert.equal(playback.durationMs, 500, 'settlePose duration is not an invented loop completion boundary');
  assert.equal(h.suspended(), true, 'automatic motion stays isolated throughout debug idle');
  const idleRevision = h.scheduler.getPreviewRevision();
  h.scheduler.stopPreview();
  assert.ok(h.scheduler.getPreviewRevision() > idleRevision, 'stop invalidates pending asynchronous compilation');
  h.clock.advance(300);
  assert.equal(h.suspended(), false);
  assert.equal(h.blink().paused, true);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flushMicrotasks = async () => { for (let index = 0; index < 6; index++) await Promise.resolve(); };

function audioHarness(suspended = false) {
  const clock = new FakeClock();
  const decodes = [];
  const sources = [];
  const frames = new Map();
  const resume = deferred();
  let speaking = false;
  let mouth = 0;
  let nextFrame = 1;
  class FakeAudioContext {
    state = suspended ? 'suspended' : 'running';
    destination = {};
    async resume() { await resume.promise; this.state = 'running'; }
    decodeAudioData() { const pending = deferred(); decodes.push(pending); return pending.promise; }
    createBufferSource() {
      const source = { connect() {}, disconnect() {}, start() { this.started = true; }, stop() {}, onended: null };
      sources.push(source);
      return source;
    }
    createAnalyser() {
      return { frequencyBinCount: 128, connect() {}, disconnect() {}, getByteFrequencyData: data => data.fill(32) };
    }
    close() {}
  }
  const audioLoad = createLoader({
    '../live2d/LAppLive2DManager': {
      LAppLive2DManager: { getInstance: () => ({ getActiveModel: () => ({ setLipSyncValue: value => { mouth = value; }, setSpeaking() {} }) }) },
    },
    '../live2d/LAppPal': { LAppPal: { printLog() {}, printError() {} } },
    '../store/appStore': { useAppStore: { getState: () => ({ setSpeaking: value => { speaking = value; } }) } },
  }, {
    AudioContext: FakeAudioContext,
    atob,
    performance: { now: () => clock.now },
    requestAnimationFrame: callback => { const id = nextFrame++; frames.set(id, callback); return id; },
    cancelAnimationFrame: id => frames.delete(id),
  });
  const { TTSPlayer } = audioLoad('src/audio/TTSPlayer.ts');
  return {
    player: TTSPlayer.getInstance(), decodes, sources, frames, resume,
    speaking: () => speaking,
    mouth: () => mouth,
    tick(durationMs) {
      clock.advance(durationMs);
      for (const [id, callback] of [...frames]) {
        frames.delete(id);
        callback(clock.now);
      }
    },
  };
}

{
  const h = audioHarness(true);
  const play = h.player.play('YQ==');
  h.player.stop();
  h.resume.resolve();
  await play;
  assert.equal(h.decodes.length, 0, 'cancelling audio unlock must prevent even starting the decode');
}
{
  const h = audioHarness();
  const play = h.player.play('YQ==');
  await flushMicrotasks();
  assert.equal(h.decodes.length, 1);
  h.player.stop();
  h.decodes[0].resolve({ duration: 2 });
  await play;
  assert.equal(h.sources.length, 0, 'a cancelled pending decode must never become audible');
  assert.equal(h.speaking(), false);
}
{
  const h = audioHarness();
  const old = h.player.play('YQ==');
  await flushMicrotasks();
  const current = h.player.play('Yg==');
  await flushMicrotasks();
  h.decodes[1].resolve({ duration: 2 });
  await flushMicrotasks();
  h.decodes[0].reject(new Error('old decode failed late'));
  await old;
  assert.equal(h.sources.length, 1, 'only the newest audio may start');
  assert.equal(h.speaking(), true, 'a stale decode failure cannot close the new speaking state');
  let settled = false;
  current.then(() => { settled = true; });
  h.player.stop();
  await flushMicrotasks();
  assert.equal(settled, true, 'stop must settle the in-flight playback promise');
  assert.equal(h.frames.size, 0, 'stop must cancel lip-sync frames');
}
{
  const h = audioHarness();
  const first = h.player.play('YQ==');
  await flushMicrotasks();
  h.decodes[0].resolve({ duration: 1 });
  await flushMicrotasks();
  const staleEnded = h.sources[0].onended;
  staleEnded();
  await first;
  assert.ok(h.frames.size > 0, 'natural completion should schedule a smooth mouth close');
  const second = h.player.play('Yg==');
  assert.equal(h.frames.size, 0, 'a new clip cancels the previous mouth-close animation');
  await flushMicrotasks();
  h.decodes[1].resolve({ duration: 2 });
  await flushMicrotasks();
  staleEnded();
  assert.equal(h.player.getIsPlaying(), true, 'old onended callbacks cannot stop a new clip');
  assert.equal(h.speaking(), true);
  h.player.stop();
  await second;
}
{
  const h = audioHarness();
  const play = h.player.play('YQ==');
  await flushMicrotasks();
  h.decodes[0].resolve({ duration: 1 });
  await flushMicrotasks();
  h.sources[0].onended();
  await play;
  assert.ok(h.mouth() > 0, 'the closing animation begins from the last speaking mouth value');
  h.tick(250);
  near(h.mouth(), 0, 'a delayed frame must still close the mouth within the time envelope');
  assert.equal(h.frames.size, 0, 'the completed mouth close must not leave an animation frame queued');
}

console.log('Rushia runtime checks passed: framing, native metadata/index/loop, curve ownership/release, preview lifecycle/idle/blink restoration and cancelled TTS.');
