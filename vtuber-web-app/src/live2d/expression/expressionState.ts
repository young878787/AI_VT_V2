import type { BasePoseParams } from './expressionParams';

export function createNeutralTargetParams(): BasePoseParams {
  return {
    headIntensity: 0,
    bodyAngleX: 0,
    bodyAngleY: 0,
    bodyAngleZ: 0,
    breathLevel: 0.35,
    physicsImpulse: 0,
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
  };
}

export function copyBasePoseParams(params: BasePoseParams): BasePoseParams {
  return { ...params };
}
