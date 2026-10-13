import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 模型素材未納入 Git；此驗收必須明確執行，缺檔時直接失敗。
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifests = [];
const parameters = [];
for (const directory of ['Rushia', 'Rushia_hd']) {
  const assetDirectory = path.join(root, 'public/Resources', directory);
  const manifest = JSON.parse(fs.readFileSync(path.join(assetDirectory, 'RushiaHD.model3.json'), 'utf8'));
  const references = manifest.FileReferences;
  for (const asset of [
    references.Moc, references.Physics, references.DisplayInfo, ...references.Textures,
    ...references.Expressions.map(expression => expression.File),
    ...Object.values(references.Motions).flat().map(motion => motion.File),
  ]) assert.ok(fs.existsSync(path.join(assetDirectory, asset)), `missing ${directory} resource: ${asset}`);
  manifests.push(manifest);
  parameters.push(JSON.parse(fs.readFileSync(path.join(assetDirectory, references.DisplayInfo), 'utf8')).Parameters);
}
assert.deepEqual(manifests[0].Groups, manifests[1].Groups, 'both resources share eye blink and lip sync IDs');
const hdIds = new Set(parameters[1].map(parameter => parameter.Id));
for (const parameter of parameters[0]) assert.ok(hdIds.has(parameter.Id), `missing shared parameter: ${parameter.Id}`);

console.log('Rushia asset checks passed: both manifests, referenced resources and shared parameter IDs.');
