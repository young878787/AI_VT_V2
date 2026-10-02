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
const assetDirectory = path.join(root, 'public/Resources/Rushia');
const manifest = JSON.parse(fs.readFileSync(path.join(assetDirectory, fixedModel.fileName), 'utf8'));
const references = manifest.FileReferences;
for (const asset of [
  references.Moc, references.Physics, ...references.Textures,
  ...references.Expressions.map(expression => expression.File),
  ...Object.values(references.Motions).flat().map(motion => motion.File),
]) assert.ok(fs.existsSync(path.join(assetDirectory, asset)), `missing Rushia resource: ${asset}`);

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
      LAppLive2DManager: { getInstance: () => ({ getActiveModel: () => ({ cancelExpressionAction() {} }) }) },
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

console.log('Rushia runtime checks passed: fixed assets, framing, mouth/eye composition, action timing and cancelled TTS.');
