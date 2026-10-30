// 以 Python 產生的 ambient_plan 序列（gen-plans.py）餵給 AmbientMotion + 真實 ParameterMixer，
// 以 60fps 模擬 10 分鐘並輸出頭／身／視線的動態統計（靜止佔比、掃視 90% 到位時間、自相關…）。
// 用法：node scripts/idle-motion/measure.mjs <plans.json> [noiseSeed] [srcRoot]
//   DUMP=rows.json 可另存 30Hz 的輸出角度，供 z-dwell.py 等後處理。
//   srcRoot 預設為 vtuber-web-app/src，可指向舊版快照做前後對照。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const ts = createRequire(path.join(appRoot, 'package.json'))('typescript');
const [plansFile, seedArg, rootArg] = process.argv.slice(2);
const root = path.resolve(rootArg ?? path.join(appRoot, 'src'));
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
const plans = JSON.parse(fs.readFileSync(plansFile, 'utf8'));
// 與 rig 範圍一致的假 Core（AngleX ±45、AngleY/Z ±30、Body ±10、EyeBall ±1）。
const ids = ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX', 'ParamBodyAngleY', 'ParamBodyAngleZ', 'ParamEyeBallX', 'ParamEyeBallY'];
const range = { ParamAngleX: 45, ParamAngleY: 30, ParamAngleZ: 30, ParamBodyAngleX: 10, ParamBodyAngleY: 10, ParamBodyAngleZ: 10, ParamEyeBallX: 1, ParamEyeBallY: 1 };
const vals = new Map(ids.map(i => [i, 0]));
const core = { getParameterCount: () => ids.length, getParameterId: i => ({ getString: () => ids[i] }), getParameterDefaultValue: () => 0,
  getParameterMinimumValue: i => -range[ids[i]], getParameterMaximumValue: i => range[ids[i]],
  getParameterValueByIndex: i => vals.get(ids[i]), setParameterValueByIndex: (i, v) => vals.set(ids[i], v) };
const mixer = new ParameterMixer(core);
const m = seedArg === undefined ? new AmbientMotion() : new AmbientMotion(Number(seedArg));
const fps = 60, dt = 1 / fps, total = 600;
const MAP = { headX: 'ParamAngleX', headY: 'ParamAngleY', headZ: 'ParamAngleZ', bodyX: 'ParamBodyAngleX', bodyY: 'ParamBodyAngleY', bodyZ: 'ParamBodyAngleZ', gazeX: 'ParamEyeBallX', gazeY: 'ParamEyeBallY' };
const rows = [];
let pi = 0;
const gazeEvents = []; // 每次視線目標變更的時間與前後輸出
let lastGaze = [0, 0];
for (let f = 0; f < total * fps; f += 1) {
  const now = f * 1000 / fps;
  while (pi < plans.length && plans[pi].t * 1000 <= now) {
    const p = plans[pi].plan;
    const before = rows.length ? [rows.at(-1).gazeX, rows.at(-1).gazeY] : [0, 0];
    if (m.apply(p, now) && (p.target.gazeX !== lastGaze[0] || p.target.gazeY !== lastGaze[1])) { gazeEvents.push({ i: rows.length, before, tx: p.target.gazeX, ty: p.target.gazeY }); lastGaze = [p.target.gazeX, p.target.gazeY]; }
    pi += 1;
  }
  const s = m.update(dt, now, true);
  const row = {};
  for (const [k, id] of Object.entries(MAP)) row[k] = mixer.write(id, mixer.normalized(id, s[k]), dt);
  rows.push(row);
}
const speed = (k, i) => Math.abs(rows[i][k] - rows[i - 1][k]) * fps;
const head = rows.slice(1).map((_, i) => Math.hypot(speed('headX', i + 1), speed('headY', i + 1), speed('headZ', i + 1)));
const body = rows.slice(1).map((_, i) => Math.hypot(speed('bodyX', i + 1), speed('bodyY', i + 1), speed('bodyZ', i + 1)));
const spans = (v, th, min) => { let run = 0; const out = []; for (const x of v) { if (x < th) run += 1; else { if (run / fps >= min) out.push(run / fps); run = 0; } } if (run / fps >= min) out.push(run / fps); return out; };
const stillSpans = spans(head, 1.0, 0.4);
const pct = (v, th) => (100 * v.filter(x => x < th).length / v.length).toFixed(1);
const gaze = rows.slice(1).map((_, i) => Math.hypot(speed('gazeX', i + 1) / 0.85, speed('gazeY', i + 1)));
const exactCenter = rows.filter(r => Math.abs(r.gazeX) < 1e-3 && Math.abs(r.gazeY) < 1e-3).length / rows.length;
// 掃視：目標變更後，輸出走完 90% 變化量所需時間（ms）；只取變化量 > 0.2 的
const t90 = [];
for (const e of gazeEvents) {
  const after = rows[e.i + 24]; if (!after) continue; // 終點取 400ms 後，排除微掃視正在進行的瞬間
  const dx = after.gazeX - e.before[0], dy = after.gazeY - e.before[1];
  if (Math.hypot(dx / 0.85, dy) < 0.4) continue;
  for (let k = 0; k < 24; k += 1) {
    const r = rows[e.i + k];
    const prog = ((r.gazeX - e.before[0]) * dx + (r.gazeY - e.before[1]) * dy) / (dx * dx + dy * dy);
    if (prog >= 0.9) { t90.push(k * 1000 / fps); break; }
  }
}
t90.sort((a, b) => a - b);
// 自相關：頭部 X 角度，找 2–60 秒的最大峰
const series = rows.filter((_, i) => i % 6 === 0).map(r => r.headX); // 10 Hz
const mean = series.reduce((a, b) => a + b, 0) / series.length;
const dev = series.map(x => x - mean); const varr = dev.reduce((a, b) => a + b * b, 0);
let best = { lag: 0, r: -1 };
for (let lag = 20; lag <= 600; lag += 1) { let s = 0; for (let i = 0; i + lag < dev.length; i += 1) s += dev[i] * dev[i + lag]; const r = s / varr; if (r > best.r) best = { lag: lag / 10, r }; }
const rng = k => [Math.min(...rows.map(r => r[k])).toFixed(1), Math.max(...rows.map(r => r[k])).toFixed(1)];
const rms = k => Math.sqrt(rows.reduce((a, r) => a + r[k] * r[k], 0) / rows.length).toFixed(2);
const maxHeadAcc = Math.max(...head.slice(1).map((v, i) => Math.abs(v - head[i]) * fps));
console.log(JSON.stringify({
  head_lt1_pct: pct(head, 1.0), head_lt2_5_pct: pct(head, 2.5), body_lt0_5_pct: pct(body, 0.5),
  head_still_spans_ge0_4s: stillSpans.length, longest_head_still_s: (Math.max(0, ...stillSpans)).toFixed(2),
  gaze_still_pct: pct(gaze, 0.05), gaze_exact_center_pct: (exactCenter * 100).toFixed(1),
  saccade_t90_ms: { n: t90.length, median: t90[Math.floor(t90.length / 2)], p90: t90[Math.floor(t90.length * 0.9)] },
  headX_autocorr_peak: best, range_deg: { headX: rng('headX'), headY: rng('headY'), headZ: rng('headZ'), bodyX: rng('bodyX') },
  rms_deg: { headX: rms('headX'), headY: rms('headY'), headZ: rms('headZ'), bodyX: rms('bodyX') },
  head_speed_median: [...head].sort((a, b) => a - b)[Math.floor(head.length / 2)].toFixed(2), max_head_speed: Math.max(...head).toFixed(0),
}));
fs.writeFileSync(process.env.DUMP || '/dev/null', JSON.stringify(rows.filter((_, i) => i % 2 === 0)));
