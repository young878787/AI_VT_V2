/**
 * Rushia 模型管理器
 * 負責共用舞台模型的載入、切換、更新和銷毀。
 */

import type { CubismMatrix44 } from '@framework/math/cubismmatrix44';
import { LAppModel } from './LAppModel';
import { AvailableModels, getDefaultModel, getModelPath, getModelJsonPath } from './LAppDefine';
import { LAppPal } from './LAppPal';

export class LAppLive2DManager {
  private static s_instance: LAppLive2DManager | null = null;

  private _activeModel: LAppModel | null = null;
  private _loadingModel: LAppModel | null = null;
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

  public loadDefaultModel(): Promise<LAppModel> {
    return this.loadModel(getDefaultModel().directory);
  }

  /** 新資源完整載入後才替換舞台；失敗時保留原模型。 */
  public async loadModel(directory: string): Promise<LAppModel> {
    const config = AvailableModels.find(model => model.directory === directory);
    if (!config) {
      throw new Error('不支援的模型資源');
    }
    const gl = this._gl;
    if (!gl) {
      throw new Error('WebGL 上下文尚未設置');
    }
    if (this._loadingModel) {
      throw new Error('模型正在載入，請稍後再切換');
    }
    if (this._activeModel?.getModelConfig()?.directory === directory) {
      return this._activeModel;
    }

    const model = new LAppModel();
    this._loadingModel = model;
    try {
      await model.loadAssets(getModelPath(config), config.fileName, config);
      if (this._loadingModel !== model || this._gl !== gl) {
        throw new Error('模型載入已取消');
      }
      model.setupRenderer(gl);
      await model.setupTextures();
      if (this._loadingModel !== model || this._gl !== gl) {
        throw new Error('模型載入已取消');
      }

      const previous = this._activeModel;
      if (previous) {
        model.inheritAmbientBaseline(previous);
        model.setCanvasSize(previous.getCanvasWidth(), previous.getCanvasHeight());
        model.setModelScale(previous.getModelScale());
        const position = previous.getModelPosition();
        model.setModelPosition(position.x, position.y);
      }
      this._activeModel = model;
      this._loadingModel = null;
      previous?.release();
      LAppPal.printLog(`模型載入完成：${config.displayName}`);
      return model;
    } catch (error) {
      if (this._loadingModel === model) this._loadingModel = null;
      model.release();
      const detail = error instanceof Error ? error.message : String(error);
      const message = `${config.displayName} 模型載入失敗（${getModelJsonPath(config)}）：${detail}`;
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
    this._loadingModel = null;
    this._activeModel?.release();
    this._activeModel = null;
    this._gl = null;
    LAppPal.printLog('Rushia 模型已釋放');
  }
}
