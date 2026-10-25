import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
import './check-blink-runtime.mjs';
import './check-face-idle-runtime.mjs';
import './check-face-composition.mjs';

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
assert.deepEqual(Array.from(defines.AvailableModels, config => config.displayName), ['Rushia', 'Rushia_hd']);
for (const config of defines.AvailableModels) {
  assert.equal(config.name, 'Rushia', 'both resources must use the same Chat and expression compiler identity');
  assert.equal(defines.getModelJsonPath(config), `/Resources/${config.directory}/RushiaHD.model3.json`);
}

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
const { BlinkRuntime } = load('src/live2d/runtime/BlinkRuntime.ts');
const { FaceIdleRuntime } = load('src/live2d/runtime/FaceIdleRuntime.ts');
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
model._blinkRuntime = new BlinkRuntime(() => 0.5);
model._faceIdleRuntime = new FaceIdleRuntime(() => 0);
model._lastFaceIdleSample = { eyeL: 1, eyeR: 1, browL: 0, browR: 0, winking: false, kind: 'still' };
model._faceListening = false;
model._manualUntilMs = 0;
model._activeExpressionEvents = [];
model._expressionActionActive = false;
model._eyeBlinkIds = ['left', 'right'];
model._nativeParamOverrides = new Map();
model._nativeMotionParameters = new Set();
model._autoEffectsEnabled = true;
model.pauseAutoBlink();
for (let frame = 0; frame < 30; frame += 1) {
  modelNowMs += 1000 / 60;
  model.updateEyeBlink(1 / 60, modelNowMs);
  near(parameters.get('left'), 0.5, 'paused blink preserves half-closed left eye');
  near(parameters.get('right'), 0.3, 'paused blink preserves asymmetric right eye');
}
model.resumeAutoBlink();
model.forceBlink();
let minimumLeft = 1;
for (let frame = 0; frame < 45; frame += 1) {
  modelNowMs += 1000 / 60;
  model.updateEyeBlink(1 / 60, modelNowMs);
  minimumLeft = Math.min(minimumLeft, parameters.get('left'));
  near(parameters.get('left') / 0.5, parameters.get('right') / 0.3,
    'blink keeps the expression eye ratio');
}
assert.ok(minimumLeft < 0.05, 'resumed blink must visibly close the eyes');
near(parameters.get('left'), 0.5, 'completed blink restores the left expression');
near(parameters.get('right'), 0.3, 'completed blink restores the right expression');
model.setBlinkInterval(2, 6);
assert.deepEqual({ ...model.getBlinkState() }, { paused: false, intervalMin: 2, intervalMax: 6 });
model._autoEffectsEnabled = false;
modelNowMs += 1000 / 60;
model.updateEyeBlink(1 / 60, modelNowMs);
near(parameters.get('left'), 0.5, 'disabled autonomous effects release the eye to its expression base');
model.forceBlink();
let disabledForcedMinimum = 1;
for (let frame = 0; frame < 45; frame += 1) {
  modelNowMs += 1000 / 60;
  model.updateEyeBlink(1 / 60, modelNowMs);
  disabledForcedMinimum = Math.min(disabledForcedMinimum, parameters.get('left'));
}
assert.ok(disabledForcedMinimum < 0.05, 'explicit force blink still works with automatic effects disabled');
near(parameters.get('left'), 0.5, 'forced blink restores the expression while automatic effects remain disabled');
model._autoEffectsEnabled = true;
model._nativeParamOverrides.set('ParamEyeLOpen', { value: 0.2, lastSetAt: modelNowMs });
model.forceBlink();
modelNowMs += 1000 / 60;
model.updateEyeBlink(1 / 60, modelNowMs);
near(parameters.get('right'), 0.3, 'manual eye ownership suppresses the autonomous blink event');
model._nativeParamOverrides.clear();
modelNowMs += 1000 / 60;
model.updateEyeBlink(1 / 60, modelNowMs);
near(parameters.get('right'), 0.3, 'manual eye release cannot replay a suppressed blink');
modelNowMs += 10000;
model.pauseAutoBlink(3);
model.updateEyeBlink(1 / 60, modelNowMs);
assert.equal(model.getBlinkState().paused, true, 'a new pause after background inactivity starts from the command time');
modelNowMs += 2990;
model.updateEyeBlink(1 / 60, modelNowMs);
assert.equal(model.getBlinkState().paused, true, 'the new pause retains its full duration across frame gaps');
modelNowMs += 10;
model.updateEyeBlink(1 / 60, modelNowMs);
assert.equal(model.getBlinkState().paused, false, 'timed pause completes at its actual deadline');
modelNowMs += 10000;
model.forceBlink();
let resumedForceMinimum = 1;
for (let frame = 0; frame < 30; frame += 1) {
  modelNowMs += 1000 / 60;
  model.updateEyeBlink(1 / 60, modelNowMs);
  resumedForceMinimum = Math.min(resumedForceMinimum, parameters.get('left'));
}
assert.ok(resumedForceMinimum < 0.05, 'a fresh force command after background inactivity must execute');

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

function schedulerHarness() {
  const clock = new FakeClock();
  const reports = [];
  const applied = [];
  const ambientUpdates = [];
  const listeners = [];
  let state = {
    isSpeaking: false,
    disconnectAmbient() {},
    setAmbientPlan(plan) { ambientUpdates.push(plan); },
    setAiTyping() {},
    setCompressing() {},
    resetChatContext() {},
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
      LAppLive2DManager: { getInstance: () => ({ getActiveModel: () => ({
        cancelExpressionAction() {}, finishExpressionAction() {}, setManualControl() {},
      }) }) },
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
    scheduler, clock, applied, reports, ambientUpdates, useAppStore,
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
    '../live2d/LAppLive2DManager': { LAppLive2DManager: { getInstance: () => ({ getActiveModel: () => null }) } },
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
  const ambientUpdate = { type: 'ambient_plan', sequence: 1, state: 'idle', moveMs: 900,
    target: { headX: 0, headY: 0, headZ: 0, bodyX: 0, bodyY: 0, bodyZ: 0, gazeX: 0, gazeY: 0 },
    breath: { periodSec: 4, amplitude: 0.5, rootAmplitude: 0.02 } };
  sockets[0].onmessage({ data: JSON.stringify({ ...ambientUpdate, turn_id: 'unrelated-old-turn' }) });
  assert.equal(h.ambientUpdates.length, 1, 'autonomous updates are independent of Chat turn filtering');
  sockets[0].onmessage({ data: JSON.stringify({ ...ambientUpdate, sequence: -1 }) });
  assert.equal(h.ambientUpdates.length, 1, 'WebSocket rejects malformed autonomous updates');
  assert.deepEqual(h.reports.map(event => event.status), ['started'],
    'autonomous updates cannot submit, finish or cancel the active Chat action');
  h.clock.advance(1000);
  sockets[0].readyState = 3;
  sockets[0].onclose();
  assert.deepEqual(h.reports.map(event => [event.status, event.turnId]), [
    ['started', 'active-disconnect'], ['cancelled', 'active-disconnect'],
  ], 'the actual WebSocket close callback must cancel before stopping speech can start pending work');
  assert.equal(h.applied.length, 1, 'disconnect must never apply the queued expression');
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

// 模型切換採用真實 manager；只替換 WebGL／模型檔案邊界。
{
  const instances = [];
  let textureGate = null;
  let textureError = null;
  class FakeModel {
    released = 0;
    width = 800;
    height = 600;
    scale = 1;
    position = { x: 0, y: 0 };
    constructor() { instances.push(this); }
    async loadAssets(home, fileName, config) { this.home = home; this.config = config; }
    setupRenderer(gl) { this.gl = gl; }
    async setupTextures() {
      if (textureGate) await textureGate.promise;
      if (textureError) throw textureError;
    }
    getModelConfig() { return this.config; }
    getCanvasWidth() { return this.width; }
    getCanvasHeight() { return this.height; }
    setCanvasSize(width, height) { this.width = width; this.height = height; }
    getModelScale() { return this.scale; }
    setModelScale(scale) { this.scale = scale; }
    getModelPosition() { return this.position; }
    setModelPosition(x, y) { this.position = { x, y }; }
    inheritAmbientBaseline(previous) {
      this.inheritedFrom = previous;
      this.previousReleasedAtInheritance = previous.released;
    }
    release() { this.released++; }
  }
  const managerLoad = createLoader({
    './LAppModel': { LAppModel: FakeModel },
    './LAppPal': { LAppPal: { printLog() {}, printError() {} } },
  });
  const { LAppLive2DManager } = managerLoad('src/live2d/LAppLive2DManager.ts');
  const manager = LAppLive2DManager.getInstance();
  await assert.rejects(manager.loadDefaultModel(), /WebGL/);
  manager.setGLContext({});
  await assert.rejects(manager.loadModel('../other'), /不支援/);
  const original = await manager.loadDefaultModel();
  original.setCanvasSize(1200, 900);
  original.setModelScale(1.15);
  original.setModelPosition(0.12, -0.08);
  original.connected = false;
  assert.equal(await manager.loadDefaultModel(), original, 'selecting the active resource must not allocate another model');

  textureGate = deferred();
  const switchToHd = manager.loadModel('Rushia_hd');
  await flushMicrotasks();
  assert.equal(manager.getActiveModel(), original, 'the original stays visible while the new textures load');
  assert.equal(original.released, 0);
  await assert.rejects(manager.loadModel('Rushia'), /正在載入/);
  textureGate.resolve();
  const hd = await switchToHd;
  textureGate = null;
  assert.equal(manager.getActiveModel(), hd);
  assert.equal(hd.home, '/Resources/Rushia_hd/');
  assert.equal(hd.config.name, 'Rushia');
  assert.equal(original.released, 1, 'the previous model is released after the new model is ready');
  assert.equal(hd.width, 1200);
  assert.equal(hd.height, 900);
  assert.equal(hd.scale, 1.15);
  assert.equal(hd.position.x, 0.12);
  assert.equal(hd.position.y, -0.08);
  assert.equal(hd.inheritedFrom, original, 'offline resource switching still inherits the existing ambient baseline');
  assert.equal(hd.previousReleasedAtInheritance, 0, 'the baseline is inherited before releasing the previous resource');

  textureError = new Error('missing texture');
  await assert.rejects(manager.loadModel('Rushia'), /missing texture/);
  assert.equal(manager.getActiveModel(), hd, 'failed switching keeps the working model');
  assert.equal(hd.released, 0);
  assert.equal(instances.at(-1).released, 1, 'a failed candidate releases its resources');
  textureError = null;
  const restored = await manager.loadModel('Rushia');
  assert.equal(hd.released, 1);
  assert.equal(manager.getActiveModel(), restored, 'switching back uses the same manager');

  textureGate = deferred();
  const cancelled = manager.loadModel('Rushia_hd');
  await flushMicrotasks();
  manager.release();
  textureGate.resolve();
  await assert.rejects(cancelled, /取消/);
  assert.equal(manager.getActiveModel(), null, 'a late completion cannot revive a released stage');
  assert.equal(instances.at(-1).released, 1);
}

// 切換服務使用真實 store 與 Scheduler，確認表情計畫與角色身份共用。
{
  const clock = new FakeClock();
  const applied = [];
  const settled = [];
  const models = [];
  let gate = null;
  let failure = null;
  const makeModel = () => ({
    setModelScale(value) { this.scale = value; },
    setEyeTrackingEnabled(value) { this.tracking = value; },
    setAutoEffectsEnabled(value) { this.autoEffects = value; },
    setSpeaking(value) { this.speaking = value; },
    cancelExpressionAction() {},
    finishExpressionAction() {},
    setManualControl() {},
    resumeAutoBlink() {},
    applyBasePose(pose) { applied.push(pose); },
    applyIdlePlan(plan) { settled.push(plan.settlePose); },
    applyAmbientPlan(plan) { this.ambient = plan; this.ambientApplications = (this.ambientApplications ?? 0) + 1; },
    enqueueSequence() {},
  });
  let active = makeModel();
  const manager = {
    getActiveModel: () => active,
    async loadModel() {
      if (gate) await gate.promise;
      if (failure) throw failure;
      active = makeModel();
      models.push(active);
      return active;
    },
  };
  const create = initializer => {
    let state;
    const listeners = [];
    const getState = () => state;
    const setState = patch => {
      const previous = state;
      state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
      listeners.forEach(listener => listener(state, previous));
    };
    state = initializer(setState, getState);
    return { getState, setState, subscribe: listener => listeners.push(listener) };
  };
  const serviceLoad = createLoader({
    zustand: { create },
    '../live2d/LAppLive2DManager': { LAppLive2DManager: { getInstance: () => manager } },
  }, {
    structuredClone,
    performance: { now: () => clock.now },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  const { useAppStore: store } = serviceLoad('src/store/appStore.ts');
  const { switchModel } = serviceLoad('src/services/modelService.ts');
  assert.equal(store.getState().eyeTrackingEnabled, false, 'autonomous gaze defaults to the calibrated forward target');
  const plan = makePlan({
    sequence: [mouthEvent],
    idlePlan: { enterAfterMs: 700, settlePose: { preset: 'happy_soft', durationSec: 0.3,
      params: { ...createNeutralTargetParams(), mouthForm: 0.4, blushLevel: 0.2 } } },
  });
  const ambient = { type: 'ambient_plan', sequence: 4, state: 'idle', moveMs: 900,
    target: { headX: 0.2, headY: 0, headZ: 0, bodyX: 0, bodyY: 0, bodyZ: 0, gazeX: 0, gazeY: 0 },
    breath: { periodSec: 4, amplitude: 0.5, rootAmplitude: 0.02 } };
  store.setState({ modelLoaded: true, expressionPlan: plan, ambientPlan: ambient,
    modelScale: 1.2, eyeTrackingEnabled: false, isSpeaking: true });
  gate = deferred();
  const switching = switchModel('Rushia_hd');
  assert.equal(store.getState().modelLoading, true);
  assert.equal(store.getState().modelLoaded, false);
  await switchModel('Rushia_hd');
  gate.resolve();
  await switching;
  gate = null;
  assert.equal(models.length, 1, 'duplicate clicks during loading cannot allocate another model');
  assert.equal(store.getState().currentModelDirectory, 'Rushia_hd');
  assert.equal(store.getState().currentModelName, 'Rushia');
  assert.equal(store.getState().modelLoaded, true);
  assert.equal(store.getState().modelLoading, false);
  assert.equal(active.scale, 1.2);
  assert.equal(active.tracking, false);
  assert.equal(active.speaking, true);
  // VM 邊界的原型不同；以同一 realm 的 structured clone 比較完整資料。
  assert.equal(applied.length, 0, 'switching never replays a completed or interrupted short action');
  assert.deepEqual(structuredClone(settled.at(-1)), structuredClone(plan.idlePlan.settlePose),
    'switching restores the current settled emotion');
  assert.deepEqual(structuredClone(active.ambient), structuredClone(ambient),
    'switching restores the latest Python autonomous target');
  store.getState().setAmbientPlan(ambient);
  store.getState().setAmbientPlan({ ...ambient, sequence: 3 });
  assert.equal(active.ambientApplications, 1, 'store discards duplicate and stale autonomous sequences');
  const latestAmbient = { ...ambient, sequence: 5 };
  store.getState().setAmbientPlan(latestAmbient);
  assert.equal(active.ambientApplications, 2, 'store forwards each new autonomous sequence once');
  assert.equal(applied.length, 0, 'autonomous store updates never reapply the current Chat expression plan');
  assert.deepEqual(structuredClone(store.getState().expressionPlan), structuredClone(plan),
    'switching must not change shared parameter values');
  await switchModel('Rushia_hd');
  await switchModel('../other');
  assert.equal(models.length, 1);

  failure = new Error('missing texture');
  await switchModel('Rushia');
  assert.equal(store.getState().currentModelDirectory, 'Rushia_hd');
  assert.equal(store.getState().modelLoaded, true);
  assert.match(store.getState().modelError, /仍使用 Rushia_hd/);
  assert.equal(applied.length, 0, 'failed switching cannot replay short actions on the original model');
  assert.deepEqual(structuredClone(active.ambient), structuredClone(latestAmbient),
    'failed switching keeps the working autonomous target');
  failure = null;
  await switchModel('Rushia');
  assert.equal(store.getState().currentModelDirectory, 'Rushia');
  assert.equal(store.getState().modelError, null);
  assert.equal(store.getState().currentModelName, 'Rushia');
}

// 圖片缺檔須讓 setupTextures 拒絕，避免切換永久停在載入中。
{
  const images = [];
  const textureLoad = createLoader({}, {
    Image: class { constructor() { images.push(this); } },
    console: { error() {} },
  });
  const { LAppTextureManager } = textureLoad('src/live2d/LAppTextureManager.ts');
  const textureModel = Object.create(LAppModel.prototype);
  textureModel._modelHomeDir = '/Resources/Rushia_hd/';
  textureModel._modelSetting = { getTextureCount: () => 1, getTextureFileName: () => 'missing.png' };
  textureModel._textureManager = new LAppTextureManager({});
  const result = textureModel.setupTextures();
  images[0].onerror();
  await assert.rejects(result, /無法載入圖片/);
}

// 真實 LAppModel + Matrix／Mixer；只替換 Core／WebGL 邊界。
{
  const { CubismMatrix44 } = load('src/live2d/framework/math/cubismmatrix44.ts');
  const { CubismModelMatrix } = load('src/live2d/framework/math/cubismmodelmatrix.ts');
  const defaults = load('src/live2d/framework/cubismdefaultparameterid.ts');
  const { ParameterMixer } = load('src/live2d/runtime/ParameterMixer.ts');
  let now = 10000;
  const integrationLoad = createLoader({
    ...Object.fromEntries(sdkNames.map(name => [`@framework/${name}`, {}])),
    '@framework/math/cubismmatrix44': { CubismMatrix44 },
    '@framework/math/cubismmodelmatrix': { CubismModelMatrix },
    '@framework/cubismdefaultparameterid': defaults,
    '@framework/live2dcubismframework': { CubismFramework: { getIdManager: () => ({ getId: id => ({ getString: () => id }) }) } },
    '@framework/model/cubismusermodel': { CubismUserModel: class {} },
    './LAppTextureManager': { LAppTextureManager: class {} },
    './LAppDelegate': { LAppDelegate: { getInstance: () => ({ getFrameBuffer: () => null }) } },
    './LAppPal': { LAppPal: { getDeltaTime: () => 1 / 60, log() {}, printLog() {}, printError() {} } },
  }, { performance: { now: () => now }, console: { log() {}, warn() {}, error() {} } });
  const { LAppModel: RuntimeModel } = integrationLoad('src/live2d/LAppModel.ts');
  const ids = [
    'ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX', 'ParamBodyAngleY', 'ParamBodyAngleZ',
    'ParamEyeBallX', 'ParamEyeBallY', 'ParamBreath', 'ParamEyeLOpen', 'ParamEyeROpen',
    'ParamCheek', 'ParamMouthOpenY', 'ParamMouthForm', 'ParamBrowLY', 'ParamBrowRY',
    'ParamBrowLAngle', 'ParamBrowRAngle', 'ParamBrowLForm', 'ParamBrowRForm', 'ParamBrowLX', 'ParamBrowRX',
  ];
  const controlled = ids.slice(0, 8);
  const values = new Map(ids.map(id => [id, id.endsWith('Open') ? 1 : 0]));
  const parameterName = id => typeof id === 'string' ? id : id.getString();
  let saved = new Map(values);
  let writes = [];
  let physicsSnapshot = null;
  let renderedMatrix = null;
  let meshTop = 0.1;
  const core = {
    getParameterCount: () => ids.length,
    getParameterId: index => ({ getString: () => ids[index] }),
    getParameterDefaultValue: index => ids[index].endsWith('Open') ? 1 : 0,
    getParameterMinimumValue: index => ids[index].includes('Angle') ? -30 : -1,
    getParameterMaximumValue: index => ids[index].includes('Angle') ? 30 : 1,
    getParameterValueByIndex: index => values.get(ids[index]) ?? 0,
    getParameterValueById: id => values.get(parameterName(id)) ?? 0,
    setParameterValueByIndex(index, value) { values.set(ids[index], value); writes.push({ kind: 'mixer', id: ids[index], value }); },
    setParameterValueById(id, value) { id = parameterName(id); values.set(id, value); writes.push({ kind: 'parameter', id, value }); },
    addParameterValueById(id, value) {
      id = parameterName(id);
      const next = (values.get(id) ?? 0) + value;
      values.set(id, next); writes.push({ kind: 'source', id, value: next });
    },
    loadParameters() { for (const [id, value] of saved) values.set(id, value); },
    saveParameters() { saved = new Map(values); },
    update() { writes.push({ kind: 'core' }); },
    getDrawableCount: () => 1,
    getDrawableVertexCount: () => 4,
    getDrawableOpacity: () => 1,
    getDrawableVertices: () => [-0.1, -0.1, 0.1, -0.1, 0.1, meshTop, -0.1, meshTop],
  };
  const runtime = new RuntimeModel();
  runtime._model = core;
  runtime._modelMatrix = new CubismModelMatrix(2, 2);
  runtime._parameterMixer = new ParameterMixer(core);
  runtime._dragManager = { update() {}, getX: () => 0.8, getY: () => 0.7 };
  runtime._motionManager = { isFinished: () => true };
  runtime._physics = { evaluate() {
    physicsSnapshot = new Map(values);
    writes.push({ kind: 'physics' });
  } };
  runtime.getRenderer = () => ({
    setRenderState() {}, setMvpMatrix(matrix) { renderedMatrix = matrix; }, drawModel() {},
  });
  runtime.setCanvasSize(1280, 800);
  assert.equal(runtime.getAmbientDiagnostics().pointer, false, 'LAppModel pointer tracking defaults off');
  assert.equal(typeof runtime.setThinking, 'undefined', 'generation no longer starts a second autonomous animation policy');
  const plan = { type: 'ambient_plan', sequence: 1, state: 'idle', moveMs: 900,
    target: { headX: 0.6, headY: -0.25, headZ: 0.3, bodyX: 0.4, bodyY: -0.2, bodyZ: 0.3, gazeX: -0.6, gazeY: 0.2 },
    breath: { periodSec: 4, amplitude: 0.6, rootAmplitude: 0.04 } };
  runtime.applyAmbientPlan(plan);
  const tick = (frames = 1) => {
    for (let frame = 0; frame < frames; frame++) {
      now += 1000 / 60;
      writes = [];
      runtime.update();
      assert.ok([...values.values()].every(Number.isFinite), 'complete LAppModel frames keep all Core values finite');
    }
  };
  tick(120);
  for (const id of controlled) {
    const final = writes.filter(write => write.id === id);
    assert.equal(final.filter(write => write.kind === 'mixer').length, 1, `${id} has one final mixer write per frame`);
    assert.equal(final.at(-1).kind, 'mixer', `${id} is finalized by ParameterMixer`);
    near(physicsSnapshot.get(id), values.get(id), `${id} enters Physics with its final mixed value`);
  }
  assert.ok(writes.findIndex(write => write.kind === 'physics') > writes.findLastIndex(write => write.kind === 'mixer'),
    'Physics evaluates after controlled channel composition');
  assert.ok(values.get('ParamAngleX') > 10 && values.get('ParamEyeBallX') < -0.4,
    'Python targets drive the actual Core head and gaze despite a displaced pointer');

  const matrixBefore = Array.from(runtime.getModelMatrix().getArray());
  for (let repeat = 0; repeat < 100; repeat++) runtime.updateModelMatrix();
  assert.deepEqual(Array.from(runtime.getModelMatrix().getArray()), matrixBefore, 'Root is always composed from framing, never accumulated');
  const projection = new CubismMatrix44();
  runtime.draw(projection);
  assert.deepEqual(Array.from(renderedMatrix.getArray()), matrixBefore, 'render uses the current Root model matrix');
  const matrix = runtime.getModelMatrix();
  const hitX = matrix.transformX(0);
  const hitY = matrix.transformY(0);
  assert.equal(runtime.hitTest(hitX, hitY, 'Body'), true, 'hit testing follows the rendered Root translation');
  assert.equal(runtime.hitTest(matrix.transformX(0.4), hitY, 'Body'), false, 'hit testing still rejects points outside the rendered mesh');
  const root = runtime.getAmbientDiagnostics().rootOffset;
  assert.ok(root > 0 && root < 0.13, 'actual model Root includes the bounded breathing contribution');
  runtime.setModelPosition(0.1, 0.05);
  near(runtime.getModelPosition().x, 0.1, 'Root never changes the user X baseline');
  near(runtime.getModelPosition().y, 0.05, 'Root never changes the user Y baseline');
  tick();
  near(runtime.getAmbientDiagnostics().rootOffset,
    Math.max(0, Math.min(runtime.getAmbientDiagnostics().sample.rootY,
      (1 - (resolveHalfBodyFraming(1280, 800).y + 0.05 + 0.1 * resolveHalfBodyFraming(1280, 800).scale)) / 2 - 0.02)) * 2,
    'Root safety recomputes after user movement');
  const framing = resolveHalfBodyFraming(1280, 800);
  runtime.setModelPosition(0.1, 0.91 - framing.y - meshTop * framing.scale);
  tick();
  near(runtime.getAmbientDiagnostics().rootOffset, 0.05,
    'current drawable bounds limit Root to the remaining viewport headroom', 1e-6);
  assert.ok(runtime.getModelMatrix().transformY(meshTop) <= 0.960001,
    'root translation retains the required top margin');
  meshTop = 0.3;
  tick();
  near(runtime.getAmbientDiagnostics().rootOffset, 0,
    'a changed animated mesh recomputes the safety limit instead of using a fixed head calibration');
  meshTop = 0.1;
  runtime.setModelPosition(0, 0);

  runtime._eyeBlinkIds = ['ParamEyeLOpen', 'ParamEyeROpen'];
  runtime.pauseAutoBlink();
  runtime._nativeMotionParameters = new Set(['ParamAngleX', 'ParamEyeBallX', 'ParamBreath', 'ParamEyeLOpen', 'ParamMouthOpenY']);
  runtime._motionManager = { isFinished: () => false, updateMotion() {
    core.setParameterValueById('ParamAngleX', 27);
    core.setParameterValueById('ParamEyeBallX', 0.95);
    core.setParameterValueById('ParamBreath', 0.9);
    core.setParameterValueById('ParamEyeLOpen', 0.2);
    core.setParameterValueById('ParamMouthOpenY', 0.65);
  } };
  tick(60);
  near(values.get('ParamAngleX'), 27, 'native head motion retains the valid Core range beyond autonomous limits', 1e-4);
  near(values.get('ParamEyeBallX'), 0.95, 'native gaze retains the valid Core range beyond autonomous limits', 1e-7);
  near(values.get('ParamBreath'), 0.9, 'native breathing ownership suppresses autonomous breath writes');
  near(values.get('ParamEyeLOpen'), 0.2, 'native eye opening is not overwritten by the blink layer');
  near(values.get('ParamMouthOpenY'), 0.65, 'native mouth remains in control without active speech');
  runtime.setSpeaking(true);
  runtime.setLipSyncValue(0.8);
  tick();
  near(values.get('ParamMouthOpenY'), 0.8, 'actual speech mouth playback has explicit priority');
  runtime.setSpeaking(false);
  runtime.setLipSyncValue(0);
  tick();
  near(values.get('ParamMouthOpenY'), 0.65, 'speech ending releases the mouth back to native motion');
  runtime._motionManager = { isFinished: () => true };
  tick(90);
  // 自主輸出含持續微動；容差只涵蓋 ParameterMixer 追隨微動的延遲。
  const autonomousAngleX = () => runtime._parameterMixer.normalized('ParamAngleX', runtime.getAmbientDiagnostics().sample.headX);
  near(values.get('ParamAngleX'), autonomousAngleX(), 'finished native motion smoothly releases back to the latest Python target', 0.1);
  assert.ok(values.get('ParamEyeBallX') < -0.4, 'finished native gaze releases back to the current autonomous gaze');

  const emotion = { ...createNeutralTargetParams(), mouthForm: 0.45, blushLevel: 0.3, browLY: 0.2,
    bodyAngleX: 0.25, bodyAngleY: -0.2, bodyAngleZ: 0.15,
    eyeLOpen: 0.75, eyeROpen: 0.75, eyeBallX: 0.6, mouthOpenBias: 0.8 };
  const idlePlan = { enterAfterMs: 1000, settlePose: { preset: 'happy_soft', durationSec: 0.3, params: emotion },
    ambientEnterAfterMs: 1000, ambientSwitchIntervalMs: 500,
    ambientPlan: { states: [{ kind: 'legacy', params: { mouthForm: -1 } }] } };
  runtime.applyIdlePlan(idlePlan);
  runtime.applyBasePose({ preset: 'reaction', durationSec: 3, params: { ...emotion, mouthForm: -0.5,
    bodyAngleX: 0, bodyAngleY: 0, bodyAngleZ: 0, eyeBallX: 0, eyeBallY: 0 } });
  runtime.enqueueMicroEvent(mouthEvent);
  tick(30);
  const faceOnly = runtime.getAmbientDiagnostics().sample;
  assert.ok(faceOnly.headX > 0.5 && faceOnly.bodyX > 0.3 && faceOnly.rootY > 0.001,
    'a face-only expression keeps autonomous head, body and Root activity running');
  assert.ok(values.get('ParamAngleX') > 10 && values.get('ParamBodyAngleX') > 2,
    'face-only expression does not claim unrelated Core head/body channels');
  const timerBefore = runtime._aiBehaviorTimer;
  const eventsBefore = runtime._activeExpressionEvents;
  const breathBefore = runtime.getAmbientDiagnostics().sample.breath;
  runtime.applyAmbientPlan({ ...plan, sequence: 2, target: { ...plan.target, headX: -0.5 } });
  runtime.applyAmbientMotion(0, now);
  assert.equal(runtime._aiBehaviorTimer, timerBefore, 'ambient updates cannot restart or cancel a Chat action');
  assert.equal(runtime._activeExpressionEvents, eventsBefore, 'ambient updates preserve pending explicit events');
  near(runtime.getAmbientDiagnostics().sample.breath, breathBefore, 'ambient updates preserve the visible breathing phase');
  runtime.cancelExpressionAction();
  assert.equal(runtime.getAmbientDiagnostics().action, false);
  assert.equal(runtime._activeExpressionEvents.length, 0, 'cancel removes short action events');
  tick(180);
  near(values.get('ParamMouthForm'), emotion.mouthForm, 'cancel returns to the settled emotion', 1e-7);
  near(values.get('ParamCheek'), emotion.blushLevel, 'cancel preserves settled blush', 1e-7);
  near(values.get('ParamMouthOpenY'), 0, 'settle clears transient surprised mouth opening', 1e-7);
  near(runtime._ambientBase.eyeBallX, 0, 'a past gaze offset cannot redefine the calibrated forward target');
  const leaningBody = ['ParamBodyAngleX', 'ParamBodyAngleY', 'ParamBodyAngleZ'].map(id => values.get(id));
  runtime.applyIdlePlan({ ...idlePlan, settlePose: { ...idlePlan.settlePose,
    params: { ...emotion, bodyAngleX: 0, bodyAngleY: 0, bodyAngleZ: 0 } } });
  tick(120);
  const neutralBody = ['ParamBodyAngleX', 'ParamBodyAngleY', 'ParamBodyAngleZ'].map(id => values.get(id));
  assert.ok(leaningBody[0] - neutralBody[0] > 0.5 && leaningBody[1] - neutralBody[1] < -0.3 &&
    leaningBody[2] - neutralBody[2] > 0.2, 'settled body lean remains visible alongside the same autonomous target');
  runtime.applyIdlePlan(idlePlan);
  tick(120);
  const adoptedBase = structuredClone(runtime._ambientBase);
  runtime.applyBasePose({ preset: 'reaction', durationSec: 1, params: { ...emotion, mouthForm: -0.7 } });
  runtime.finishExpressionAction();
  tick(180);
  near(values.get('ParamMouthForm'), emotion.mouthForm, 'finish preserves the same settled emotion', 1e-7);
  assert.deepEqual(structuredClone(runtime._ambientBase), adoptedBase,
    'retired frontend idle timers never replace the settled emotion');
  runtime.setManualControl(300);
  assert.ok(runtime._manualUntilMs > now, 'manual controls obtain a bounded runtime ownership window');
  runtime.setAiBehavior(0, 0.8, 1, 1, 0.3, -0.4);
  tick(10);
  assert.ok(values.get('ParamMouthForm') < 0 && values.get('ParamCheek') > 0.5,
    'the existing manual parameter panel can apply setAiBehavior during its ownership window');
  tick(120);
  near(values.get('ParamMouthForm'), emotion.mouthForm, 'manual ownership expiry returns to the settled emotion', 1e-5);

  runtime.applyAmbientPlan({ ...plan, sequence: 3, target: { ...plan.target, headX: 0 } });
  runtime.setNativeParamOverride('ParamAngleX', 22);
  const expiryFrames = [];
  for (let frame = 1; frame <= 302; frame++) {
    tick();
    if (frame >= 299) expiryFrames.push({ value: values.get('ParamAngleX'), physics: physicsSnapshot.get('ParamAngleX') });
  }
  near(expiryFrames[0].value, 22, 'the native override remains effective before its five-second expiry');
  near(expiryFrames[0].physics, 22, 'Physics reads the active native override, not the earlier autonomous value');
  for (let index = 1; index < expiryFrames.length; index++) {
    assert.ok(expiryFrames[index].value > 16 && Math.abs(expiryFrames[index].value - expiryFrames[index - 1].value) < 3,
      'override expiry preserves the current rendered position across frames 299–302');
    near(expiryFrames[index].physics, expiryFrames[index].value, 'released override values remain consistent before Physics');
  }
  assert.equal(runtime.getNativeParamOverrides().has('ParamAngleX'), false, 'expired overrides are removed');
  tick(120);
  near(values.get('ParamAngleX'), autonomousAngleX(), 'expired native override smoothly returns to the autonomous target', 0.1);

  runtime.setAiBehavior(0, 0, 1, 1, 2, 0, 0, 0, 0, 0, 0, 0, true, 0, 0, 0, 0, 0, 0, 0, 0.8, 0, 0, 0);
  tick(60);
  const explicitBreath = values.get('ParamBreath');
  assert.ok(explicitBreath > 0.7, 'direct setAiBehavior can explicitly claim breathLevel above the default');
  runtime.cancelExpressionAction();
  tick();
  assert.ok(Math.abs(values.get('ParamBreath') - explicitBreath) < 0.25 &&
    values.get('ParamBreath') > runtime.getAmbientDiagnostics().sample.breath + 0.05,
    'breath cancellation smoothly releases ownership instead of jumping to the autonomous curve');
  tick(120);
  near(values.get('ParamBreath'), runtime.getAmbientDiagnostics().sample.breath,
    'released breathing follows the continuously running autonomous phase', 0.04);

  saved.set('ParamEyeBallX', 0);
  saved.set('ParamEyeBallY', 0);
  runtime.enqueueScheduledMicroEvent({ kind: 'gaze', patch: { eyeBallX: 0.8 }, durationMs: 1500,
    returnToBase: true }, now + 5000);
  tick(60);
  assert.ok(values.get('ParamEyeBallX') < -0.4 && runtime.getAmbientDiagnostics().sample.gazeX < -0.5,
    'a future queued eye event cannot claim gaze before its scheduled start');
  tick(270);
  assert.ok(values.get('ParamEyeBallX') > 0.4, 'the scheduled eye event owns gaze once its active window begins');
  runtime.cancelExpressionAction();
  tick(60);
  assert.ok(values.get('ParamEyeBallX') < -0.4, 'cancelling the eye event smoothly restores the autonomous gaze');

  runtime.setModelPosition(0, 0.88 - framing.y - meshTop * framing.scale);
  runtime.applyAmbientPlan({ ...plan, sequence: 4, breath: { ...plan.breath, rootAmplitude: 0 },
    hop: { height: 0.1, durationMs: 1000 } });
  const boundedHop = [];
  for (let frame = 0; frame < 60; frame++) {
    tick();
    boundedHop.push(runtime.getAmbientDiagnostics().rootOffset);
  }
  assert.ok(Math.max(...boundedHop) > 0.03 && Math.max(...boundedHop) <= 0.080001,
    'the full hop is scaled into the available viewport height');
  assert.ok(boundedHop.filter(value => value >= 0.079999).length <= 3,
    'viewport fitting must not turn a one-second hop into a long clipped ceiling plateau');
  runtime.applyAmbientPlan({ ...plan, sequence: 5, breath: { ...plan.breath, rootAmplitude: 0.08 } });
  tick(180);
  const boundedBreath = [];
  for (let frame = 0; frame < 240; frame++) {
    tick();
    boundedBreath.push(runtime.getAmbientDiagnostics().rootOffset);
  }
  assert.ok(Math.max(...boundedBreath) > 0.06 && Math.max(...boundedBreath) <= 0.080001,
    'large root breathing keeps its full waveform within the visible height budget');
  assert.ok(boundedBreath.filter(value => value >= 0.079999).length <= 3,
    'large root breathing must not flatten against the viewport ceiling');

  const beforeHopRoot = runtime.getAmbientDiagnostics().rootOffset;
  runtime.applyAmbientPlan({ ...plan, sequence: 6, hop: { height: 0.1, durationMs: 1000 } });
  runtime.applyAmbientMotion(0, now);
  runtime.limitRootToViewport(0);
  runtime.updateModelMatrix();
  near(runtime.getAmbientDiagnostics().rootOffset, beforeHopRoot,
    'a new hop envelope cannot instantly rescale ongoing root breathing');
  tick(15);
  assert.ok(runtime.getAmbientDiagnostics().rootOffset > 0.02, 'offline inheritance is exercised during a visible hop');
  const beforeHopCancel = runtime.getAmbientDiagnostics().rootOffset;
  runtime.applyAmbientPlan({ ...plan, sequence: 7, state: 'listening' });
  runtime.applyAmbientMotion(0, now);
  runtime.limitRootToViewport(0);
  runtime.updateModelMatrix();
  near(runtime.getAmbientDiagnostics().rootOffset, beforeHopCancel,
    'listening cancellation preserves the root breathing and hop handoff height');
  runtime.disconnectAmbient();
  runtime.applyAmbientMotion(0, now);
  runtime.limitRootToViewport(0);
  near(runtime.getAmbientDiagnostics().rootOffset, beforeHopCancel,
    'disconnecting a hop does not jump the root breathing envelope');
  assert.equal(runtime.getAmbientDiagnostics().active, true, 'disconnect retains initialized basic breathing');
  const replacement = new RuntimeModel();
  replacement.inheritAmbientBaseline(runtime);
  assert.equal(replacement.getAmbientDiagnostics().active, true, 'offline replacement keeps the basic runtime initialized');
  assert.deepEqual(structuredClone(replacement._ambientBase), structuredClone(runtime._ambientBase),
    'offline replacement retains the settled emotion and body baseline');
  assert.notEqual(replacement._ambientBase, runtime._ambientBase, 'resource instances do not share a mutable settled pose');
  near(replacement._ambientMotion.update(0, now, true).rootY, 0, 'offline replacement never replays the previous hop');
  let replacementSample;
  for (let frame = 1; frame <= 60; frame++) replacementSample = replacement._ambientMotion.update(1 / 60, now + frame * 1000 / 60, true);
  assert.ok(replacementSample.breath > 0 && replacementSample.rootY <= 0.006,
    'offline replacement continues basic breathing without a new Python plan');

  // 八方向都必須到達最後的眼球參數，不能只在 payload 裡有 gazeY。
  runtime.cancelExpressionAction();
  runtime.setEyeTrackingEnabled(false);
  const settledBeforeGaze = structuredClone(runtime._ambientBase);
  const gazeDirections = [
    ['left', -0.6, 0], ['right', 0.6, 0],
    ['up', 0, 1], ['down', 0, -1],
    ['upper_left', -0.8, 1], ['upper_right', 0.8, 1],
    ['lower_left', -0.8, -1], ['lower_right', 0.8, -1],
  ];
  for (const [index, [name, x, y]] of gazeDirections.entries()) {
    runtime.applyAmbientPlan({ ...plan, sequence: 100 + index,
      target: { ...plan.target, gazeX: x, gazeY: y } });
    tick(60);
    near(values.get('ParamEyeBallX'), x * 0.85, `${name} reaches final eye X`, 1e-6);
    near(values.get('ParamEyeBallY'), y, `${name} reaches the stronger final eye Y`, 1e-6);
    near(physicsSnapshot.get('ParamEyeBallY'), values.get('ParamEyeBallY'),
      `${name} vertical gaze survives the complete frame`, 1e-6);
  }
  runtime.applyAmbientPlan({ ...plan, sequence: 108, state: 'listening',
    target: { ...plan.target, gazeX: 0, gazeY: 0 } });
  tick(60);
  near(values.get('ParamEyeBallX'), 0, 'listening returns X to calibrated forward', 1e-6);
  near(values.get('ParamEyeBallY'), 0, 'listening returns Y to calibrated forward', 1e-6);
  assert.deepEqual(structuredClone(runtime._ambientBase), settledBeforeGaze,
    'eight-direction glances preserve the settled emotion and body baseline');
  runtime.setEyeTrackingEnabled(true);
  tick(60);
  near(values.get('ParamEyeBallX'), 0.8 * 0.55 * 0.85, 'pointer horizontal amplitude remains unchanged', 1e-6);
  near(values.get('ParamEyeBallY'), 0.7 * 0.45 * 0.85, 'stronger autonomous Y preserves pointer amplitude', 1e-6);
  runtime.setEyeTrackingEnabled(false);
  tick(60);
  near(values.get('ParamEyeBallY'), 0, 'pointer release returns to calibrated autonomous gaze', 1e-6);
  const smallerEyeRange = new ParameterMixer({ ...core,
    getParameterMinimumValue: index => ids[index] === 'ParamEyeBallY' ? -0.6 : core.getParameterMinimumValue(index),
    getParameterMaximumValue: index => ids[index] === 'ParamEyeBallY' ? 0.7 : core.getParameterMaximumValue(index),
  });
  near(smallerEyeRange.normalized('ParamEyeBallY', 1), 0.7, 'stronger Y still intersects the model upper bound');
  near(smallerEyeRange.normalized('ParamEyeBallY', -1), -0.6, 'stronger Y still intersects the model lower bound');
  near(smallerEyeRange.write('ParamEyeBallY', 2, 0, true), 0.7, 'oversized Y is clamped before Core');
}

// Python 自主計畫只由前端連續執行；驗證真實 guard、曲線與收尾，不複製 runtime。
{
  const { isAmbientPlan } = load('src/types/ambientPlan.ts');
  const { AmbientMotion, clampRootY } = load('src/live2d/runtime/AmbientMotion.ts');
  const ambientPlan = (overrides = {}) => ({
    type: 'ambient_plan', sequence: 1, state: 'idle', moveMs: 900,
    target: { headX: 0.8, headY: -0.4, headZ: 0.6, bodyX: 0.3, bodyY: -0.2, bodyZ: 0.5, gazeX: -0.6, gazeY: 0.2 },
    breath: { periodSec: 4.8, amplitude: 0.7, rootAmplitude: 0.04 }, ...overrides,
  });
  assert.equal(isAmbientPlan(ambientPlan()), true);
  for (const malformed of [
    { sequence: 0 }, { sequence: -1 }, { sequence: 1.5 }, { sequence: Infinity },
    { moveMs: 299 }, { moveMs: 3001 }, { state: 'speaking' },
    { target: { ...ambientPlan().target, gazeX: NaN } },
    { target: { ...ambientPlan().target, headY: 1.1 } },
    { target: { headX: 0 } },
    { breath: { periodSec: 2, amplitude: 0.5, rootAmplitude: 0.01 } },
    { breath: { periodSec: 4, amplitude: Infinity, rootAmplitude: 0.01 } },
    { breath: { periodSec: 4, amplitude: 0.5, rootAmplitude: 0.081 } },
    { hop: { height: 0.121, durationMs: 800 } },
    { hop: { height: 0.1, durationMs: 1601 } },
  ]) assert.equal(isAmbientPlan(ambientPlan(malformed)), false, `invalid ambient payload ${JSON.stringify(malformed)}`);
  const advanceAmbient = (runtime, rate, startMs, seconds, enabled = true, body = 1, gaze = 1) => {
    let sample;
    for (let frame = 1; frame <= Math.round(rate * seconds); frame++) {
      sample = runtime.update(1 / rate, startMs + frame * 1000 / rate, enabled, body, gaze);
      assert.ok(Object.values(sample).every(Number.isFinite), 'ambient samples stay finite');
      assert.ok(sample.breath >= 0 && sample.breath <= 1 && sample.rootY >= 0 && sample.rootY <= 0.2);
    }
    return sample;
  };
  const samples = [30, 60, 120].map(rate => {
    const runtime = new AmbientMotion();
    runtime.apply(ambientPlan({ breath: { periodSec: 3.4, amplitude: 0.7, rootAmplitude: 0.04 } }), 0);
    return advanceAmbient(runtime, rate, 0, 2);
  });
  for (const sample of samples.slice(1)) {
    near(sample.headX, samples[0].headX, 'head trajectory is frame-rate independent', 1e-7);
    near(sample.gazeX, samples[0].gazeX, 'gaze trajectory is frame-rate independent', 1e-7);
    near(sample.breath, samples[0].breath, 'breath phase is frame-rate independent', 1e-6);
    near(sample.rootY, samples[0].rootY, 'root does not accumulate across frame rates', 1e-6);
  }
  const rapidGaze = [30, 60, 120].map(rate => {
    const runtime = new AmbientMotion();
    runtime.apply(ambientPlan({ moveMs: 2400, target: { ...ambientPlan().target, headX: 0.85, gazeX: 0.85 } }), 0);
    const sample = advanceAmbient(runtime, rate, 0, 0.2);
    assert.ok(sample.gazeX > 0.8 && sample.headX < 0.1,
      'gaze reaches its independent short movement before a slow head/body move');
    return sample;
  });
  for (const sample of rapidGaze.slice(1)) near(sample.gazeX, rapidGaze[0].gazeX,
    'independent rapid gaze stays continuous at 30/60/120 Hz', 1e-8);
  const layeredMotion = [30, 60, 120].map(rate => {
    const runtime = new AmbientMotion();
    runtime.apply(ambientPlan({ moveMs: 1200,
      target: { ...ambientPlan().target, headX: 0.8, bodyX: 0.8, gazeX: 0.8 } }), 0);
    const sample = advanceAmbient(runtime, rate, 0, 0.5);
    assert.ok(sample.gazeX > sample.headX + 0.2 && sample.headX > sample.bodyX + 0.1,
      'eyes arrive before the head and the slower torso at each frame rate');
    return sample;
  });
  for (const sample of layeredMotion.slice(1)) near(sample.bodyX, layeredMotion[0].bodyX,
    'slower torso timing stays frame-rate independent', 1e-8);
  const quiet = new AmbientMotion();
  const unfocused = new AmbientMotion();
  const quietTarget = { ...ambientPlan().target, headX: 0.4, bodyZ: -0.3 };
  quiet.apply(ambientPlan({ target: quietTarget }), 0);
  unfocused.apply(ambientPlan({ target: quietTarget }), 0);
  const settled = advanceAmbient(quiet, 60, 0, 6);
  advanceAmbient(unfocused, 60, 0, 6);
  quiet.apply(ambientPlan({ sequence: 2, target: { ...quietTarget, gazeX: 0.025 } }), 6000);
  const refocused = advanceAmbient(quiet, 60, 6000, 0.5);
  const unchanged = advanceAmbient(unfocused, 60, 6000, 0.5);
  // 微動照常進行；同一時間線上，重新對焦不得改變頭身軌跡。
  near(refocused.headX, unchanged.headX, 'a quiet refocus does not restart a settled head', 1e-8);
  near(refocused.bodyZ, unchanged.bodyZ, 'a quiet refocus preserves a settled torso', 1e-7);
  assert.ok(Math.abs(refocused.headX - settled.headX) > 1e-4,
    'settled head keeps a subtle continuous drift instead of freezing');
  assert.ok(refocused.gazeX > 0.024 && refocused.gazeX <= 0.025,
    'quiet refocus moves only the shared small gaze target');
  assert.ok(Math.abs(refocused.breath - settled.breath) > 0.01,
    'quiet posture holds retain the running breathing phase');
  const shortBody = new AmbientMotion();
  shortBody.apply(ambientPlan({ moveMs: 300, target: { ...ambientPlan().target, gazeX: 0.85 } }), 0);
  near(advanceAmbient(shortBody, 60, 0, 0.2).gazeX, rapidGaze[1].gazeX,
    'body moveMs does not change gaze timing');
  const steady = new AmbientMotion();
  const refreshed = new AmbientMotion();
  steady.apply(ambientPlan(), 0);
  refreshed.apply(ambientPlan(), 0);
  advanceAmbient(steady, 60, 0, 1);
  advanceAmbient(refreshed, 60, 0, 1);
  assert.equal(refreshed.apply(ambientPlan(), 1000), false, 'duplicate plan cannot restart a trajectory');
  assert.equal(refreshed.apply(ambientPlan({ sequence: 2 }), 1000), true);
  const oldSample = advanceAmbient(steady, 60, 1000, 1);
  const refreshedSample = advanceAmbient(refreshed, 60, 1000, 1);
  near(refreshedSample.breath, oldSample.breath, 'new plans preserve the breathing phase');
  near(refreshedSample.headX, oldSample.headX, 'new plans preserve current position and velocity');
  assert.equal(refreshed.apply(ambientPlan({ sequence: 1 }), 2000), false, 'stale plans must be discarded');
  assert.equal(refreshed.apply(ambientPlan({ sequence: 3 }), NaN), false, 'invalid timestamps must be rejected');
  refreshed.apply(ambientPlan({ sequence: 3, target: { ...ambientPlan().target, headX: -0.8 } }), 2000);
  near(refreshed.update(0, 2000, true).headX, refreshedSample.headX,
    'opposite targets start from the current rendered position');
  assert.ok(advanceAmbient(refreshed, 60, 2000, 2).headX < -0.7,
    'an interrupted trajectory reaches the new target without resetting the runtime');
  assert.ok(Object.values(refreshed.update(NaN, Infinity, true, Infinity, NaN)).every(Number.isFinite),
    'invalid local timing or weights cannot poison future frames');

  const hopping = new AmbientMotion();
  const noRootBreath = { periodSec: 4.8, amplitude: 0.5, rootAmplitude: 0 };
  hopping.apply(ambientPlan({ breath: noRootBreath, hop: { height: 0.1, durationMs: 1000 } }), 0);
  const rising = advanceAmbient(hopping, 120, 0, 0.25);
  assert.ok(rising.rootY > 0.02, 'hop must visibly rise');
  hopping.apply(ambientPlan({ sequence: 2, state: 'listening', breath: noRootBreath }), 250);
  const atHandoff = hopping.update(0, 250, true);
  near(atHandoff.rootY, rising.rootY, 'listening cancellation must not teleport the root');
  const landed = advanceAmbient(hopping, 120, 250, 1);
  assert.ok(landed.rootY < 0.0001, 'listening must finish the current hop while root breathing settles');
  hopping.apply(ambientPlan({ sequence: 3, state: 'listening', breath: noRootBreath, hop: { height: 0.1, durationMs: 1000 } }), 1250);
  near(advanceAmbient(hopping, 60, 1250, 1).rootY, 0, 'listening never starts a new hop', 1e-6);

  const disconnected = new AmbientMotion();
  disconnected.apply(ambientPlan({ hop: { height: 0.1, durationMs: 1000 } }), 0);
  const beforeDisconnect = advanceAmbient(disconnected, 120, 0, 0.25);
  disconnected.disconnect(250);
  near(disconnected.update(0, 250, true).rootY, beforeDisconnect.rootY, 'disconnect preserves root continuity');
  const offline = advanceAmbient(disconnected, 120, 250, 2);
  assert.ok(offline.rootY <= 0.0061, 'disconnected runtime retains only basic root breathing');
  assert.ok(offline.breath > 0, 'basic breathing survives disconnect');
  assert.equal(disconnected.apply(ambientPlan(), 2250), true, 'a new connection may restart sequence numbering');
  const disabled = advanceAmbient(disconnected, 60, 2250, 2, false);
  assert.ok(Math.abs(disabled.headX) < 1e-4 && disabled.rootY < 1e-4, 'disable smoothly settles all contributions');
  const weighted = advanceAmbient(disconnected, 60, 4250, 1, true, 0, 0);
  assert.ok(Math.abs(weighted.headX) < 1e-4 && Math.abs(weighted.gazeX) < 1e-4 && weighted.rootY < 1e-4,
    'explicit action owners can suppress body and gaze without stopping the runtime');
  assert.ok(advanceAmbient(disconnected, 60, 5250, 1).headX > 0.6, 'ownership release smoothly returns to the current plan');

  const resumed = new AmbientMotion();
  resumed.apply(ambientPlan({ hop: { height: 0.1, durationMs: 1000 } }), 0);
  const beforePause = advanceAmbient(resumed, 60, 0, 0.25);
  const afterPause = resumed.update(0, 10000, true);
  near(afterPause.rootY, beforePause.rootY, 'background tab resume keeps the last rendered height');
  assert.ok(advanceAmbient(resumed, 60, 10000, 1).rootY < 0.041,
    'background tab resume settles the old hop without replaying it');

  near(clampRootY(0.12, 0.08), 0.06, 'root respects calibrated headroom');
  near(clampRootY(0.12, 0.01), 0, 'insufficient headroom prevents upward translation');
  near(clampRootY(0.12, 0.08, 0.04), 0.04, 'root retains the requested top margin');
  near(clampRootY(NaN, 0.08), 0, 'invalid root offsets are suppressed');
  near(clampRootY(0.12, Infinity), 0, 'unknown headroom cannot move the root offscreen');
}

// 真實 canvas 同步服務；只替換分頁通訊、GPU 擷取與時鐘邊界。
{
  const clock = new FakeClock();
  const channels = new Set();
  const captures = [];
  const received = [];
  let captureCount = 0;
  let receiverCount = 0;
  class Bitmap {
    closed = false;
    constructor(label) { this.label = label; }
    close() { this.closed = true; }
  }
  class Channel {
    listeners = new Set();
    closed = false;
    constructor(name) { this.name = name; channels.add(this); }
    addEventListener(type, listener) { if (type === 'message') this.listeners.add(listener); }
    removeEventListener(type, listener) { if (type === 'message') this.listeners.delete(listener); }
    postMessage(data) {
      assert.equal(this.closed, false, 'a closed channel cannot send');
      for (const channel of channels) {
        if (channel === this || channel.name !== this.name || channel.closed) continue;
        const clone = data.type === 'frame' ? { ...data, bitmap: new Bitmap(data.bitmap.label) } : { ...data };
        void Promise.resolve().then(() => {
          if (channel.closed) clone.bitmap?.close();
          else for (const listener of [...channel.listeners]) listener({ data: clone });
        });
      }
    }
    close() { this.closed = true; channels.delete(this); }
  }
  const realm = () => {
    const events = new Map();
    const intervals = new Set();
    const window = {
      setInterval(callback, delay) {
        const interval = { timer: null };
        const tick = () => {
          callback();
          interval.timer = clock.setTimeout(tick, delay);
        };
        interval.timer = clock.setTimeout(tick, delay);
        intervals.add(interval);
        return interval;
      },
      clearInterval(interval) { clock.clearTimeout(interval.timer); intervals.delete(interval); },
      addEventListener(type, listener) {
        if (!events.has(type)) events.set(type, new Set());
        events.get(type).add(listener);
      },
      removeEventListener(type, listener) { events.get(type)?.delete(listener); },
      emit(type) { for (const listener of [...(events.get(type) ?? [])]) listener(); },
      crash() { for (const interval of intervals) this.clearInterval(interval); },
    };
    const canvasLoad = createLoader({}, {
      window, BroadcastChannel: Channel, ImageBitmap: Bitmap,
      performance: { now: () => clock.now }, crypto: { randomUUID: () => `receiver_${receiverCount++}` },
      createImageBitmap() {
        captureCount++;
        const capture = deferred();
        captures.push(capture);
        return capture.promise;
      },
    });
    return { service: canvasLoad('src/services/canvasSyncService.ts').canvasSyncService, window };
  };
  const delegate = () => ({
    callback: null,
    setPostRenderCallback(callback) { this.callback = callback; },
    frame() { this.callback?.({}); },
  });
  const complete = async label => {
    assert.ok(captures.length, 'a GPU capture must be pending');
    const bitmap = new Bitmap(label);
    captures.shift().resolve(bitmap);
    await flushMicrotasks();
    assert.equal(bitmap.closed, true, 'the source releases every captured bitmap');
  };
  const receive = bitmap => { received.push(bitmap.label); bitmap.close(); };
  const source = realm();
  const main = delegate();
  source.service.attachToRenderLoop(main);
  await flushMicrotasks();
  for (let frame = 0; frame < 120; frame++) main.frame();
  assert.equal(captureCount, 0, 'no display receiver means zero GPU captures');

  const display = realm();
  let closeDisplay = display.service.startReceiving(receive);
  await flushMicrotasks();
  main.frame();
  main.frame();
  assert.equal(captureCount, 1, 'an active receiver enables capture with one pending readback');
  await complete('first');
  assert.deepEqual(received, ['first'], 'the existing frame/bitmap contract reaches the receiver');
  closeDisplay();
  await flushMicrotasks();
  for (let frame = 0; frame < 120; frame++) main.frame();
  assert.equal(captureCount, 1, 'receiver cleanup immediately stops capture');

  closeDisplay = display.service.startReceiving(receive);
  await flushMicrotasks();
  main.frame();
  closeDisplay();
  await flushMicrotasks();
  await complete('closed_while_pending');
  assert.equal(received.length, 1, 'closing the receiver discards an in-flight capture');

  closeDisplay = display.service.startReceiving(receive);
  await flushMicrotasks();
  main.frame();
  const remounted = delegate();
  source.service.attachToRenderLoop(remounted);
  assert.equal(main.callback, null, 'remount removes the old render hook');
  await flushMicrotasks();
  remounted.frame();
  const beforeOldCompletion = captureCount;
  await complete('old_generation');
  remounted.frame();
  assert.equal(captureCount, beforeOldCompletion, 'stale capture completion cannot unlock the new readback');
  assert.equal(received.length, 1, 'stale render generations never publish frames');
  await complete('remounted');
  assert.equal(received.at(-1), 'remounted');

  source.service.stopBroadcasting();
  source.service.channel.close();
  const reload = realm();
  const reloadedMain = delegate();
  reload.service.attachToRenderLoop(reloadedMain);
  await flushMicrotasks();
  const beforeReload = captureCount;
  reloadedMain.frame();
  assert.equal(captureCount, beforeReload + 1, 'source_ready restores an existing receiver after main reload');
  await complete('reloaded');

  clock.advance(8000);
  await flushMicrotasks();
  const beforeHeartbeat = captureCount;
  reloadedMain.frame();
  assert.equal(captureCount, beforeHeartbeat + 1, 'heartbeat renews a receiver beyond its original lease');
  await complete('heartbeat');
  display.window.emit('pagehide');
  await flushMicrotasks();
  const beforeHide = captureCount;
  reloadedMain.frame();
  assert.equal(captureCount, beforeHide, 'pagehide immediately stops capture');
  display.window.emit('pageshow');
  await flushMicrotasks();
  reloadedMain.frame();
  assert.equal(captureCount, beforeHide + 1, 'pageshow restores a cached display receiver');
  await complete('restored');

  display.window.crash();
  display.service.channel.close();
  clock.advance(6001);
  await flushMicrotasks();
  const beforeExpired = captureCount;
  for (let frame = 0; frame < 120; frame++) reloadedMain.frame();
  assert.equal(captureCount, beforeExpired, 'a crashed receiver stops capture after its lease expires');
  const secondDisplay = realm();
  const closeSecond = secondDisplay.service.startReceiving(receive);
  await flushMicrotasks();
  reloadedMain.frame();
  await complete('new_receiver');
  closeSecond();
  await flushMicrotasks();
  reload.service.stopBroadcasting();
  assert.equal(reloadedMain.callback, null, 'stopping the source removes its render hook');
  assert.equal(captureCount, 8);
  assert.equal(received.length, 6);
  assert.equal(captures.length, 0, 'canvas sync leaves no GPU readback pending');
}

console.log('Rushia runtime checks passed: shared model switching, failure recovery, framing, expression composition, action timing, cancelled TTS, autonomous ambient execution and on-demand canvas mirroring.');
