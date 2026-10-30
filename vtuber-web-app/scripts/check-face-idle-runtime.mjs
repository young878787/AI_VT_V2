import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const filename = path.join(root, 'src/live2d/runtime/FaceIdleRuntime.ts');
const module = { exports: {} };
const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  fileName: filename,
}).outputText;
vm.runInNewContext(code, { module, exports: module.exports }, { filename });
const { FaceIdleRuntime, BROW_FORM_GAIN, BROW_ANGLE_GAIN } = module.exports;

const BROW_KEYS = ['eyeL', 'eyeR', 'browLForm', 'browRForm', 'browLAngle', 'browRAngle'];
const baseContext = {
  enabled: true, eyesAllowed: true, browsAllowed: true, listening: false,
  blinkPaused: false, blinkReady: true, eyeLOpen: 1, eyeROpen: 1,
};
const near = (actual, expected, label, tolerance = 1e-8) => {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: expected ${expected}, got ${actual}`);
};

function rig(context = {}) {
  const values = [];
  let fallback = 0;
  let draws = 0;
  const runtime = new FaceIdleRuntime(() => { draws += 1; return values.length ? values.shift() : fallback; });
  const state = { ...baseContext, ...context };
  let now = 0;
  let sample = runtime.update(0, now, state);
  return {
    runtime, context: state, values,
    fallback(value) { fallback = value; },
    draws() { return draws; },
    at(target, step = 5) {
      assert.ok(target >= now);
      if (target === now) sample = runtime.update(0, now, state);
      while (now < target) {
        const next = Math.min(target, now + step);
        sample = runtime.update((next - now) / 1000, next, state);
        for (const value of [sample.eyeL, sample.eyeR]) assert.ok(value >= 0 && value <= 1, 'eye multipliers stay within 0..1');
        for (const value of [sample.browLForm, sample.browRForm]) assert.ok(Math.abs(value) <= 0.14 * BROW_FORM_GAIN + 1e-9, 'brow form deltas stay bounded');
        for (const value of [sample.browLAngle, sample.browRAngle]) assert.ok(Math.abs(value) <= 0.14 * BROW_ANGLE_GAIN + 1e-9, 'brow angle deltas stay bounded');
        now = next;
      }
      return sample;
    },
    gap(delta) {
      now += delta;
      sample = runtime.update(delta / 1000, now, state);
      return sample;
    },
  };
}

// 各種一般眼皮事件在完整停留時符合目標範圍，最深仍至少保留 0.64。
for (const [pick, kind, minimum, maximum] of [
  [0.3, 'relax', 0.84, 0.94], [0.5, 'half', 0.78, 0.88], [0.66, 'deep', 0.64, 0.75],
]) {
  const test = rig();
  test.at(2999);
  test.values.push(pick);
  const sample = test.at(3800);
  assert.equal(sample.kind, kind);
  assert.ok(sample.eyeL >= minimum && sample.eyeL <= maximum);
  near(sample.eyeL, sample.eyeR, 'ordinary relaxation keeps the eyes paired');
  const draws = test.draws();
  for (let index = 0; index < 10; index += 1) {
    const repeated = test.at(3800);
    for (const key of BROW_KEYS) near(repeated[key], sample[key], 'repeated sampling never accumulates');
  }
  assert.equal(test.draws(), draws, 'stable sampling must not draw random numbers');
  near(test.at(5100).eyeL, 1, 'event returns to the expression baseline');
}

{
  const test = rig();
  test.at(2999);
  test.values.push(0.76, 0.5, 0.5, 0);
  const sample = test.at(3800);
  assert.equal(sample.kind, 'asymmetric');
  assert.ok(sample.eyeL >= 0.74 && sample.eyeL <= 0.88);
  assert.ok(sample.eyeR >= 0.92 && sample.eyeR <= 1);
}

// 半閉眼原表情只接受淺放鬆；回傳乘數不覆蓋原本左右差異。
{
  const test = rig({ eyeLOpen: 0.5, eyeROpen: 0.85 });
  test.at(2999);
  test.values.push(0.66);
  const sample = test.at(3800);
  assert.equal(sample.kind, 'relax');
  assert.ok(sample.eyeL >= 0.90 && sample.eyeL <= 0.96);
  near((0.5 * sample.eyeL) / (0.85 * sample.eyeR), 0.5 / 0.85, 'base asymmetry remains intact');
}

for (const pick of [0.5, 0.66, 0.76]) {
  const test = rig({ listening: true });
  test.at(2999);
  test.values.push(pick);
  const sample = test.at(3800);
  assert.equal(sample.kind, 'relax', 'listening replaces half/deep/asymmetric with light relaxation');
  assert.ok(sample.eyeL >= 0.90 && sample.eyeL <= 0.96);
  near(sample.eyeL, sample.eyeR, 'listening keeps both eyes paired');
}

// 已開始的深眼皮在聆聽／低基準切換時，保留當下值並於 500ms 平滑收尾。
for (const context of [{ listening: true }, { eyeLOpen: 0.5 }]) {
  for (const pick of [0.5, 0.66, 0.76]) {
    const test = rig();
    test.at(2999);
    test.values.push(pick);
    const before = test.at(3800);
    const draws = test.draws();
    Object.assign(test.context, context);
    const release = test.at(3800);
    assert.equal(release.kind, 'relax');
    for (const key of BROW_KEYS) near(release[key], before[key], `continuous release ${key}`);
    assert.equal(test.draws(), draws, 'release does not redraw targets');
    const middle = test.at(4050);
    near(middle.eyeL, (before.eyeL + 1) / 2, 'release left eye reaches midpoint after 250ms');
    near(middle.eyeR, (before.eyeR + 1) / 2, 'release right eye reaches midpoint after 250ms');
    near(middle.browLForm, before.browLForm / 2, 'release keeps the brow envelope continuous');
    const ended = test.at(4300);
    near(ended.eyeL, 1, 'release finishes within 500ms');
    near(ended.eyeR, 1, 'release finishes both eyes');
    near(ended.browLForm, 0, 'release finishes remaining brows');
    assert.equal(ended.kind, 'still', 'persistent context must not restart the release every frame');
  }
}

{
  const test = rig();
  test.at(2999);
  test.values.push(0.66);
  const before = test.at(3350);
  near(before.eyeL, 0.82, 'release test begins halfway through closing');
  test.context.listening = true;
  near(test.at(3350).eyeL, before.eyeL, 'release uses the current coefficient rather than the full target');
  near(test.at(3600).eyeL, (before.eyeL + 1) / 2, 'partial closure releases from its actual midpoint');
  near(test.at(3850).eyeL, 1, 'partial closure also completes release in 500ms');
}

{
  const test = rig();
  test.at(2999);
  test.values.push(0.66, 0.5, 1, 1, 1, 1);
  const lowest = test.at(4400).eyeL;
  near(test.at(5600).eyeL, lowest, 'deep relaxation holds for at most 1200ms');
  assert.ok(test.at(5601).eyeL > lowest, 'deep relaxation starts reopening after its shorter hold');
  near(test.at(7200).eyeL, 1, 'maximum deep envelope returns fully to baseline');
}

// 首次 wink 至少等待 45 秒；已選同側時降低近期一側權重。
{
  const test = rig();
  test.at(47999);
  test.values.push(0.99, 0.1, 0, 0, 0, 1);
  assert.equal(test.at(48000).kind, 'winkLeft');
  const left = test.at(48150);
  near(left.eyeL, 0, 'left wink fully closes only the left eye');
  near(left.eyeR, 1, 'left wink keeps the right eye open');
  assert.equal(left.winking, true);
  assert.equal(test.at(48420).winking, false);
  test.at(93419);
  test.values.push(0.99, 0.5, 0, 0, 0, 1);
  assert.equal(test.at(93420).kind, 'winkRight', 'recent left wink makes the same roll choose right');
  const right = test.at(93570);
  near(right.eyeL, 1, 'right wink keeps the left eye open');
  near(right.eyeR, 0, 'right wink closes only the right eye');
  test.at(138839);
  test.values.push(0.99, 0.5, 0, 0, 0, 1);
  assert.equal(test.at(138840).kind, 'winkLeft', 'recent right wink makes the same roll choose left');
}

{
  const test = rig();
  test.at(47999);
  test.values.push(0.99, 0.1, 1, 1, 1, 1);
  near(test.at(48090).eyeL, 0.5, 'longest wink closing lasts 180ms');
  near(test.at(48180).eyeL, 0, 'longest wink fully closes');
  near(test.at(48430).eyeL, 0, 'longest wink hold lasts 250ms');
  near(test.at(48605).eyeL, 0.5, 'longest wink opening lasts 350ms');
  near(test.at(48780).eyeL, 1, 'longest wink returns to baseline');
}

// 即使每次抽中 wink，早於首次等待或冷卻期間都不能開始。
{
  const test = rig();
  test.fallback(0.99);
  let previous = false;
  const starts = [];
  for (let now = 20; now <= 160000; now += 20) {
    const sample = test.at(now, 20);
    if (sample.winking && !previous) starts.push(now);
    previous = sample.winking;
  }
  assert.ok(starts.length >= 2, 'must exercise repeated wink attempts');
  assert.ok(starts[0] >= 45000, 'first wink must wait 45 seconds');
  for (let index = 1; index < starts.length; index += 1) {
    assert.ok(starts[index] - starts[index - 1] >= 45000 + 420, 'wink cooldown includes the full preceding event');
  }
}

for (const context of [
  { blinkReady: false }, { blinkPaused: true }, { listening: true },
  { eyeLOpen: 0.77 }, { eyeLOpen: 0.8, eyeROpen: 1 },
]) {
  const test = rig(context);
  test.at(47999);
  test.values.push(0.99);
  const sample = test.at(48150);
  assert.equal(sample.winking, false, `wink must respect context ${JSON.stringify(context)}`);
  if (context.blinkPaused) near(sample.eyeL, 1, 'pause suppresses ordinary eye relaxation too');
}

// 明確的眼／眉接管各自刪除對應組，放回所有權不補播原事件。
{
  const test = rig();
  test.at(2999);
  test.values.push(0.4);
  const active = test.at(3800);
  assert.ok(active.eyeL < 1 && active.browLForm !== 0);
  test.context.eyesAllowed = false;
  const browsOnly = test.at(3800);
  near(browsOnly.eyeL, 1, 'eyes owner suppresses eyes only');
  assert.ok(browsOnly.browLForm !== 0, 'remaining brow event continues');
  test.context.eyesAllowed = true;
  near(test.at(3900).eyeL, 1, 'restored eye ownership does not replay eyes');
  test.context.browsAllowed = false;
  near(test.at(3900).browLForm, 0, 'brow owner suppresses brows');
  test.context.browsAllowed = true;
  near(test.at(4000).browLForm, 0, 'restored brow ownership does not replay brows');
}

{
  const test = rig();
  test.at(2999);
  test.values.push(0.4);
  test.at(3800);
  test.context.browsAllowed = false;
  const eyesOnly = test.at(3800);
  assert.ok(eyesOnly.eyeL < 1, 'eyes continue after brow ownership changes');
  near(eyesOnly.browLForm, 0, 'brow delta immediately clears');
  test.context.browsAllowed = true;
  near(test.at(3900).browLForm, 0, 'old brow event never returns');
  test.runtime.cancelEyes(3900);
  near(test.at(4000).eyeL, 1, 'cancelEyes discards current eyelids');
  test.fallback(0.8);
  near(test.at(6899).eyeL, 1, 'cancelEyes imposes a complete new interval');
}

// 眉毛事件用 BrowForm／BrowAngle（BrowY 行程只有 0.8px 看不見）：上揚配內側上提，左右 Angle 符號相反才對稱。
{
  const test = rig();
  test.at(2999);
  test.values.push(0.88);
  const sample = test.at(3800);
  assert.equal(sample.kind, 'brow');
  near(sample.eyeL, 1, 'brow-only event preserves eyes');
  near(Math.abs(sample.browLForm), 0.06 * BROW_FORM_GAIN, 'smallest brow form delta');
  near(sample.browLAngle, -sample.browLForm * BROW_ANGLE_GAIN / BROW_FORM_GAIN, 'left angle opposes the left form');
  near(sample.browRForm, 0, 'brow sides may remain independent');
  near(sample.browRAngle, 0, 'untouched side keeps its angle');
}

// 關閉、背景大 gap、reset 均回到基準，不補播舊事件。
{
  const test = rig();
  test.at(2999);
  test.values.push(0.66);
  assert.ok(test.at(3800).eyeL < 1);
  const resumed = test.gap(10000);
  near(resumed.eyeL, 1, 'background gap discards old eye events');
  near(resumed.browLForm, 0, 'background gap discards old brow events');
  test.context.enabled = false;
  near(test.at(13800).eyeL, 1, 'global disable is neutral');
  test.context.enabled = true;
  near(test.at(13800).eyeL, 1, 'enable schedules a fresh delay');
  test.runtime.reset();
  test.context.blinkReady = true;
  near(test.at(13800).eyeL, 1, 'reset initializes against the new timestamp');
  test.fallback(0.99);
  for (let now = 13820; now < 58800; now += 20) {
    assert.equal(test.at(now, 20).winking, false, 'reset restarts the first-wink cooldown');
  }
}

// 不依 frame 數累加，30／60／120 Hz 在共同時間點得到相同值。
{
  const results = [30, 60, 120].map(hz => {
    const test = rig();
    test.at(2999, 1000 / hz);
    test.values.push(0.66);
    return [3100, 3500, 3800, 4500, 5100, 13000].map(now => test.at(now, 1000 / hz));
  });
  for (let rate = 1; rate < results.length; rate += 1) {
    results[0].forEach((expected, index) => {
      for (const key of BROW_KEYS) near(results[rate][index][key], expected[key], `frame independent ${key}`);
      assert.equal(results[rate][index].kind, expected.kind);
    });
  }
}

// 長時間掛機：臉部事件常見且看得出來，但不密集到每秒都在變。
{
  let seed = 20261026;
  const random = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };
  const runtime = new FaceIdleRuntime(random);
  const starts = [];
  let previous = 'still';
  let deepestRelax = 1;
  let strongestBrow = 0;
  const context = { ...baseContext };
  for (let now = 0; now <= 600000; now += 1000 / 60) {
    const sample = runtime.update(1 / 60, now, context);
    if (sample.kind !== 'still' && previous === 'still') starts.push(now);
    if (sample.kind === 'relax') deepestRelax = Math.min(deepestRelax, sample.eyeL);
    strongestBrow = Math.max(strongestBrow, Math.abs(sample.browLForm), Math.abs(sample.browRForm));
    previous = sample.kind;
  }
  const perMinute = starts.length / 10;
  assert.ok(perMinute >= 4 && perMinute <= 10, `face events stay frequent but calm: ${perMinute}/min`);
  assert.ok(Math.max(...starts.slice(1).map((start, index) => start - starts[index])) <= 25000,
    'the face never stays idle for long stretches');
  assert.ok(deepestRelax <= 0.86, `relaxation reaches a visible depth: ${deepestRelax}`);
  assert.ok(strongestBrow >= 0.1 * BROW_FORM_GAIN, `brow accents reach a visible size: ${strongestBrow}`);
}

console.log('Face idle runtime checks passed: bounded events, balanced wink cooldown, blink guards, group ownership, cancellation, reset and sampling.');
