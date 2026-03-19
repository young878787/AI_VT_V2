/**
 * 嘴型同步管理器
 * 將麥克風音量映射到 Live2D 模型的嘴巴參數
 */

import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import { MicrophoneManager } from './MicrophoneManager';
import { LAppPal } from '../live2d/LAppPal';

/**
 * LipSync 配置
 */
export interface LipSyncConfig {
  // 嘴巴張開參數名稱（Hiyori 使用 ParamMouthOpenY）
  mouthParamId: string;
  
  // 音量到嘴巴開合的映射曲線係數
  volumeMultiplier: number;
  
  // 最大嘴巴開合值
  maxMouthOpen: number;
  
  // 閉嘴速度（值越小閉合越快）
  closingSpeed: number;
  
  // 開嘴速度（值越小打開越快）
  openingSpeed: number;
  
  // 靜音閾值
  silenceThreshold: number;
}

/**
 * 嘴型同步管理器
 */
export class LipSyncManager {
  private static s_instance: LipSyncManager | null = null;

  private _microphoneManager: MicrophoneManager;
  private _isEnabled: boolean = false;
  private _currentMouthValue: number = 0;
  private _targetMouthValue: number = 0;
  
  private _config: LipSyncConfig;

  /**
   * 取得單例實例
   */
  public static getInstance(): LipSyncManager {
    if (!this.s_instance) {
      this.s_instance = new LipSyncManager();
    }
    return this.s_instance;
  }

  /**
   * 釋放單例實例
   */
  public static releaseInstance(): void {
    this.s_instance = null;
  }

  private constructor() {
    this._microphoneManager = MicrophoneManager.getInstance();
    
    // 預設配置
    this._config = {
      mouthParamId: 'ParamMouthOpenY',
      volumeMultiplier: 8.0,  // 增加倍數以提高嘴型幅度
      maxMouthOpen: 1.0,
      closingSpeed: 0.15,
      openingSpeed: 0.3,
      silenceThreshold: 0.02,
    };
    
    LAppPal.printLog('LipSyncManager 已創建');
  }

  /**
   * 啟用嘴型同步
   */
  public async enable(): Promise<boolean> {
    if (this._isEnabled) {
      return true;
    }

    const success = await this._microphoneManager.enable();
    if (!success) {
      LAppPal.printError('無法啟用麥克風，嘴型同步啟動失敗');
      return false;
    }

    this._isEnabled = true;
    LAppPal.printLog('嘴型同步已啟用');
    return true;
  }

  /**
   * 禁用嘴型同步
   */
  public disable(): void {
    this._isEnabled = false;
    this._currentMouthValue = 0;
    this._targetMouthValue = 0;
    this._microphoneManager.disable();
    LAppPal.printLog('嘴型同步已禁用');
  }

  /**
   * 是否已啟用
   */
  public isEnabled(): boolean {
    return this._isEnabled;
  }

  /**
   * 更新嘴型同步
   * 應在每幀渲染循環中呼叫
   */
  public update(): void {
    if (!this._isEnabled) {
      return;
    }

    // 分析麥克風音量
    const analysis = this._microphoneManager.analyze();
    
    // 計算目標嘴巴開合值
    if (analysis.volume > this._config.silenceThreshold) {
      this._targetMouthValue = Math.min(
        this._config.maxMouthOpen,
        analysis.volume * this._config.volumeMultiplier
      );
    } else {
      this._targetMouthValue = 0;
    }

    // 平滑過渡到目標值
    if (this._currentMouthValue < this._targetMouthValue) {
      // 張嘴
      this._currentMouthValue += 
        (this._targetMouthValue - this._currentMouthValue) * this._config.openingSpeed;
    } else {
      // 閉嘴
      this._currentMouthValue += 
        (this._targetMouthValue - this._currentMouthValue) * this._config.closingSpeed;
    }

    // 確保值在有效範圍內
    this._currentMouthValue = Math.max(0, Math.min(this._config.maxMouthOpen, this._currentMouthValue));

    // 應用到模型
    this.applyToModel();
  }

  /**
   * 將嘴巴開合值應用到模型
   */
  private applyToModel(): void {
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    
    if (!model) {
      return;
    }

    // 直接設置 LipSync 值，模型會在 update 時應用
    model.setLipSyncValue(this._currentMouthValue);
  }

  /**
   * 取得當前嘴巴開合值
   */
  public getCurrentMouthValue(): number {
    return this._currentMouthValue;
  }

  /**
   * 取得當前配置
   */
  public getConfig(): LipSyncConfig {
    return { ...this._config };
  }

  /**
   * 更新配置
   */
  public updateConfig(config: Partial<LipSyncConfig>): void {
    this._config = { ...this._config, ...config };
    LAppPal.printLog('LipSync 配置已更新');
  }

  /**
   * 釋放資源
   */
  public release(): void {
    this.disable();
    LAppPal.printLog('LipSyncManager 資源已釋放');
  }
}
