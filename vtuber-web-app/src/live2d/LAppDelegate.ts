/**
 * Live2D 應用程式委託
 * 統籌管理整個 Live2D 系統的初始化、更新和渲染
 */

import { CubismFramework, Option, LogLevel } from '@framework/live2dcubismframework';
import { CubismMatrix44 } from '@framework/math/cubismmatrix44';
import { LAppView } from './LAppView';
import { LAppPal } from './LAppPal';
import { LAppLive2DManager } from './LAppLive2DManager';
import { DebugSettings } from './LAppDefine';

/**
 * 應用程式委託類
 */
export class LAppDelegate {
  private static s_instance: LAppDelegate | null = null;

  private _canvas: HTMLCanvasElement | null = null;
  private _gl: WebGLRenderingContext | WebGL2RenderingContext | null = null;
  private _view: LAppView | null = null;
  private _isInitialized = false;
  private _isRunning = false;
  private _animationId: number | null = null;
  private _frameBuffer: WebGLFramebuffer | null = null;

  /** 每幀渲染完成後的回呼，由 canvasSyncService 注入，用於零延遲畫面擷取 */
  private _postRenderCallback: ((canvas: HTMLCanvasElement) => void) | null = null;

  // 拖移狀態
  private _isDraggingModel = false;
  private _lastDragX = 0;
  private _lastDragY = 0;

  /**
   * 取得單例實例
   */
  public static getInstance(): LAppDelegate {
    if (!this.s_instance) {
      this.s_instance = new LAppDelegate();
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
    LAppPal.printLog('LAppDelegate 已創建');
  }

  /**
   * 初始化應用程式
   */
  public async initialize(canvas: HTMLCanvasElement): Promise<boolean> {
    LAppPal.printLog('開始初始化 LAppDelegate...');

    if (this._isInitialized) {
      LAppPal.printWarning('LAppDelegate 已經初始化');
      return true;
    }

    if (!canvas) {
      LAppPal.printError('Canvas 元素為 null');
      return false;
    }

    this._canvas = canvas;

    // 1. 檢查 Live2DCubismCore 是否已載入
    if (typeof (window as any).Live2DCubismCore === 'undefined') {
      LAppPal.printError('Live2DCubismCore 尚未載入');
      return false;
    }

    const core = (window as any).Live2DCubismCore;
    const coreVersion = core.Version;
    LAppPal.printLog(`Live2DCubismCore 已載入，版本: ${coreVersion?.versionNumber || 'Unknown'}`);

    // 檢查 Core 的關鍵 API
    if (!core.Moc || !core.Model) {
      LAppPal.printError('Live2DCubismCore API 不完整：缺少 Moc 或 Model');
      return false;
    }

    if (typeof core.Moc.fromArrayBuffer !== 'function') {
      LAppPal.printError('Live2DCubismCore.Moc.fromArrayBuffer 方法不存在');
      return false;
    }

    LAppPal.printLog('✓ Live2DCubismCore API 完整');

    // 2. 創建 WebGL 上下文
    // preserveDrawingBuffer: true 讓 canvas.toBlob() / toDataURL() 可正確讀取畫面內容
    // 這是 /display 鏡像串流的必要條件
    LAppPal.printLog('步驟 2: 開始創建 WebGL 上下文...');
    const gl = canvas.getContext('webgl2', {
      alpha: true,
      premultipliedAlpha: true,
      preserveDrawingBuffer: true,
    }) || canvas.getContext('webgl', {
      alpha: true,
      premultipliedAlpha: true,
      preserveDrawingBuffer: true,
    });

    if (!gl) {
      LAppPal.printError('無法創建 WebGL 上下文');
      return false;
    }

    this._gl = gl as WebGL2RenderingContext;

    // 保存當前的 framebuffer binding
    this._frameBuffer = gl.getParameter(gl.FRAMEBUFFER_BINDING);

    LAppPal.printLog('✓ WebGL 上下文已創建');

    // 3. 初始化 Cubism Framework
    LAppPal.printLog('步驟 3: 開始初始化 Cubism Framework...');
    LAppPal.printLog('步驟 3: 開始初始化 Cubism Framework...');
    try {
      LAppPal.printLog('3.1: 創建 Option 對象...');
      const cubismOption = new Option();
      cubismOption.logFunction = (message: string) => {
        if (DebugSettings.LogEnable) {
          LAppPal.printLog(message);
        }
      };
      cubismOption.loggingLevel = DebugSettings.LogEnable
        ? LogLevel.LogLevel_Verbose
        : LogLevel.LogLevel_Error;

      LAppPal.printLog('3.2: 調用 CubismFramework.startUp...');
      CubismFramework.startUp(cubismOption);
      LAppPal.printLog('3.3: 調用 CubismFramework.initialize...');
      CubismFramework.initialize();
      LAppPal.printLog('✓ Cubism Framework 已初始化');
    } catch (error) {
      LAppPal.printError(`Cubism Framework 初始化失敗：${error}`);
      return false;
    }

    // 4. 初始化視圖
    LAppPal.printLog('步驟 4: 開始初始化 LAppView...');
    LAppPal.printLog('步驟 4: 開始初始化 LAppView...');
    try {
      LAppPal.printLog('4.1: 創建 LAppView 實例...');
      this._view = new LAppView();
      if (!this._view) {
        LAppPal.printError('LAppView 創建失敗');
        return false;
      }
      LAppPal.printLog('4.2: 調用 _view.initialize...');
      this._view.initialize(this._gl);
      LAppPal.printLog('✓ LAppView 已初始化');
    } catch (error) {
      LAppPal.printError(`LAppView 初始化失敗：${error}`);
      return false;
    }

    // 5. 設置 Canvas 尺寸
    LAppPal.printLog('步驟 5: 開始設置 Canvas 尺寸...');
    try {
      this.resizeCanvas();
      LAppPal.printLog('✓ Canvas 尺寸已設置');
    } catch (error) {
      LAppPal.printError(`Canvas 尺寸設置失敗：${error}`);
      return false;
    }

    // 6. 設置模型管理器的 GL 上下文
    LAppPal.printLog('步驟 6: 開始設置模型管理器...');
    try {
      const modelManager = LAppLive2DManager.getInstance();
      if (!modelManager) {
        LAppPal.printError('模型管理器獲取失敗');
        return false;
      }
      modelManager.setGLContext(this._gl);
      LAppPal.printLog('✓ 模型管理器 GL 上下文已設置');
    } catch (error) {
      LAppPal.printError(`模型管理器設置失敗：${error}`);
      return false;
    }

    // 7. 載入預設模型
    LAppPal.printLog('步驟 7: 開始載入預設模型...');
    try {
      const modelManager = LAppLive2DManager.getInstance();
      await modelManager.loadDefaultModel();
      LAppPal.printLog('✓ 預設模型載入成功');

      // 設定模型的 canvas 尺寸
      const model = modelManager.getActiveModel();
      if (model && this._canvas) {
        model.setCanvasSize(this._canvas.width, this._canvas.height);
        LAppPal.printLog(`✓ 模型 canvas 尺寸已設置：${this._canvas.width}x${this._canvas.height}`);
      }
    } catch (error) {
      LAppPal.printError(`預設模型載入失敗：${error}`);
      // 即使模型載入失敗，也標記為已初始化（允許稍後重試載入模型）
      this._isInitialized = true;
      return false;
    }

    this._isInitialized = true;
    LAppPal.printLog('========================================');
    LAppPal.printLog('✓✓✓ LAppDelegate 初始化完全成功 ✓✓✓');
    LAppPal.printLog('========================================');

    return true;
  }

  /**
   * 調整 Canvas 尺寸
   */
  public resizeCanvas(): void {
    if (!this._canvas || !this._view) return;

    const displayWidth = this._canvas.clientWidth;
    const displayHeight = this._canvas.clientHeight;

    LAppPal.printLog(`Canvas 客戶端尺寸：${displayWidth}x${displayHeight}`);

    if (displayWidth === 0 || displayHeight === 0) {
      LAppPal.printWarning('Canvas 尺寸為 0，可能尚未渲染或 CSS 設置有問題');
    }

    if (this._canvas.width !== displayWidth || this._canvas.height !== displayHeight) {
      this._canvas.width = displayWidth;
      this._canvas.height = displayHeight;
      this._view.resize(displayWidth, displayHeight);
      LAppPal.printLog(`✓ Canvas 尺寸已調整：${displayWidth}x${displayHeight}`);
    }
  }

  /**
   * 啟動渲染循環
   */
  public run(): void {
    if (!this._isInitialized) {
      LAppPal.printError('請先初始化應用程式');
      return;
    }

    if (this._isRunning) {
      LAppPal.printWarning('渲染循環已在運行');
      return;
    }

    if (!this._view) {
      LAppPal.printError('LAppView 為 null，無法啟動渲染循環');
      return;
    }

    this._isRunning = true;
    LAppPal.printLog('✓ 渲染循環已啟動');

    const loop = (): void => {
      if (!this._isRunning) return;

      // 更新時間
      LAppPal.updateTime();

      // 渲染
      if (this._view) {
        this._view.render();
      }

      // 渲染完成後立即擷取畫面（canvasSyncService 注入的 hook，零延遲）
      if (this._postRenderCallback && this._canvas) {
        this._postRenderCallback(this._canvas);
      }

      // 請求下一幀
      this._animationId = requestAnimationFrame(loop);
    };

    loop();
  }

  /**
   * 停止渲染循環
   */
  public stop(): void {
    if (this._animationId !== null) {
      cancelAnimationFrame(this._animationId);
      this._animationId = null;
    }
    this._isRunning = false;
    LAppPal.printLog('渲染循環已停止');
  }

  /**
   * 載入模型（透過名稱）
   */
  public async loadModel(modelName: string): Promise<boolean> {
    try {
      const modelManager = LAppLive2DManager.getInstance();
      await modelManager.loadModelByName(modelName);
      LAppPal.printLog(`模型載入成功：${modelName}`);
      return true;
    } catch (error) {
      LAppPal.printError(`模型載入失敗：${error}`);
      return false;
    }
  }

  /**
   * 切換模型
   */
  public switchModel(modelName: string): boolean {
    const modelManager = LAppLive2DManager.getInstance();
    return modelManager.switchModel(modelName);
  }

  /**
   * 取得當前模型
   */
  public getActiveModel() {
    const modelManager = LAppLive2DManager.getInstance();
    return modelManager.getActiveModel();
  }

  /**

   * 處理滑鼠移動（用於視線追蹤）
   */
  public onMouseMoved(x: number, y: number): void {
    if (!this._canvas) return;

    const model = this.getActiveModel();
    if (!model) return;

    // 將螢幕座標轉換為 -1 到 1 的範圍
    const rect = this._canvas.getBoundingClientRect();
    let normalizedX = ((x - rect.left) / rect.width) * 2 - 1;
    let normalizedY = -(((y - rect.top) / rect.height) * 2 - 1);

    // 限制追蹤範圍，避免過度轉動（只追蹤畫布範圍內）
    normalizedX = Math.max(-1.0, Math.min(1.0, normalizedX));
    normalizedY = Math.max(-1.0, Math.min(1.0, normalizedY));

    // 使用 CubismUserModel 的 setDragging 方法實現視線追蹤
    // 這會影響模型的頭部和眼睛方向
    model.setDragging(normalizedX, normalizedY);
  }

  /**
   * 取得相機投影與視圖合併的矩陣 (Projection * View)
   * 這是用來將剪裁空間座標 (Clip) 轉回 Local View (用於 hitTest)
   */
  public createProjViewMatrix(): CubismMatrix44 | null {
    if (!this._canvas) return null;
    const model = this.getActiveModel();
    if (!model || !model.getModel()) return null;

    const projection = new CubismMatrix44();
    const rect = this._canvas.getBoundingClientRect();

    // 1. Aspect Ratio Projection
    if (rect.width > 0 && rect.height > 0) {
      if (model.getModel().getCanvasWidth() > 1.0 && rect.width < rect.height) {
        projection.scale(1.0, rect.width / rect.height);
      } else {
        projection.scale(rect.height / rect.width, 1.0);
      }
    }

    // 2. View Matrix
    const viewMatrix = this._view?.getViewMatrix();
    if (viewMatrix) {
      projection.multiplyByMatrix(viewMatrix);
    }

    return projection;
  }

  /**
   * 取得當下的完整矩陣 (Projection * View * Model)
   */
  public createMVPMatrix(): CubismMatrix44 | null {
    const projView = this.createProjViewMatrix();
    if (!projView) return null;

    // 3. 加上 Model Matrix
    const model = this.getActiveModel();
    if (model && model.getModel()) {
      projView.multiplyByMatrix(model.getModelMatrix());
    }
    return projView;
  }

  /**
   * 處理點擊事件（改為智能檢測）
   */
  public onTapped(x: number, y: number): void {
    if (!this._canvas) return;

    const model = this.getActiveModel();
    if (!model) return;

    // 將螢幕座標轉換為 GL 剪裁空間 (-1 到 +1)
    const rect = this._canvas.getBoundingClientRect();
    const clipX = ((x - rect.left) / rect.width) * 2.0 - 1.0;
    const clipY = -(((y - rect.top) / rect.height) * 2.0 - 1.0); // WebGL Y 軸向上

    // 只套用 Inverse(Proj * View)，因為 model.hitTest 內建會使用 Inverse(ModelMatrix)
    const projView = this.createProjViewMatrix();
    if (!projView) return;
    const inverseProjView = projView.getInvert();
    const viewX = inverseProjView.transformX(clipX);
    const viewY = inverseProjView.transformY(clipY);

    console.log('[Click Debug]');
    console.log('  Screen:', x.toFixed(1), y.toFixed(1));
    console.log('  LocalView:', viewX.toFixed(3), viewY.toFixed(3));

    // 傳入 View 座標，model 內部會自動再進行 Model 座標轉換並碰撞檢測
    if (model.hitTest(viewX, viewY)) {
      console.log('✓ 點擊在模型區域，開始拖移');
      this.startModelDrag(x, y);
      return;
    } else {
      console.log('✗ 點擊在模型外');
    }
  }

  /**
   * 開始拖移模型
   */
  public startModelDrag(x: number, y: number): void {
    this._isDraggingModel = true;
    this._lastDragX = x;
    this._lastDragY = y;
    LAppPal.printLog('開始拖移模型');
  }

  /**
   * 更新拖移位置
   */
  public updateModelDrag(x: number, y: number): void {
    if (!this._isDraggingModel || !this._canvas) return;

    const model = this.getActiveModel();
    if (!model) return;

    // 計算移動距離（螢幕座標轉換為模型座標）
    // 因為 WebGL 投影矩陣的高預設對應 -1 到 1（跨度 2.0）
    // 所以寬高的位移都統一除以 rect.height 並乘上 2.0，能達成完美的 1:1 拖移手感
    // 這修復了以前 (x/width * 4.0) 造成左右移動過於靈敏的問題
    const rect = this._canvas.getBoundingClientRect();
    const deltaX = (x - this._lastDragX) / rect.height * 2.0;
    const deltaY = -(y - this._lastDragY) / rect.height * 2.0;

    model.moveModel(deltaX, deltaY);

    this._lastDragX = x;
    this._lastDragY = y;
  }

  /**
   * 結束拖移
   */
  public endModelDrag(): void {
    this._isDraggingModel = false;
  }

  /**
   * 取得 WebGL 上下文
   */
  public getGL(): WebGLRenderingContext | WebGL2RenderingContext | null {
    return this._gl;
  }

  /**
   * 取得 FrameBuffer
   */
  public getFrameBuffer(): WebGLFramebuffer | null {
    return this._frameBuffer;
  }

  /**
   * 取得 Canvas
   */
  public getCanvas(): HTMLCanvasElement | null {
    return this._canvas;
  }

  /**
   * 取得視圖
   */
  public getView(): LAppView | null {
    return this._view;
  }

  /**
   * 檢查是否已初始化
   */
  public isInitialized(): boolean {
    return this._isInitialized;
  }

  /**
   * 檢查是否正在運行
   */
  public isRunning(): boolean {
    return this._isRunning;
  }

  /**
   * 設定渲染後回呼（供 canvasSyncService hook 進渲染迴圈，實現零延遲畫面擷取）
   * 傳入 null 可清除回呼。
   */
  public setPostRenderCallback(cb: ((canvas: HTMLCanvasElement) => void) | null): void {
    this._postRenderCallback = cb;
  }

  /**
   * 釋放所有資源
   */
  public release(): void {
    LAppPal.printLog('開始釋放 LAppDelegate...');

    // 清除渲染後回呼
    this._postRenderCallback = null;

    // 停止渲染循環
    this.stop();

    // 釋放模型管理器
    LAppLive2DManager.releaseInstance();

    // 釋放視圖
    if (this._view) {
      this._view.release();
      this._view = null;
    }

    // 釋放 WebGL 上下文
    if (this._gl && this._canvas) {
      const loseContextExt = this._gl.getExtension('WEBGL_lose_context');
      if (loseContextExt) {
        loseContextExt.loseContext();
      }
    }
    this._gl = null;

    // 釋放 Cubism Framework
    CubismFramework.dispose();

    this._canvas = null;
    this._isInitialized = false;

    LAppPal.printLog('✓ LAppDelegate 已完全釋放');
  }
}
