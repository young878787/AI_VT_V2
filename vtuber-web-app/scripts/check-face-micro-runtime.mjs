import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const filename = path.join(root, 'src/live2d/runtime/FaceMicroRuntime.ts');
const module = { exports: {} };
const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  fileName: filename,
}).outputText;
vm.runInNewContext(code, { module, exports: module.exports, Math }, { filename });
const { FaceMicroRuntime } = module.exports;

const baseContext = { enabled: true, mouthAllowed: true, pupilsAllowed: true, listening: false };
const near = (actual, expected, label, tolerance = 1e-8) => {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: expected ${expected}, got ${actual}`);
};
const plain = sample => JSON.parse(JSON.stringify(sample));
const NEUTRAL = { mouthForm: 0, mouthPout: 0, mouthAngle: 0, eyeBallForm: 0 };
const mouthMagnitude = sample => Math.abs(sample.mouthForm) + sample.mouthPout + sample.mouthAngle;

// 建構時固定抽 4 個數給瞳孔波形；之後第一次啟用抽事件間隔。fallback 0.5 → 間隔 6500ms。
function rig(context = {}) {
  const values = [];
  let draws = 0;
  const runtime = new FaceMicroRuntime(() => { draws += 1; return values.length ? values.shift() : 0.5; });
  const state = { ...baseContext, ...context };
  let now = 0;
  let sample = runtime.update(0, now, state);
  return {
    runtime, context: state, values, draws: () => draws,
    at(target, step = 5) {
      assert.ok(target >= now);
      while (now < target) {
        const next = Math.min(target, now + step);
        sample = runtime.update((next - now) / 1000, next, state);
        now = next;
      }
      return sample;
    },
    gap(delta) { now += delta; sample = runtime.update(delta / 1000, now, state); return sample; },
  };
}

// 各種嘴型事件在完整停留時落在範圍內；smile／flat 只動 MouthForm，pout 只動 Pout＋Angle。
for (const [pick, kind] of [[0.2, 'smile'], [0.6, 'flat'], [0.9, 'pout']]) {
  const test = rig();
  test.at(6499);
  test.values.push(pick);
  const sample = test.at(6500 + 1100 + 100);
  if (kind === 'smile') {
    assert.ok(sample.mouthForm >= 0.12 && sample.mouthForm <= 0.28, `smile form ${sample.mouthForm}`);
    near(sample.mouthPout + sample.mouthAngle, 0, 'smile keeps pout at rest');
  } else if (kind === 'flat') {
    assert.ok(sample.mouthForm <= -0.1 && sample.mouthForm >= -0.22, `flat form ${sample.mouthForm}`);
    near(sample.mouthPout + sample.mouthAngle, 0, 'flat keeps pout at rest');
  } else {
    assert.ok(sample.mouthPout >= 0.55 && sample.mouthPout <= 0.9, `pout ${sample.mouthPout}`);
    assert.ok(sample.mouthAngle >= 25 && sample.mouthAngle <= 50, `pout angle ${sample.mouthAngle}`);
    near(sample.mouthForm, 0, 'pout does not mix MouthForm');
  }
  const draws = test.draws();
  const repeated = test.at(6500 + 1100 + 100);
  assert.deepEqual(plain(repeated), plain(sample), 'repeated sampling never accumulates');
  assert.equal(test.draws(), draws, 'stable sampling must not draw random numbers');
  assert.equal(mouthMagnitude(test.at(6500 + 1100 + 2200 + 1300 + 100)), 0, 'event returns to the baseline');
}

// 尖嘴：聆聽中或冷卻內改成 flat；冷卻 40s 後可再次出現。
{
  const test = rig({ listening: true });
  test.at(6499);
  test.values.push(0.9);
  const sample = test.at(7800);
  assert.ok(sample.mouthForm < 0 && sample.mouthPout === 0, 'listening replaces pout with a flat mouth');
}
{
  const test = rig();
  test.at(6499);
  test.values.push(0.9);
  assert.ok(test.at(7800).mouthPout > 0);
  test.at(12000);
  test.values.push(0.9);
  // 事件於 ≥ 6500+700+... 結束後再排；以下一個事件開始時間檢查冷卻。
  let next = null;
  for (let now = 12000; now < 40000 && !next; now += 5) {
    const sample = test.at(now);
    if (sample.mouthForm !== 0 || sample.mouthPout > 0) next = { now, sample };
  }
  assert.ok(next && next.sample.mouthPout === 0, 'cooldown turns the second pout into a flat mouth');
}

// 嘴被說話／表情接手：快速淡出並丟掉事件，歸還後不補播，要等完整新間隔。
{
  const test = rig();
  test.at(6499);
  test.values.push(0.2);
  const active = test.at(7800);
  assert.ok(active.mouthForm > 0);
  test.context.mouthAllowed = false;
  const fading = test.at(7860);
  assert.ok(fading.mouthForm < active.mouthForm && fading.mouthForm > 0, 'fades instead of snapping');
  assert.equal(mouthMagnitude(test.at(8600)), 0, 'ownership removes the mouth within ~800ms');
  test.context.mouthAllowed = true;
  assert.equal(mouthMagnitude(test.at(9000)), 0, 'returned ownership does not replay the event');
  assert.equal(mouthMagnitude(test.at(8600 + 3900)), 0, 'a full new interval is respected');
}

// 瞳孔：慢速、不超過幅度，所有權消失時淡出為 0；不是事件。
{
  const test = rig();
  let peak = 0;
  for (let now = 0; now <= 60000; now += 1000 / 60) peak = Math.max(peak, Math.abs(test.at(now, 1000 / 60).eyeBallForm));
  assert.ok(peak > 0.15 && peak <= 0.3 + 1e-9, `pupil breathing stays within +/-0.3: ${peak}`);
  test.context.pupilsAllowed = false;
  assert.ok(Math.abs(test.at(60000 + 3000).eyeBallForm) < 0.003, 'pupil releases to 0 when another owner takes it');
}

// 關閉、背景大 gap、reset 均回中性，不補播舊事件。
{
  const test = rig();
  test.at(6499);
  test.values.push(0.2);
  assert.ok(test.at(7800).mouthForm > 0);
  const resumed = test.gap(10000);
  assert.deepEqual(plain(resumed), NEUTRAL, 'background gap discards old events');
  test.context.enabled = false;
  assert.deepEqual(plain(test.gap(1000)), NEUTRAL, 'global disable is neutral');
  test.runtime.reset();
}

// 30／60／120Hz 輸出一致（絕對時間排程 + 解析式包絡）。
{
  const results = [30, 60, 120].map(hz => {
    const test = rig();
    test.at(6499, 1000 / hz);
    test.values.push(0.2);
    return [6900, 7600, 8500, 9500, 12000, 13000].map(now => test.at(now, 1000 / hz));
  });
  for (let rate = 1; rate < results.length; rate += 1) {
    results[0].forEach((expected, index) => {
      for (const key of Object.keys(expected)) near(results[rate][index][key], expected[key], `frame independent ${key}`, 1e-6);
    });
  }
}

// 長時間掛機：嘴型事件常見但不密集、尖嘴偶爾出現且遵守冷卻、數值有界且可重現。
{
  const run = () => {
    let seed = 20261030;
    const random = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };
    const runtime = new FaceMicroRuntime(random);
    const starts = [];
    const pouts = [];
    let previous = 0;
    let previousPout = 0;
    let extremes = { form: 0, pout: 0, angle: 0, pupil: 0 };
    const trace = [];
    for (let now = 0; now <= 600000; now += 1000 / 60) {
      const sample = runtime.update(1 / 60, now, baseContext);
      const magnitude = mouthMagnitude(sample);
      if (magnitude > 0 && previous === 0) starts.push(now);
      if (sample.mouthPout > 0 && previousPout === 0) pouts.push(now);
      previous = magnitude;
      previousPout = sample.mouthPout;
      extremes = {
        form: Math.max(extremes.form, Math.abs(sample.mouthForm)), pout: Math.max(extremes.pout, sample.mouthPout),
        angle: Math.max(extremes.angle, sample.mouthAngle), pupil: Math.max(extremes.pupil, Math.abs(sample.eyeBallForm)),
      };
      if (Math.round(now) % 5000 === 0) trace.push(sample);
    }
    return { starts, pouts, extremes, trace };
  };
  const first = run();
  assert.deepEqual(plain(run().trace), plain(first.trace), 'same random source reproduces the same output');
  const perMinute = first.starts.length / 10;
  assert.ok(perMinute >= 3 && perMinute <= 8, `mouth events stay calm: ${perMinute}/min`);
  assert.ok(first.pouts.length >= 2 && first.pouts.length <= 12, `pouts are occasional: ${first.pouts.length}/10min`);
  for (let index = 1; index < first.pouts.length; index += 1) {
    assert.ok(first.pouts[index] - first.pouts[index - 1] >= 40000 - 1, 'pout cooldown holds');
  }
  assert.ok(first.extremes.form <= 0.28 + 1e-9 && first.extremes.pout <= 0.9 + 1e-9
    && first.extremes.angle <= 50 + 1e-9 && first.extremes.pupil <= 0.3 + 1e-9, JSON.stringify(first.extremes));
  assert.ok(first.extremes.form >= 0.15 && first.extremes.pupil >= 0.2, 'values reach a visible size');
}

console.log('Face micro runtime checks passed: mouth events, pout cooldown, ownership fade, pupil breathing, reset and sampling.');
