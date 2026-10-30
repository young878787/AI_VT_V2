// 單一 headX／bodyX 階躍目標的響應：到位時間、過衝（扣掉同 seed 零目標的對照組，只看目標造成的位移）。
// 用法：node scripts/idle-motion/step-response.mjs [srcRoot]
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const ts = createRequire(path.join(appRoot, 'package.json'))('typescript');
const root = path.resolve(process.argv[2] ?? path.join(appRoot, 'src'));
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
const { ParameterMixer } = load(path.join(root, 'live2d/runtime/ParameterMixer.ts'));
const ids = ['ParamAngleX', 'ParamBodyAngleX'];
const vals = new Map(ids.map(i => [i, 0]));
const mk = () => new ParameterMixer({ getParameterCount: () => 2, getParameterId: i => ({ getString: () => ids[i] }), getParameterDefaultValue: () => 0,
  getParameterMinimumValue: i => i ? -10 : -45, getParameterMaximumValue: i => i ? 10 : 45, getParameterValueByIndex: i => vals.get(ids[i]), setParameterValueByIndex: (i, v) => vals.set(ids[i], v) });
const tgt = { headX: 0.4, headY: 0, headZ: 0, bodyX: 0.4, bodyY: 0, bodyZ: 0, gazeX: 0, gazeY: 0 };
const zero = Object.fromEntries(Object.keys(tgt).map(k => [k, 0]));
const breath = { periodSec: 4.8, amplitude: 0, rootAmplitude: 0 };
const run = (target) => { const m = new AmbientMotion(5), mix = mk(); m.apply({ type: 'ambient_plan', sequence: 1, state: 'idle', moveMs: 1000, target, breath }, 0);
  const out = []; for (let f = 1; f <= 60 * 6; f++) { const t = f * 1000 / 60; const s = m.update(1 / 60, t, true);
    out.push([mix.write('ParamAngleX', mix.normalized('ParamAngleX', s.headX), 1 / 60), mix.write('ParamBodyAngleX', mix.normalized('ParamBodyAngleX', s.bodyX), 1 / 60)]); } return out; };
const a = run(tgt), b = run(zero);
const head = a.map((r, i) => r[0] - b[i][0]), body = a.map((r, i) => r[1] - b[i][1]);
const fin = 0.4 * 35, finB = 0.4 * 8;
const peak = v => Math.max(...v);
const t90 = (v, f) => (v.findIndex(x => x >= 0.9 * f) + 1) * 1000 / 60;
console.log(`head: final≈${head.at(-1).toFixed(2)}° (target ${fin}) peak ${peak(head).toFixed(2)}° overshoot ${((peak(head) / fin - 1) * 100).toFixed(1)}%  t90 ${t90(head, fin).toFixed(0)}ms`);
console.log(`body: final≈${body.at(-1).toFixed(2)}° (target ${finB}) peak ${peak(body).toFixed(2)}° overshoot ${((peak(body) / finB - 1) * 100).toFixed(1)}%  t90 ${t90(body, finB).toFixed(0)}ms`);
