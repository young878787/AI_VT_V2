import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const filename = path.join(root, 'src/live2d/runtime/BlinkRuntime.ts');
const module = { exports: {} };
const source = fs.readFileSync(filename, 'utf8');
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  fileName: filename,
}).outputText;
vm.runInNewContext(code, { module, exports: module.exports }, { filename });
const { BlinkRuntime } = module.exports;

const near = (actual, expected, label, tolerance = 1e-8) => {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: expected ${expected}, got ${actual}`);
};

function clock(runtime, origin = 0) {
  let now = origin;
  let value = runtime.update(0, now);
  return {
    at(target, allowed = true, step = 5, automaticEnabled = true) {
      assert.ok(target >= now, 'test clock must advance');
      if (target === now) value = runtime.update(0, now, allowed, automaticEnabled);
      while (now < target) {
        const next = Math.min(target, now + step);
        value = runtime.update((next - now) / 1000, next, allowed, automaticEnabled);
        assert.ok(Number.isFinite(value) && value >= 0 && value <= 1, 'openness must stay within 0..1');
        now = next;
      }
      return value;
    },
    gap(delta, allowed = true) {
      now += delta;
      value = runtime.update(delta / 1000, now, allowed);
      return value;
    },
  };
}

// 三段時間使用每次事件抽樣；最短、最長都完整閉眼後再睜眼。
for (const [random, closing, closed, opening] of [[0, 70, 20, 120], [1, 120, 60, 200]]) {
  const runtime = new BlinkRuntime(() => random);
  const time = clock(runtime);
  runtime.forceBlink();
  near(time.at(0), 1, 'forced blink begins open');
  near(time.at(closing / 2), 0.5, 'random closing midpoint');
  near(time.at(closing), 0, 'closing completes');
  near(time.at(closing + closed / 2), 0, 'closed hold');
  near(time.at(closing + closed), 0, 'opening begins closed');
  near(time.at(closing + closed + opening / 2), 0.5, 'random opening midpoint');
  near(time.at(closing + closed + opening), 1, 'blink completes fully open');
}

// 明確測試 7.5% 邊界；第二眨必須等第一眨睜開後才開始。
for (const [random, expectedDouble] of [[0.074, true], [0.075, false]]) {
  const runtime = new BlinkRuntime(() => random);
  runtime.setBlinkingIntervalRange(1, 1);
  const time = clock(runtime);
  const firstEnd = 1000 + 70 + random * 50 + 20 + random * 40 + 120 + random * 80;
  near(time.at(firstEnd), 1, 'first blink finishes before second');
  near(time.at(firstEnd + 99), 1, 'double blink cannot start before 100ms');
  assert.equal(time.at(1400) < 1, expectedDouble, 'only the configured chance schedules a second blink');
}

{
  let random = () => 1;
  const runtime = new BlinkRuntime(() => random());
  runtime.setBlinkingIntervalRange(1, 1);
  const time = clock(runtime);
  let draws = 0;
  random = () => (++draws === 4 ? 0 : 1);
  near(time.at(1380), 1, 'longest first blink fully opens');
  near(time.at(1559), 1, 'longest double gap lasts 180ms');
  near(time.at(1560), 1, 'second blink starts after the longest double gap');
  near(time.at(1620), 0.5, 'second blink has its own sampled closing phase');
}

// 強制連續命中雙眨機率時，雙眨完成後仍保留至少八秒冷卻。
{
  const runtime = new BlinkRuntime(() => 0);
  runtime.setBlinkingIntervalRange(1, 1);
  const time = clock(runtime);
  const starts = [];
  let previous = 1;
  for (let now = 1; now <= 21000; now += 1) {
    const value = time.at(now);
    if (previous === 1 && value < 1) starts.push(now - 1);
    previous = value;
  }
  const doubles = starts.filter((start, index) => index > 0 && start - starts[index - 1] < 400);
  assert.ok(doubles.length >= 2, 'test must observe repeated double blink events');
  for (let index = 1; index < doubles.length; index += 1) {
    const nextFirstStart = starts[starts.indexOf(doubles[index]) - 1];
    assert.ok(nextFirstStart - (doubles[index - 1] + 210) >= 8000, 'double cooldown starts after the second blink ends');
  }
  near(starts[1] - starts[0] - 210, 100, 'second blink waits after fully opening');
}

// pause／resume 會丟棄未完成雙眨；已暫停時 force 仍能完成單眨。
{
  const runtime = new BlinkRuntime(() => 0);
  const time = clock(runtime);
  time.at(1250);
  runtime.pause();
  assert.equal(runtime.isPaused(), true);
  near(time.at(1400), 1, 'pause cancels a queued second blink');
  runtime.forceBlink();
  time.at(1400);
  near(time.at(1435), 0.5, 'force works during indefinite pause');
  near(time.at(1610), 1, 'forced blink completes during pause');
  assert.equal(runtime.isPaused(), true, 'force with no new pause keeps the existing pause');
  near(time.at(2500), 1, 'paused runtime stays under expression control');
  runtime.resume();
  assert.equal(runtime.isPaused(), false);
  near(time.at(3499), 1, 'resume schedules a fresh interval');
  assert.ok(time.at(3535) < 1, 'automatic blink returns after resume');
}

// 暫停時間從完整睜眼才開始，不能從 force 指令呼叫時提早起算。
{
  const runtime = new BlinkRuntime(() => 0);
  const time = clock(runtime);
  runtime.forceBlink(1);
  time.at(0);
  near(time.at(210), 1, 'forced blink ends before timed pause');
  assert.equal(runtime.isPaused(), true);
  time.at(1209);
  assert.equal(runtime.isPaused(), true, 'pause lasts a full second after opening');
  time.at(1210);
  assert.equal(runtime.isPaused(), false);
  near(time.at(2209), 1, 'timed resume also schedules a new interval');
  assert.ok(time.at(2245) < 1, 'timed resume restores automatic blink');
}

// 首次 frame 使用 performance.now 等非零時間時，先前的 pause 不會立刻過期。
{
  const runtime = new BlinkRuntime(() => 0);
  runtime.pause(0.5);
  const time = clock(runtime, 10000);
  time.at(10499);
  assert.equal(runtime.isPaused(), true);
  time.at(10500);
  assert.equal(runtime.isPaused(), false);
}

// 關閉自動效果只停止自動事件，立即單眨仍能完整執行。
{
  const runtime = new BlinkRuntime(() => 0);
  const time = clock(runtime);
  time.at(1250);
  near(time.at(1400, true, 5, false), 1, 'automatic off cancels a queued second blink');
  runtime.forceBlink();
  near(time.at(1400, true, 5, false), 1, 'force starts with automatic effects off');
  near(time.at(1435, true, 5, false), 0.5, 'force closes with automatic effects off');
  near(time.at(1610, true, 5, false), 1, 'force fully opens with automatic effects off');
  near(time.at(2500, true, 5, false), 1, 'automatic off must not schedule any follow-up');
  near(time.at(2500), 1, 'automatic enable starts a fresh interval');
  near(time.at(3499), 1, 'automatic enable does not replay a queued second blink');
  assert.ok(time.at(3535) < 1, 'automatic enable returns normal behavior');
}

// 外部接管及背景恢復不能補播舊事件，也不能排隊補播強制要求。
{
  const runtime = new BlinkRuntime(() => 0);
  const time = clock(runtime);
  assert.ok(time.at(1035) < 1);
  near(time.at(1040, false), 1, 'native/manual ownership suppresses the current blink');
  runtime.forceBlink(1);
  near(time.at(1200, false), 1, 'force is discarded while external owner is active');
  time.at(1400, false);
  near(time.at(1400), 1, 'ownership release does not replay old events');
  near(time.at(2399), 1, 'ownership release waits a fresh interval');
  assert.ok(time.at(2435) < 1);
  near(time.gap(10000), 1, 'background resume discards an active blink');
  near(time.at(13434), 1, 'background resume waits a fresh interval');
  assert.ok(time.at(13470) < 1);
  assert.equal(runtime.isPaused(), false, 'suppressed force must not leave a pending pause');
}

// 仍可調整間隔；不合法設定不得污染目前設定。
{
  const runtime = new BlinkRuntime(() => 0);
  const time = clock(runtime);
  runtime.setBlinkingIntervalRange(2, 6);
  assert.equal(runtime.getIntervalRange().min, 2);
  assert.equal(runtime.getIntervalRange().max, 6);
  runtime.setBlinkingIntervalRange(Number.NaN, 1);
  runtime.setBlinkingIntervalRange(-1, 1);
  runtime.setBlinkingIntervalRange(6, 2);
  assert.equal(runtime.getIntervalRange().min, 2);
  assert.equal(runtime.getIntervalRange().max, 6);
  near(time.at(1999), 1, 'updated interval controls automatic start');
  assert.ok(time.at(2035) < 1);
}

// 30／60／120 Hz 取樣在共同時間點得到相同係數，不依 frame 數延長階段。
{
  const samples = [30, 60, 120].map(hz => {
    const runtime = new BlinkRuntime(() => 0.5);
    runtime.setBlinkingIntervalRange(1, 1);
    const time = clock(runtime);
    return [1050, 1110, 1220, 1300, 2400].map(now => time.at(now, true, 1000 / hz));
  });
  for (let rate = 1; rate < samples.length; rate += 1) {
    samples[0].forEach((expected, index) => near(samples[rate][index], expected, 'frame rate independent envelope'));
  }
}

console.log('Blink runtime checks passed: randomized phases, double blink cooldown, controls, ownership, frame gaps and sampling.');
