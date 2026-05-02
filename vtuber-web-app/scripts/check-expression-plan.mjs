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
    bodyAngleX: 0,
    bodyAngleY: 0,
    bodyAngleZ: 0,
    breathLevel: 0.35,
    physicsImpulse: 0.12,
  },
  bodyMotionProfile: {
    style: 'bright_bounce',
    speed: 1.2,
    swayScale: 1.25,
    bobScale: 1.15,
    twistScale: 0.9,
    breathScale: 1.1,
    headScale: 1.1,
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

const makeIdlePlan = () => ({
  name: 'happy_idle',
  mode: 'loop',
  enterAfterMs: 1800,
  loopIntervalMs: 2200,
  ambientEnterAfterMs: 11200,
  ambientSwitchIntervalMs: 6800,
  interruptible: true,
  settlePose: makeBasePose(),
  loopEvents: [
    {
      kind: 'happy_idle_warm_lift',
      durationMs: 680,
      patch: { mouthForm: 0.34, eyeLSmile: 0.56 },
      returnToBase: true,
    },
  ],
  ambientPlan: {
    states: [
      {
        kind: 'ambient_idle_breath',
        params: makeBasePose().params,
      },
      {
        kind: 'ambient_idle_look_around',
        params: makeBasePose().params,
      },
      {
        kind: 'ambient_idle_active_shift',
        params: makeBasePose().params,
      },
    ],
  },
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
const validMicroEventEnvelope = {
  ...makePayload([{ action: 'force_blink' }]),
  sequence: [
    {
      kind: 'bright_sway_left',
      durationMs: 900,
      fadeInMs: 180,
      fadeOutMs: 260,
      patch: {
        mouthForm: 0.42,
        eyeLSmile: 0.68,
        physicsImpulse: 0.92,
      },
      returnToBase: true,
    },
  ],
};
const invalidMicroEventEnvelope = {
  ...makePayload([{ action: 'force_blink' }]),
  sequence: [
    {
      kind: 'bright_sway_left',
      durationMs: 900,
      fadeInMs: -1,
      patch: {
        bodyAngleX: 0.58,
      },
      returnToBase: true,
    },
  ],
};
const validAmbientIdlePlan = {
  ...makePayload([{ action: 'force_blink' }]),
  idlePlan: makeIdlePlan(),
};
const reversedAmbientInterval = {
  ...makePayload([{ action: 'force_blink' }]),
  idlePlan: {
    ...makeIdlePlan(),
    ambientEnterAfterMs: 'nope',
  },
};
const reversedAmbientPlan = {
  ...makePayload([{ action: 'force_blink' }]),
  idlePlan: {
    ...makeIdlePlan(),
    ambientPlan: {
      states: [
        {
          kind: 'ambient_idle_breath',
          params: makeBasePose().params,
        },
        {
          kind: 'ambient_idle_look_around',
          params: makeBasePose().params,
        },
      ],
    },
  },
};
const invalidBodyMotionProfile = {
  ...makePayload([{ action: 'force_blink' }]),
  basePose: {
    ...makeBasePose(),
    bodyMotionProfile: {
      ...makeBasePose().bodyMotionProfile,
      style: 'wild_unknown',
    },
  },
};

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
if (!isExpressionPlanPayload(validMicroEventEnvelope)) {
  throw new Error('Expected micro event envelope fade fields to be valid');
}
if (isExpressionPlanPayload(invalidMicroEventEnvelope)) {
  throw new Error('Expected negative micro event fadeInMs to be rejected');
}
if (!isExpressionPlanPayload(validAmbientIdlePlan)) {
  throw new Error('Expected ambient idle plan with ambientPlan states to be valid');
}
if (isExpressionPlanPayload(reversedAmbientInterval)) {
  throw new Error('Expected invalid ambient enterAfterMs to be rejected');
}
if (isExpressionPlanPayload(reversedAmbientPlan)) {
  throw new Error('Expected ambient plan with too few states to be rejected');
}
if (isExpressionPlanPayload(invalidBodyMotionProfile)) {
  throw new Error('Expected unknown body motion profile style to be rejected');
}

console.log('expressionPlan validator smoke test passed.');
