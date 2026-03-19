/**
 * Live2D 模型管理器
 * 負責管理多個模型實例的創建、切換和銷毀
 */

import { LAppModel } from './LAppModel';
import type { ModelConfig } from './LAppDefine';
import { getDefaultModel, getModelConfig } from './LAppDefine';
import { LAppPal } from './LAppPal';

/**
 * 模型管理器類
 */
export class LAppLive2DManager {
  private static s_instance: LAppLive2DManager | null = null;

  private _models: Map<string, LAppModel> = new Map();
  private _activeModel: LAppModel | null = null;
  private _gl: WebGLRenderingContext | WebGL2RenderingContext | null = null;

  /**
   * 取得單例實例
   */
  public static getInstance(): LAppLive2DManager {
    if (!this.s_instance) {
      this.s_instance = new LAppLive2DManager();
    }
    return this.s_instance;
  }

  /**
   * 釋放單例實例
   */
  public static releaseInstance(): void {
    if (this.s_instance) {
      this.s_instance.release();
    }
    this.s_instance = null;
  }

  /**
   * 私有建構子（單例模式）
   */
  private constructor() {
    LAppPal.printLog('LAppLive2DManager 已初始化');
  }

  /**
   * 設置 WebGL 上下文
   */
  public setGLContext(gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    this._gl = gl;
  }

  /**
   * 載入模型
   * @param modelConfig 模型配置
   * @param setAsActive 是否設為當前活動模型
   */
  public async loadModel(
    modelConfig: ModelConfig,
    setAsActive: boolean = true
  ): Promise<LAppModel> {
    if (!this._gl) {
      throw new Error('WebGL 上下文尚未設置');
    }

    // 檢查模型是否已載入
    if (this._models.has(modelConfig.name)) {
      LAppPal.printLog(`模型已存在：${modelConfig.name}，返回現有實例`);
      const existingModel = this._models.get(modelConfig.name)!;
      
      if (setAsActive) {
        this._activeModel = existingModel;
      }
      
      return existingModel;
    }

    // 創建新模型
    LAppPal.printLog(`開始載入新模型：${modelConfig.displayName}`);
    const model = new LAppModel();

    try {
      // 載入模型資源
      const modelPath = `/Resources/${modelConfig.directory}/`;
      LAppPal.printLog(`7.1: 載入模型資源，路徑: ${modelPath}${modelConfig.fileName}`);
      await model.loadAssets(modelPath, modelConfig.fileName, modelConfig);
      LAppPal.printLog('7.2: 模型資源載入完成');
      
      // 初始化渲染器
      if (this._gl) {
        LAppPal.printLog('7.3: 設置 Renderer...');
        model.setupRenderer(this._gl);
        
        // 載入紋理（必須在 setupRenderer 之後）
        LAppPal.printLog('7.4: 載入紋理...');
        await model.setupTextures();
        LAppPal.printLog('7.5: 紋理載入完成');
      }
      
      // 儲存模型
      this._models.set(modelConfig.name, model);
      
      // 設為活動模型
      if (setAsActive || !this._activeModel) {
        this._activeModel = model;
        LAppPal.printLog(`當前活動模型：${modelConfig.displayName}`);
      }

      return model;
      
    } catch (error) {
      LAppPal.printError(`模型載入失敗：${error}`);
      throw error;
    }
  }

  /**
   * 載入預設模型
   */
  public async loadDefaultModel(): Promise<LAppModel> {
    const defaultConfig = getDefaultModel();
    return this.loadModel(defaultConfig);
  }

  /**
   * 根據名稱載入模型
   */
  public async loadModelByName(name: string): Promise<LAppModel> {
    const config = getModelConfig(name);
    if (!config) {
      throw new Error(`找不到模型配置：${name}`);
    }
    return this.loadModel(config);
  }

  /**
   * 切換當前活動模型
   */
  public switchModel(modelName: string): boolean {
    const model = this._models.get(modelName);
    if (!model) {
      LAppPal.printWarning(`模型不存在，無法切換：${modelName}`);
      return false;
    }

    this._activeModel = model;
    const config = model.getModelConfig();
    LAppPal.printLog(`已切換到模型：${config?.displayName || 'Unknown'}`);
    return true;
  }

  /**
   * 取得當前活動模型
   */
  public getActiveModel(): LAppModel | null {
    return this._activeModel;
  }

  /**
   * 取得指定模型
   */
  public getModel(name: string): LAppModel | null {
    return this._models.get(name) || null;
  }

  /**
   * 取得所有已載入的模型
   */
  public getAllModels(): LAppModel[] {
    return Array.from(this._models.values());
  }

  /**
   * 取得已載入模型的名稱列表
   */
  public getLoadedModelNames(): string[] {
    return Array.from(this._models.keys());
  }

  /**
   * 移除指定模型
   */
  public removeModel(name: string): boolean {
    const model = this._models.get(name);
    if (!model) {
      LAppPal.printWarning(`模型不存在，無法移除：${name}`);
      return false;
    }

    // 如果是當前活動模型，則清空
    if (this._activeModel === model) {
      this._activeModel = null;
      LAppPal.printLog('當前活動模型已被移除');
    }

    // 釋放模型資源
    model.release();
    this._models.delete(name);
    
    LAppPal.printLog(`模型已移除：${name}`);
    return true;
  }

  /**
   * 更新所有模型（或僅更新活動模型）
   */
  public update(updateAll: boolean = false): void {
    if (updateAll) {
      // 更新所有已載入的模型
      for (const model of this._models.values()) {
        model.update();
      }
    } else {
      // 僅更新當前活動模型
      if (this._activeModel) {
        this._activeModel.update();
      }
    }
  }

  /**
   * 繪製當前活動模型
   */
  public draw(matrix: any): void {
    if (this._activeModel) {
      this._activeModel.draw(matrix);
    }
  }

  /**
   * 繪製所有模型
   */
  public drawAll(matrix: any): void {
    for (const model of this._models.values()) {
      model.draw(matrix);
    }
  }

  /**
   * 檢查模型是否已載入
   */
  public isModelLoaded(name: string): boolean {
    return this._models.has(name);
  }

  /**
   * 取得已載入模型數量
   */
  public getModelCount(): number {
    return this._models.size;
  }

  /**
   * 釋放所有資源
   */
  public release(): void {
    LAppPal.printLog('開始釋放所有模型...');
    
    // 釋放所有模型
    for (const [name, model] of this._models.entries()) {
      LAppPal.printLog(`釋放模型：${name}`);
      model.release();
    }

    this._models.clear();
    this._activeModel = null;
    this._gl = null;

    LAppPal.printLog('所有模型已釋放');
  }
}
