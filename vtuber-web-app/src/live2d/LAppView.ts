/**
 * Live2D 視圖管理類
 * 負責處理渲染邏輯、投影矩陣和視圖轉換
 */

import { CubismMatrix44 } from '@framework/math/cubismmatrix44';
import { CubismViewMatrix } from '@framework/math/cubismviewmatrix';
import { CanvasSettings } from './LAppDefine';
import { LAppLive2DManager } from './LAppLive2DManager';
import { LAppPal } from './LAppPal';

/**
 * 視圖管理類
 */
export class LAppView {
  private _programId: WebGLProgram | null = null;
  private _viewMatrix: CubismViewMatrix | null = null;
  private _deviceToScreen: CubismMatrix44 | null = null;
  private _gl: WebGLRenderingContext | WebGL2RenderingContext | null = null;
  private _canvasWidth: number = 0;
  private _canvasHeight: number = 0;

  /**
   * 初始化
   */
  public initialize(
    gl: WebGLRenderingContext | WebGL2RenderingContext
  ): void {
    if (!gl) {
      throw new Error('WebGL 上下文為 null');
    }

    this._gl = gl;

    // 創建視圖矩陣
    this._viewMatrix = new CubismViewMatrix();
    if (!this._viewMatrix) {
      throw new Error('CubismViewMatrix 創建失敗');
    }

    // 設置視圖範圍
    const left = CanvasSettings.ViewLogicalLeft;
    const right = CanvasSettings.ViewLogicalRight;
    const bottom = CanvasSettings.ViewLogicalBottom;
    const top = CanvasSettings.ViewLogicalTop;

    this._viewMatrix.setScreenRect(left, right, bottom, top);
    this._viewMatrix.setMaxScreenRect(
      CanvasSettings.ViewLogicalMaxLeft,
      CanvasSettings.ViewLogicalMaxRight,
      CanvasSettings.ViewLogicalMaxBottom,
      CanvasSettings.ViewLogicalMaxTop
    );

    this._viewMatrix.setMaxScale(CanvasSettings.ViewMaxScale);
    this._viewMatrix.setMinScale(CanvasSettings.ViewMinScale);

    // 創建設備轉換矩陣
    this._deviceToScreen = new CubismMatrix44();
    if (!this._deviceToScreen) {
      throw new Error('CubismMatrix44 創建失敗');
    }

    LAppPal.printLog('✓ LAppView 已初始化');
  }

  /**
   * 渲染
   */
  public render(): void {
    if (!this._gl) {
      LAppPal.printError('無法渲染：GL 上下文為 null');
      return;
    }

    const gl = this._gl;

    // 清除畫面
    gl.clearColor(0.0, 0.0, 0.0, 0.0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.clearDepth(1.0);

    // 啟用透明度混合
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    // 更新模型
    const modelManager = LAppLive2DManager.getInstance();
    if (!modelManager) {
      console.warn('[Render] modelManager is null');
      return;
    }
    
    const model = modelManager.getActiveModel();
    if (!model) {
      console.warn('[Render] model is null');
      return;
    }
    
    if (!model.getModel()) {
      console.warn('[Render] model.getModel() is null');
      return;
    }

    // 更新模型
    model.update();

    // 計算投影矩陣（使用官方 SDK 的方式）
    const projection = new CubismMatrix44();
    
    // 根據 Canvas 寬高比進行縮放
    if (this._canvasWidth > 0 && this._canvasHeight > 0) {
      if (model.getModel().getCanvasWidth() > 1.0 && this._canvasWidth < this._canvasHeight) {
        // 橫向較長的模型在縱向較長的視窗中顯示
        model.getModelMatrix().setWidth(2.0);
        projection.scale(1.0, this._canvasWidth / this._canvasHeight);
      } else {
        projection.scale(this._canvasHeight / this._canvasWidth, 1.0);
      }
    } else {
      console.warn('[Render] canvas size is 0:', this._canvasWidth, this._canvasHeight);
    }

    // 乘以視圖矩陣
    if (this._viewMatrix != null) {
      projection.multiplyByMatrix(this._viewMatrix);
    }

    // 繪製模型
    model.draw(projection);
  }

  /**
   * 調整視圖大小
   */
  public resize(width: number, height: number): void {
    if (!this._gl) return;

    this._canvasWidth = width;
    this._canvasHeight = height;

    const gl = this._gl;
    gl.viewport(0, 0, width, height);

    // 更新模型的 canvas 尺寸和渲染目標尺寸
    const modelManager = LAppLive2DManager.getInstance();
    if (modelManager) {
      const model = modelManager.getActiveModel();
      if (model) {
        model.setCanvasSize(width, height);
        model.setRenderTargetSize(width, height);
      }
    }

    // 重新計算設備轉換矩陣
    const ratio = width / height;
    const left = -ratio;
    const right = ratio;
    const bottom = CanvasSettings.ViewLogicalBottom;
    const top = CanvasSettings.ViewLogicalTop;

    // 更新視圖矩陣
    if (this._viewMatrix) {
      this._viewMatrix.setScreenRect(left, right, bottom, top);
      this._viewMatrix.scale(CanvasSettings.ViewScale, CanvasSettings.ViewScale);
    }

    // 更新設備轉換矩陣
    if (this._deviceToScreen) {
      this._deviceToScreen.loadIdentity();
      
      if (width > height) {
        const screenW = Math.abs(right - left);
        this._deviceToScreen.scaleRelative(screenW / width, -screenW / width);
      } else {
        const screenH = Math.abs(top - bottom);
        this._deviceToScreen.scaleRelative(screenH / height, -screenH / height);
      }
      this._deviceToScreen.translateRelative(-width * 0.5, -height * 0.5);
    }

    LAppPal.printLog(`視圖已調整大小：${width}x${height}`);
  }

  /**
   * 將螢幕座標轉換為視圖座標
   */
  public transformScreenToViewPoint(screenX: number, screenY: number): [number, number] {
    if (!this._gl || !this._deviceToScreen || !this._viewMatrix) {
      return [0, 0];
    }

    // 螢幕座標轉換為設備座標
    let deviceX = screenX;
    let deviceY = screenY;

    // 設備座標轉換為視圖座標
    const invertedMatrix = new CubismMatrix44();
    invertedMatrix.setMatrix(this._deviceToScreen.getArray());
    
    const inverted = invertedMatrix.getInvert();
    const viewPoint = inverted.transformX(deviceX);
    const viewPointY = inverted.transformY(deviceY);
    return [viewPoint, viewPointY];
  }

  /**
   * 縮放視圖
   */
  public scale(scaleX: number, scaleY: number): void {
    if (this._viewMatrix) {
      this._viewMatrix.scale(scaleX, scaleY);
    }
  }

  /**
   * 平移視圖
   */
  public translate(x: number, y: number): void {
    if (this._viewMatrix) {
      this._viewMatrix.translateX(x);
      this._viewMatrix.translateY(y);
    }
  }

  /**
   * 重置視圖
   */
  public resetView(): void {
    if (this._viewMatrix) {
      this._viewMatrix.scale(1.0, 1.0);
      this._viewMatrix.translateX(0.0);
      this._viewMatrix.translateY(0.0);
    }
  }

  /**
   * 取得視圖矩陣
   */
  public getViewMatrix(): CubismViewMatrix | null {
    return this._viewMatrix;
  }

  /**
   * 釋放資源
   */
  public release(): void {
    if (this._programId && this._gl) {
      this._gl.deleteProgram(this._programId);
      this._programId = null;
    }

    this._viewMatrix = null;
    this._deviceToScreen = null;
    this._gl = null;

    LAppPal.printLog('LAppView 已釋放');
  }
}
