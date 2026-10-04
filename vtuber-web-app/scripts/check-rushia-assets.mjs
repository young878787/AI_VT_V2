import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 模型素材未納入 Git；此驗收必須明確執行，缺檔時直接失敗。
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const assetDirectory = path.join(root, 'public/Resources/Rushia');
const manifest = JSON.parse(fs.readFileSync(path.join(assetDirectory, 'RushiaHD.model3.json'), 'utf8'));
const references = manifest.FileReferences;
for (const asset of [
  references.Moc, references.Physics, ...references.Textures,
  ...references.Expressions.map(expression => expression.File),
  ...Object.values(references.Motions).flat().map(motion => motion.File),
]) assert.ok(fs.existsSync(path.join(assetDirectory, asset)), `missing Rushia resource: ${asset}`);

console.log('Rushia asset checks passed: manifest and all referenced model resources exist.');
