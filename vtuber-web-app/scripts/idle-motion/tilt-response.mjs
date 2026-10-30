// headZ／headX 出發 5 秒再歸位 5 秒：量出發過衝、歸位微彈、持住沉回、峰速、到位時間（與零目標對照組相減）。
// 用法：node scripts/idle-motion/tilt-response.mjs
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const ts = createRequire(path.join(appRoot, 'package.json'))('typescript');
const root = path.join(appRoot, 'src');
const cache = new Map();
const load = file => {
  if (cache.has(file)) return cache.get(file).exports;
  const module = { exports: {} }; cache.set(file, module);
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: file }).outputText;
  vm.runInNewContext(code, { module, exports: module.exports, require: n => load(path.resolve(path.dirname(file), n + '.ts')) }, { filename: file });
  return module.exports;
};
const { AmbientMotion } = load(path.join(root, 'live2d/runtime/AmbientMotion.ts'));
const zero = { headX: 0, headY: 0, headZ: 0, bodyX: 0, bodyY: 0, bodyZ: 0, gazeX: 0, gazeY: 0 };
const plan = (sequence, target, moveMs = 1200) => ({ type: 'ambient_plan', sequence, state: 'idle', moveMs, target, breath: { periodSec: 4.8, amplitude: 0, rootAmplitude: 0 } });
for (const [axis, amp] of [['headZ', 0.6], ['headZ', 0.8], ['headX', 0.4]]) for (const seed of [5, 9, 21]) {
  const m = new AmbientMotion(seed), c = new AmbientMotion(seed);
  m.apply(plan(1, { ...zero, [axis]: amp }), 0); c.apply(plan(1, zero), 0);
  const up = [], down = []; const peakSpeed = []; let prev = 0;
  for (let f = 1; f <= 60 * 10; f++) { const t = f * 1000 / 60;
    if (f === 60 * 5) { m.apply(plan(2, zero), t); c.apply(plan(2, zero), t); }
    const d = m.update(1 / 60, t, true)[axis] - c.update(1 / 60, t, true)[axis];
    if (f <= 300) { up.push(d); peakSpeed.push(Math.abs(d - prev) * 60); } else down.push(d); prev = d; }
  const deg = { headX: 35, headZ: 23 }[axis];
  const at = s => up[Math.round(s * 60) - 1];
  console.log(axis, amp, 'seed', seed, '| depart overshoot', (Math.max(...up) / amp - 1).toFixed(3), '| return bounce', (-Math.min(...down) / amp).toFixed(3),
    '| sag@4.9s', (1 - up.at(-1) / amp).toFixed(3), '| peak°/s', (Math.max(...peakSpeed.slice(1)) * deg).toFixed(1),
    '| t90', ((up.findIndex(x => x >= 0.9 * amp) + 1) * 1000 / 60).toFixed(0), 'ret t<10%', ((down.findIndex(x => x <= 0.1 * amp) + 1) * 1000 / 60).toFixed(0));
}
