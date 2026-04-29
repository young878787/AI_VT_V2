import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import ts from 'typescript';

const rootDir = process.cwd();
const sourcePath = path.resolve(rootDir, 'src/types/expressionPlan.ts');
const sourceCode = fs.readFileSync(sourcePath, 'utf8');

const transpiled = ts.transpileModule(sourceCode, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: sourcePath,
}).outputText;

const module = { exports: {} };
const sandbox = {
  module,
  exports: module.exports,
  require: createRequire(import.meta.url),
  console,
  __filename: sourcePath,
  __dirname: path.dirname(sourcePath),
};

vm.runInNewContext(transpiled, sandbox, { filename: sourcePath });

const { isExpressionPlanPayload } = module.exports;

if (typeof isExpressionPlanPayload !== 'function') {
  throw new Error('isExpressionPlanPayload is not exported as a function');
}

const makeBasePose = () => ({
  preset: 'calm_soft',
  params: {
    headIntensity: 0.3,
    blushLevel: 0,
    eyeSync: true,
    eyeLOpen: 1,
    eyeROpen: 1,
    mouthForm: 0,
    browLY: 0,
    browRY: 0,
    browLAngle: 0,
    browRAngle: 0,
    browLForm: 0,
    browRForm: 0,
    eyeLSmile: 0,
    eyeRSmile: 0,
    browLX: 0,
    browRX: 0,
  },
  durationSec: 1.6,
});

const makePayload = (commands) => ({
  type: 'expression_plan',
  basePose: makeBasePose(),
  microEvents: [],
  sequence: [],
  blinkPlan: {
    style: 'normal',
    commands,
  },
  speakingRate: 1,
});

const validSetInterval = makePayload([
  { action: 'set_interval', intervalMin: 0.8, intervalMax: 1.5 },
]);
const missingMin = makePayload([
  { action: 'set_interval', intervalMax: 1.5 },
]);
const missingMax = makePayload([
  { action: 'set_interval', intervalMin: 0.8 },
]);
const reversedInterval = makePayload([
  { action: 'set_interval', intervalMin: 2.0, intervalMax: 1.5 },
]);
const forceBlinkWithoutDuration = makePayload([
  { action: 'force_blink' },
]);

if (!isExpressionPlanPayload(validSetInterval)) {
  throw new Error('Expected a complete set_interval command to be valid');
}
if (isExpressionPlanPayload(missingMin)) {
  throw new Error('Expected set_interval without intervalMin to be rejected');
}
if (isExpressionPlanPayload(missingMax)) {
  throw new Error('Expected set_interval without intervalMax to be rejected');
}
if (isExpressionPlanPayload(reversedInterval)) {
  throw new Error('Expected set_interval with inverted range to be rejected');
}
if (!isExpressionPlanPayload(forceBlinkWithoutDuration)) {
  throw new Error('Expected force_blink without durationSec to remain valid');
}

console.log('expressionPlan validator smoke test passed.');
