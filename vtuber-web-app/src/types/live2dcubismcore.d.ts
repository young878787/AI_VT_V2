/**
 * Live2D Cubism Core 全局類型聲明
 * 確保 TypeScript 能正確識別全局的 Live2DCubismCore 對象
 */

/// <reference path="../../public/Core/live2dcubismcore.d.ts" />

declare global {
  interface Window {
    Live2DCubismCore: typeof Live2DCubismCore;
  }
}

export {};
