declare module '@framework/cubismmodelsettingjson' {
  export class CubismModelSettingJson {
    [key: string]: any
    constructor(...args: any[])
  }
}

declare module '@framework/model/cubismusermodel' {
  export class CubismUserModel {
    [key: string]: any
    constructor(...args: any[])
  }
}

declare module '@framework/math/cubismmatrix44' {
  export class CubismMatrix44 {
    [key: string]: any
    constructor(...args: any[])
  }
}

declare module '@framework/math/cubismmodelmatrix' {
  export class CubismModelMatrix {
    [key: string]: any
    constructor(...args: any[])
  }
}

declare module '@framework/cubismdefaultparameterid' {
  export const CubismDefaultParameterId: any
}

declare module '@framework/id/cubismid' {
  export type CubismIdHandle = any
}

declare module '@framework/live2dcubismframework' {
  export const CubismFramework: any
  export class Option {
    [key: string]: any
  }
  export const LogLevel: any
}

declare module '@framework/effect/cubismeyeblink' {
  export class CubismEyeBlink {
    [key: string]: any
    static create(...args: any[]): any
  }
}

declare module '@framework/effect/cubismbreath' {
  export type BreathParameterData = any
  export class CubismBreath {
    [key: string]: any
    static create(...args: any[]): any
  }
}

declare module '@framework/motion/cubismmotionqueuemanager' {
  export type CubismMotionQueueEntryHandle = any
  export const InvalidMotionQueueEntryHandleValue: any
}

declare module '@framework/motion/acubismmotion' {
  export class ACubismMotion {
    [key: string]: any
    constructor(...args: any[])
  }
}

declare module '@framework/motion/cubismmotion' {
  export class CubismMotion {
    [key: string]: any
    constructor(...args: any[])
    static create(...args: any[]): any
  }
}

declare module '@framework/math/cubismviewmatrix' {
  export class CubismViewMatrix {
    [key: string]: any
    constructor(...args: any[])
  }
}
