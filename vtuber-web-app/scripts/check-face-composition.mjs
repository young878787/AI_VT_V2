import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// 載入真正的 TypeScript；只替換 SDK、時鐘與未啟動的 WebGL 邊界。
function createLoader(overrides = {}, globals = {}) {
  const cache = new Map();
  const load = relativePath => {
    const filename = path.resolve(root, relativePath);
    if (cache.has(filename)) return cache.get(filename).exports;
    const module = { exports: {} };
    cache.set(filename, module);
    const source = fs.readFileSync(filename, 'utf8').replaceAll('import.meta.env', '__testImportMeta.env');
    const code = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
      fileName: filename,
    }).outputText;
    vm.runInNewContext(code, {
      module, exports: module.exports, console, __testImportMeta: { env: {} }, ...globals,
      require: specifier => {
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

let nowMs = 0;
const ids = new Map();
const idFor = name => {
  if (!ids.has(name)) ids.set(name, { getString: () => name });
  return ids.get(name);
};
const sdkNames = ['cubismmodelsettingjson', 'math/cubismmatrix44', 'motion/acubismmotion',
  'motion/cubismmotion', 'motion/cubismmotionqueuemanager'];
const load = createLoader({
  ...Object.fromEntries(sdkNames.map(name => [`@framework/${name}`, {}])),
  '@framework/model/cubismusermodel': { CubismUserModel: class {} },
  '@framework/math/cubismmodelmatrix': { CubismModelMatrix: class {} },
  '@framework/live2dcubismframework': { CubismFramework: { getIdManager: () => ({ getId: idFor }) } },
  '@framework/cubismdefaultparameterid': { CubismDefaultParameterId: new Proxy({}, { get: (_, name) => name }) },
  './LAppTextureManager': { LAppTextureManager: class {} },
  './LAppPal': { LAppPal: { log() {}, printLog() {} } },
  './LAppDelegate': {},
}, { performance: { now: () => nowMs } });
const { LAppModel } = load('src/live2d/LAppModel.ts');
const { BlinkRuntime } = load('src/live2d/runtime/BlinkRuntime.ts');
const { FaceIdleRuntime } = load('src/live2d/runtime/FaceIdleRuntime.ts');
const { createNeutralTargetParams } = load('src/live2d/expression/expressionState.ts');
const neutralFace = () => ({ eyeL: 1, eyeR: 1, browL: 0, browR: 0, winking: false, kind: 'still' });

function harness(random = () => 0) {
  nowMs = 0;
  const model = new LAppModel();
  const values = new Map();
  model._model = {
    setParameterValueById: (id, value) => values.set(id.getString(), value),
    getParameterValueById: id => values.get(id.getString()) ?? 0,
  };
  model._eyeBlinkIds = [idFor('ParamEyeLOpen'), idFor('ParamEyeROpen')];
  model._blinkRuntime = new BlinkRuntime(() => 0.5);
  model._faceIdleRuntime = new FaceIdleRuntime(random);
  model._expressionManager = null;
  const tick = (elapsedMs = 1000 / 60) => {
    nowMs += elapsedMs;
    model.updateEyeBlink(Math.min(0.1, elapsedMs / 1000), nowMs);
    model.applyCurrentExpressionParameters();
    model.applyNativeParamOverrides();
    return { ...model._lastFaceIdleSample };
  };
  return { model, values, tick };
}

function fixedFace(model, sample, blink = 1) {
  const seen = [];
  model._faceIdleRuntime = {
    update: (_dt, _now, context) => { seen.push({ ...context }); return { ...sample }; },
    reset() {}, cancelEyes() {},
  };
  model._blinkRuntime = { update: () => blink, isIdle: () => blink === 1, isPaused: () => false };
  return seen;
}

// 合成只影響輸出；基準、左右眼比例與 eyeSync 作者語意均保留。
{
  const { model, values } = harness();
  model._aiEyeBaseLOpen = model._currentEyeLOpen = 0.8;
  model._aiEyeBaseROpen = model._currentEyeROpen = 0.6;
  fixedFace(model, { ...neutralFace(), eyeL: 0.64, eyeR: 0.9 }, 0.5);
  model.updateEyeBlink(1 / 60, nowMs);
  near(values.get('ParamEyeLOpen'), 0.256, 'left expression × relaxation × blink');
  near(values.get('ParamEyeROpen'), 0.27, 'right expression × relaxation × blink');
  near(model._currentEyeLOpen, 0.8, 'left current base is unchanged');
  near(model._currentEyeROpen, 0.6, 'right current base is unchanged');
  near(model._aiEyeLOpen, 1, 'AI target is unchanged');
  near(model._aiEyeROpen, 1, 'right AI target is unchanged');

  model.smoothExpressionTargets({ ...createNeutralTargetParams(), eyeLOpen: 0.8, eyeROpen: 0.2 }, 0);
  near(model._currentEyeROpen, 0.8, 'legacy eyeSync runs before face composition');
  fixedFace(model, { ...neutralFace(), eyeL: 0, winking: true, kind: 'winkLeft' });
  model.updateEyeBlink(1 / 60, nowMs);
  near(values.get('ParamEyeLOpen'), 0, 'autonomous wink survives eyeSync');
  near(values.get('ParamEyeROpen'), 0.8, 'autonomous wink leaves the other eye at its synced base');
  near(model._currentEyeLOpen, 0.8, 'wink does not mutate the smoothed expression');
}

// 中性時眉毛也要寫回，反覆事件不可殘留或累加。
{
  const { model, values } = harness();
  model._lastFaceIdleSample = { ...neutralFace(), browL: 0.04, browR: -0.03 };
  for (let index = 0; index < 100; index += 1) model.applyCurrentExpressionParameters();
  near(values.get('ParamBrowLY'), 0.04, 'neutral left brow detail is visible without accumulation');
  near(values.get('ParamBrowRY'), -0.03, 'neutral right brow detail is visible without accumulation');
  near(model._currentBrowLY, 0, 'brow detail does not alter the smoothed baseline');
  near(model._aiBrowLY, 0, 'brow detail does not alter the AI target');
  model._lastFaceIdleSample = neutralFace();
  model.applyCurrentExpressionParameters();
  near(values.get('ParamBrowLY'), 0, 'finished neutral detail restores left baseline');
  near(values.get('ParamBrowRY'), 0, 'finished neutral detail restores right baseline');
  model._currentBrowLY = 0.99;
  model._lastFaceIdleSample.browL = 0.06;
  model.applyCurrentExpressionParameters();
  near(values.get('ParamBrowLY'), 1, 'brow output remains bounded');
  near(model._currentBrowLY, 0.99, 'clamping output cannot change its baseline');
}

// Face ownership 依當下活動的 patch 與通道分組，未來／rest 不提早接管。
{
  const { model } = harness();
  const owns = (patch, expected, label, startedAtMs = 100) => {
    model._activeExpressionEvents = [{ patch, startedAtMs, durationMs: 200 }];
    assert.deepEqual({ ...model.faceOwnership(200) }, expected, label);
  };
  const free = { eyes: false, brows: false };
  for (const patch of [{}, { bodyAngleY: 0.4 }, { eyeBallX: 0.5 }, { mouthForm: 0.2 }, { breathLevel: 0.7 }]) {
    owns(patch, free, 'unrelated or empty events do not own face channels');
  }
  for (const key of ['eyeLOpen', 'eyeROpen', 'eyeLSmile', 'eyeRSmile']) {
    owns({ [key]: 0 }, { eyes: true, brows: false }, `${key} owns only eyelids`);
  }
  for (const key of ['browLY', 'browRY', 'browLAngle', 'browRAngle', 'browLForm', 'browRForm', 'browLX', 'browRX']) {
    owns({ [key]: 0 }, { eyes: false, brows: true }, `${key} owns only eyebrows`);
  }
  owns({ eyeSync: false }, { eyes: true, brows: true }, 'an explicit eyeSync patch owns both groups');
  owns({ eyeLOpen: 0 }, free, 'queued eye event does not claim early', 201);
  owns({ browLY: 0.3 }, free, 'expired brow event does not claim', 0);

  model._activeExpressionEvents = [];
  model.applyBasePose({ durationSec: 1, params: { ...createNeutralTargetParams(), bodyAngleY: 0.3, eyeSync: false } });
  assert.deepEqual({ ...model.faceOwnership(200) }, free, 'body-only base and Rushia eyeSync=false do not own face');
  model._aiEyeLOpen = 0.7;
  assert.deepEqual({ ...model.faceOwnership(200) }, { eyes: true, brows: false }, 'active half-eye base owns eyes');
  model._aiEyeLOpen = 1;
  model._aiBrowRAngle = -0.4;
  assert.deepEqual({ ...model.faceOwnership(200) }, { eyes: false, brows: true }, 'active brow base owns eyebrows');
  model._aiBehaviorTimer = 0;
  assert.equal(model._expressionActionActive, true, 'scheduler action can outlive the base timer');
  assert.deepEqual({ ...model.faceOwnership(200) }, free, 'expired base timer releases face while scheduler still runs');
}

// native/manual 的通道及五秒期限、SDK 管理器接管都不能被自主覆寫。
{
  const { model, values } = harness();
  model._nativeMotionParameters.add('ParamEyeLOpen');
  assert.deepEqual({ ...model.faceOwnership(6000) }, { eyes: true, brows: false });
  values.set('ParamEyeLOpen', 0.22);
  values.set('ParamEyeROpen', 0.61);
  model.updateEyeBlink(1 / 60, 6000, true);
  near(values.get('ParamEyeLOpen'), 0.22, 'native eye value is preserved');
  near(values.get('ParamEyeROpen'), 0.61, 'native eye group retains both values');
  model._nativeMotionParameters.clear();
  model._nativeParamOverrides.set('ParamBrowRY', { value: -0.7, lastSetAt: 1000 });
  assert.deepEqual({ ...model.faceOwnership(6000) }, { eyes: false, brows: true }, 'manual brow ownership lasts exactly five seconds');
  assert.deepEqual({ ...model.faceOwnership(6001) }, { eyes: false, brows: false }, 'expired manual brow releases only its group');
  model._nativeParamOverrides.clear();
  model._manualUntilMs = 7000;
  assert.deepEqual({ ...model.faceOwnership(6000) }, { eyes: true, brows: true }, 'global manual lock claims face');
  model._manualUntilMs = 0;
  model._expressionManager = { isFinished: () => false };
  assert.deepEqual({ ...model.faceOwnership(6000) }, { eyes: true, brows: true }, 'running SDK expression manager owns face');
  model._currentBrowLY = 0.3;
  model._currentBrowLAngle = 0.4;
  model._lastFaceIdleSample = { ...neutralFace(), browL: 0.05 };
  values.set('ParamBrowLY', -0.65);
  values.set('ParamBrowLAngle', -0.85);
  model.applyCurrentExpressionParameters();
  near(values.get('ParamBrowLY'), -0.65, 'SDK brow Y survives the normal expression writer');
  near(values.get('ParamBrowLAngle'), -0.85, 'SDK brow angle survives the normal expression writer');
  model._expressionManager = { isFinished: () => true };
  model._lastFaceIdleSample = neutralFace();
  model.applyCurrentExpressionParameters();
  near(values.get('ParamBrowLY'), 0.3, 'finished SDK expression releases to current baseline');
}

// 真正的 FaceIdleRuntime 與 LAppModel：深放鬆、眉毛以及單眼都經過實際狀態機。
for (const [random, expected] of [[0.8, 'deep'], [0.94, 'brow']]) {
  const { model, values, tick } = harness(() => random);
  model.setBlinkInterval(100, 100);
  let observed = false;
  for (let frame = 0; frame < 900; frame += 1) {
    const sample = tick();
    if (sample.kind !== expected) continue;
    if (expected === 'deep' && sample.eyeL < 0.8) {
      assert.ok(values.get('ParamEyeLOpen') >= 0.64 && values.get('ParamEyeLOpen') < 0.8);
      near(model._currentEyeLOpen, 1, 'real relaxation leaves its current baseline unchanged');
      observed = true;
    }
    if (expected === 'brow' && Math.abs(sample.browR) > 0.02) {
      near(values.get('ParamBrowRY'), sample.browR, 'real brow event reaches neutral model output');
      near(model._currentBrowRY, 0, 'real brow event leaves its current baseline unchanged');
      observed = true;
    }
  }
  assert.ok(observed, `actual ${expected} event must be observed`);
}

{
  const { model, values, tick } = harness(() => 0.98);
  model.setBlinkInterval(100, 100);
  let observed = false;
  for (let frame = 0; frame < 6000; frame += 1) {
    const sample = tick();
    if (!sample.winking || Math.min(sample.eyeL, sample.eyeR) > 0.1) continue;
    observed = true;
    assert.ok(Math.max(values.get('ParamEyeLOpen'), values.get('ParamEyeROpen')) > 0.99,
      'the other eye stays open through a real autonomous wink');
    assert.equal(model.getBlinkState().paused, false, 'wink suppression cannot change the public pause state');
    model._lastFaceIdleSample.browL = 0.035;
    model.forceBlink();
    near(model._lastFaceIdleSample.eyeL, 1, 'force immediately clears autonomous left eye coefficient');
    near(model._lastFaceIdleSample.eyeR, 1, 'force immediately clears autonomous right eye coefficient');
    near(model._lastFaceIdleSample.browL, 0.035, 'force does not clear the independent brow sample');
    assert.equal(model._lastFaceIdleSample.winking, false, 'force cancels autonomous wink ownership');
    let closed = false;
    for (let step = 0; step < 30; step += 1) {
      tick();
      if (values.get('ParamEyeLOpen') < 0.05 && values.get('ParamEyeROpen') < 0.05) closed = true;
    }
    assert.ok(closed, 'explicit force performs a genuine bilateral blink after canceling wink');
    break;
  }
  assert.ok(observed, 'actual autonomous wink must be observed after its initial cooldown');
  model.pauseAutoBlink();
  for (let frame = 0; frame < 4200; frame += 1) {
    const sample = tick();
    near(sample.eyeL, 1, 'indefinite pause suppresses autonomous left eyelid detail');
    near(sample.eyeR, 1, 'indefinite pause suppresses autonomous right eyelid detail');
    assert.equal(sample.winking, false, 'indefinite pause cannot schedule autonomous winks');
  }
  assert.equal(model.getBlinkState().paused, true, 'autonomous facial events cannot resume a public pause');
  model.forceBlink();
  let pausedForceClosed = false;
  for (let frame = 0; frame < 30; frame += 1) {
    tick();
    pausedForceClosed ||= values.get('ParamEyeLOpen') < 0.05;
  }
  assert.ok(pausedForceClosed, 'explicit force can blink during an indefinite pause');
  assert.equal(model.getBlinkState().paused, true, 'forced blink preserves the indefinite pause afterward');
  nowMs += 10000;
  model.pauseAutoBlink(3);
  tick(2990);
  assert.equal(model.getBlinkState().paused, true, 'fresh pause after frame gap retains its full deadline');
  tick(10);
  assert.equal(model.getBlinkState().paused, false, 'fresh timed pause expires from the command time');
  nowMs += 10000;
  model.forceBlink();
  let forcedClosed = false;
  for (let frame = 0; frame < 30; frame += 1) {
    tick();
    forcedClosed ||= values.get('ParamEyeLOpen') < 0.05;
  }
  assert.ok(forcedClosed, 'fresh force after frame gap is not discarded as an expired event');
}

// 同一個真實事件內的 wink 與眉毛也可分別交接，不能只清顯示 sample。
{
  const randoms = [0, ...Array.from({ length: 9 }, () => [0, 0]).flat(),
    0.99, 0, 0.5, 0.5, 0.5, 0, 0.5];
  const { model, values, tick } = harness(() => randoms.shift() ?? 0.5);
  model.setBlinkInterval(100, 100);
  let observed = false;
  for (let frame = 0; frame < 3300; frame += 1) {
    const sample = tick();
    if (!sample.winking || sample.eyeL > 0.2 || sample.browL < 0.015) continue;
    observed = true;
    model.forceBlink();
    const after = tick();
    assert.equal(after.winking, false, 'force cancels the actual runtime wink event');
    assert.ok(after.browL > 0.015, 'independent brow envelope survives force in the actual event');
    near(values.get('ParamBrowLY'), after.browL, 'surviving brow envelope still reaches the model');
    break;
  }
  assert.ok(observed, 'the scripted random source must produce a genuine wink with a brow accent');
}

// 生命週期只繼承常駐基準；過期 plan 不能變更 listening，短事件不可搬家。
{
  const { model } = harness();
  const plan = { type: 'ambient_plan', sequence: 1, state: 'listening', moveMs: 1000,
    target: { headX: 0, headY: 0, headZ: 0, bodyX: 0, bodyY: 0, bodyZ: 0, gazeX: 0, gazeY: 0 },
    breath: { periodSec: 4.8, amplitude: 0.25, rootAmplitude: 0.006 } };
  model.applyAmbientPlan(plan);
  assert.equal(model._faceListening, true, 'accepted plan sets face listening');
  model.applyAmbientPlan({ ...plan, state: 'idle' });
  assert.equal(model._faceListening, true, 'stale plan cannot modify face listening');
  model._lastFaceIdleSample = { ...neutralFace(), eyeL: 0, browR: 0.04, winking: true, kind: 'winkLeft' };
  model._ambientBase.eyeLOpen = 0.82;
  const { model: replacement } = harness();
  replacement.inheritAmbientBaseline(model);
  near(replacement._ambientBase.eyeLOpen, 0.82, 'replacement inherits settled facial baseline');
  assert.deepEqual({ ...replacement._lastFaceIdleSample }, neutralFace(), 'replacement does not inherit short facial events');
  model.disconnectAmbient();
  assert.equal(model._faceListening, false, 'disconnect clears listening without an incoming idle plan');
  assert.deepEqual({ ...model._lastFaceIdleSample }, neutralFace(), 'disconnect clears the displayed facial event');
  model._lastFaceIdleSample.eyeR = 0.2;
  model.pauseAutoBlink();
  model.resetAmbientEmotion();
  assert.deepEqual({ ...model._lastFaceIdleSample }, neutralFace(), 'emotion reset clears short facial details');
  near(model._ambientBase.eyeLOpen, 1, 'emotion reset restores its neutral settled baseline');
  assert.equal(model.getBlinkState().paused, true, 'emotion reset cannot silently resume public blink pause');
}

console.log('Face composition checks passed: expression/blink layering, per-group ownership, controls, SDK/manual priority and lifecycle.');
