/**
 * Rushia 模型管理器
 * 負責固定模型實例的載入、更新和銷毀。
 */

import type { CubismMatrix44 } from '@framework/math/cubismmatrix44';
import { LAppModel } from './LAppModel';
import { getDefaultModel, getModelPath, getModelJsonPath } from './LAppDefine';
import { LAppPal } from './LAppPal';

export class LAppLive2DManager {
  private static s_instance: LAppLive2DManager | null = null;

  private _activeModel: LAppModel | null = null;
  private _gl: WebGLRenderingContext | WebGL2RenderingContext | null = null;

  public static getInstance(): LAppLive2DManager {
    if (!this.s_instance) {
      this.s_instance = new LAppLive2DManager();
    }
    return this.s_instance;
  }

  public static releaseInstance(): void {
    if (this.s_instance) {
      this.s_instance.release();
    }
    this.s_instance = null;
  }

  private constructor() {
    LAppPal.printLog('LAppLive2DManager 已初始化');
  }

  public setGLContext(gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    this._gl = gl;
  }

  /** 載入固定的 Rushia 模型，失敗時直接回報資源路徑。 */
  public async loadDefaultModel(): Promise<LAppModel> {
    if (!this._gl) {
      throw new Error('WebGL 上下文尚未設置');
    }
    if (this._activeModel) {
      return this._activeModel;
    }

    const config = getDefaultModel();
    const model = new LAppModel();
    try {
      await model.loadAssets(getModelPath(config), config.fileName, config);
      model.setupRenderer(this._gl);
      await model.setupTextures();
      this._activeModel = model;
      LAppPal.printLog(`模型載入完成：${config.displayName}`);
      return model;
    } catch (error) {
      model.release();
      const detail = error instanceof Error ? error.message : String(error);
      const message = `Rushia 模型載入失敗（${getModelJsonPath(config)}）：${detail}`;
      LAppPal.printError(message);
      throw new Error(message);
    }
  }

  public getActiveModel(): LAppModel | null {
    return this._activeModel;
  }

  public update(): void {
    this._activeModel?.update();
  }

  public draw(matrix: CubismMatrix44): void {
    this._activeModel?.draw(matrix);
  }

  public release(): void {
    this._activeModel?.release();
    this._activeModel = null;
    this._gl = null;
    LAppPal.printLog('Rushia 模型已釋放');
  }
}
