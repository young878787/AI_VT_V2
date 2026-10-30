// 以真實 CubismPhysics + Cubism Core 驅動 physics3.json，量測頭髮／緞帶對頭身動作的響應。
// 情境：輸入角度用 0.4s 的 smoothstep 推到目標、持住 1s、再用 0.4s 放回 0（頭一停就看慣性與回彈），共 5 秒。
// 每個輸出列出：峰值、動作停止後的最大反向擺幅（占峰值）、停止後反向次數（幅度 > 10% 峰值）、停止後回到 5% 峰值內的時間。
// 另可帶 rows.json（measure.mjs 以 DUMP 輸出的 30Hz idle 軌跡），改量實際 idle 下各輸出的 RMS、峰值與「近乎靜止」占比。
// 用法：node scripts/idle-motion/physics-response.mjs [physics3.json] [moc3] [rows.json]
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(path.join(appRoot, 'package.json'));
const ts = require('typescript');
const physicsPath = path.resolve(process.argv[2] ?? path.join(appRoot, 'public/Resources/Rushia/RushiaHD.physics3.json'));
const mocPath = path.resolve(process.argv[3] ?? path.join(appRoot, 'public/Resources/Rushia/RushiaHD.moc3'));
const rowsPath = process.argv[4];

const coreSrc = fs.readFileSync(path.join(appRoot, 'public/Core/live2dcubismcore.js'), 'utf8');
const Core = new Function('require', 'process', '__dirname', coreSrc + '\nreturn Live2DCubismCore;')(require, process, '/tmp');
for (let i = 0; i < 200 && !(Core.Moc && Core.Moc.fromArrayBuffer); i += 1) await new Promise(r => setTimeout(r, 10));
await new Promise(r => setTimeout(r, 300));

const cache = new Map();
const load = file => {
  if (cache.has(file)) return cache.get(file).exports;
  const module = { exports: {} }; cache.set(file, module);
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: file }).outputText;
  vm.runInNewContext(code, {
    module, exports: module.exports, Live2DCubismCore: Core, console, Math, Float32Array, Int32Array, Uint8Array, Array, Map, Set, Object, Number, String, JSON, Error,
    require: n => {
      if (!n.startsWith('.')) return require(n);
      const p = path.resolve(path.dirname(file), n);
      return load(p.endsWith('.ts') ? p : `${p}.ts`);
    },
  }, { filename: file });
  return module.exports;
};
const framework = path.join(appRoot, 'src/live2d/framework');
const { CubismFramework } = load(path.join(framework, 'live2dcubismframework.ts'));
CubismFramework.startUp();
CubismFramework.initialize();
const { CubismPhysics } = load(path.join(framework, 'physics/cubismphysics.ts'));

const read = file => { const b = fs.readFileSync(file); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); };
const model = Core.Model.fromMoc(Core.Moc.fromArrayBuffer(read(mocPath)));
const P = model.parameters;
const ids = Array.from(P.ids);
const index = name => ids.indexOf(name);
const mock = { getModel: () => model, getParameterCount: () => P.count, getParameterIndex: id => ids.indexOf(id.getString()) };
const physicsBuffer = read(physicsPath);
const outputs = JSON.parse(fs.readFileSync(physicsPath, 'utf8')).PhysicsSettings
  .flatMap(setting => setting.Output.map(o => o.Destination.Id));

const FPS = 60;
const SCENARIOS = [
  ['AngleX 20°', 'ParamAngleX', 20], ['AngleY 15°', 'ParamAngleY', 15], ['AngleZ 15°', 'ParamAngleZ', 15],
  ['BodyAngleX 6°', 'ParamBodyAngleX', 6],
];

function run(param, amount) {
  const physics = CubismPhysics.create(physicsBuffer, physicsBuffer.byteLength);
  for (let i = 0; i < P.count; i += 1) P.values[i] = P.defaultValues[i];
  physics.stabilization(mock);
  const series = Object.fromEntries(outputs.map(id => [id, []]));
  for (let f = 0; f < 5 * FPS; f += 1) {
    const t = f / FPS;
    const ramp = x => { const u = Math.min(1, Math.max(0, x)); return u * u * (3 - 2 * u); };
    P.values[index(param)] = amount * (ramp((t - 0.5) / 0.4) - ramp((t - 1.9) / 0.4));
    physics.evaluate(mock, 1 / FPS);
    for (const id of outputs) series[id].push(P.values[index(id)]);
  }
  return series;
}

if (rowsPath) {
  const rows = JSON.parse(fs.readFileSync(rowsPath, 'utf8'));
  const physics = CubismPhysics.create(physicsBuffer, physicsBuffer.byteLength);
  for (let i = 0; i < P.count; i += 1) P.values[i] = P.defaultValues[i];
  physics.stabilization(mock);
  const MAP = { headX: 'ParamAngleX', headY: 'ParamAngleY', headZ: 'ParamAngleZ', bodyX: 'ParamBodyAngleX', bodyY: 'ParamBodyAngleY', bodyZ: 'ParamBodyAngleZ' };
  const series = Object.fromEntries(outputs.map(id => [id, []]));
  for (const row of rows) {
    for (const [key, id] of Object.entries(MAP)) P.values[index(id)] = row[key];
    physics.evaluate(mock, 1 / 30);
    for (const id of outputs) series[id].push(P.values[index(id)]);
  }
  console.log(`idle 軌跡 ${(rows.length / 30).toFixed(0)}s`);
  for (const id of outputs) {
    const v = series[id];
    const rms = Math.sqrt(v.reduce((a, x) => a + x * x, 0) / v.length);
    const speed = v.slice(1).map((x, i) => Math.abs(x - v[i]) * 30);
    const still = speed.filter(x => x < 0.02).length / speed.length;
    const range = ids[index(id)] && [P.minimumValues[index(id)], P.maximumValues[index(id)]];
    console.log(`  ${id.padEnd(18)} 範圍 ±${range[1]} | RMS ${rms.toFixed(3)} 峰值 ${Math.max(...v.map(Math.abs)).toFixed(2)} | 速度中位 ${[...speed].sort((a, b) => a - b)[speed.length >> 1].toFixed(3)}/s | 近乎靜止(<0.02/s) ${(still * 100).toFixed(0)}%`);
  }
  process.exit(0);
}
for (const [label, param, amount] of SCENARIOS) {
  const series = run(param, amount);
  console.log(`\n${label}`);
  for (const id of outputs) {
    const values = series[id];
    const peak = Math.max(...values.map(Math.abs));
    if (peak < 0.02) continue;
    const stopAt = Math.round(2.3 * FPS);
    const after = values.slice(stopAt);
    const peakAt = values.findIndex(v => Math.abs(v) === peak);
    const sign = Math.sign(values[peakAt]);
    const swing = Math.max(...after.map(v => v * sign), 0);
    const counter = Math.max(...after.map(v => -v * sign), 0);
    let reversals = 0;
    for (let i = 1; i < after.length; i += 1) {
      if (after[i - 1] * after[i] < 0 && Math.max(Math.abs(after[i - 1]), Math.abs(after[i])) > 0) reversals += 1;
    }
    const ringing = after.filter(v => Math.abs(v) > 0.1 * peak).length ? reversals : 0;
    const lastAbove = after.reduce((last, v, i) => (Math.abs(v) > 0.05 * peak ? i : last), -1);
    console.log(`  ${id.padEnd(18)} 峰值 ${peak.toFixed(2).padStart(6)} | 停後餘擺 ${(swing / peak * 100).toFixed(0).padStart(3)}% 反擺 ${(counter / peak * 100).toFixed(0).padStart(3)}% | 反向 ${ringing} | 停後安定 ${((lastAbove + 1) / FPS).toFixed(2)}s`);
  }
}
