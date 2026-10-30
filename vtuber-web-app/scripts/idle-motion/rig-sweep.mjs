// 用 Cubism Core 掃描 moc3：每個參數推到最小／最大值，列出受影響的 drawable 與最大位移（px）／opacity 變化。
// 用法：node scripts/idle-motion/rig-sweep.mjs [moc3 路徑]（預設 Rushia）；模型改版前後各跑一次比對。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const coreSrc = fs.readFileSync(path.join(appRoot, 'public/Core/live2dcubismcore.js'), 'utf8');
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const Core = new Function('require', 'process', '__dirname', coreSrc + '\nreturn Live2DCubismCore;')(require, process, '/tmp');
// wait for wasm init if needed
for (let i = 0; i < 200 && !(Core.Moc && Core.Moc.fromArrayBuffer); i++) await new Promise(r => setTimeout(r, 10));
await new Promise(r => setTimeout(r, 300));
const buf = fs.readFileSync(path.resolve(process.argv[2] ?? path.join(appRoot, 'public/Resources/Rushia/RushiaHD.moc3')));
const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
const moc = Core.Moc.fromArrayBuffer(ab);
const model = Core.Model.fromMoc(moc);
const P = model.parameters, D = model.drawables;
const ids = Array.from(P.ids), dIds = Array.from(D.ids);
const snap = () => { model.update(); return Array.from({ length: D.count }, (_, i) => Float32Array.from(D.vertexPositions[i])); };
const opac = () => Array.from(D.opacities);
for (let i = 0; i < P.count; i++) P.values[i] = P.defaultValues[i];
const base = snap(), baseOp = opac();
const ppu = model.canvasinfo.PixelsPerUnit;
const res = [];
const touched = new Set();
for (let i = 0; i < P.count; i++) {
  const per = new Map();
  let opChange = 0;
  for (const v of [P.minimumValues[i], P.maximumValues[i]]) {
    for (let j = 0; j < P.count; j++) P.values[j] = P.defaultValues[j];
    P.values[i] = v;
    const s = snap(), op = opac();
    for (let d = 0; d < D.count; d++) {
      let m = 0;
      for (let k = 0; k < s[d].length; k++) m = Math.max(m, Math.abs(s[d][k] - base[d][k]));
      m *= ppu;
      if (m > 0.5) per.set(dIds[d], Math.max(per.get(dIds[d]) || 0, m));
      opChange = Math.max(opChange, Math.abs(op[d] - baseOp[d]));
    }
  }
  for (const k of per.keys()) touched.add(k);
  const top = [...per.entries()].sort((a, b) => b[1] - a[1]);
  res.push({ id: ids[i], min: P.minimumValues[i], max: P.maximumValues[i], def: P.defaultValues[i], drawables: per.size,
    maxPx: top.length ? top[0][1].toFixed(1) : '0', opacity: opChange.toFixed(2), top: top.slice(0, 4).map(([k, v]) => `${k.replace('ArtMesh', '')}:${v.toFixed(0)}`).join(' ') });
}
console.log('canvas px', model.canvasinfo.CanvasWidth, 'x', model.canvasinfo.CanvasHeight, 'drawables', D.count, 'params', P.count, 'parts', model.parts.count);
for (const r of res) console.log(`${r.id.padEnd(20)} [${r.min},${r.max}] d=${r.def} | drawables=${String(r.drawables).padStart(2)} maxPx=${r.maxPx.padStart(6)} op=${r.opacity} | ${r.top}`);
console.log('\nNEVER deformed by any param:', dIds.filter(d => !touched.has(d)).join(', '));
