/**
 * 動作控制器
 * 管理 Live2D 模型的動作播放、切換和優先級控制
 */

import { LAppLive2DManager } from './LAppLive2DManager';
import { LAppPal } from './LAppPal';
import { Priority } from './LAppDefine';

/**
 * 動作資訊介面
 */
export interface MotionInfo {
  group: string;         // 動作群組（如 Idle, TapBody 等）
  index: number;         // 動作在群組中的索引
  name?: string;         // 動作名稱（可選）
  priority: number;      // 優先級
}

/**
 * 動作控制器類
 */
export class MotionController {
  private static s_instance: MotionController | null = null;
  
  private _isEnabled: boolean = true;
  private _currentMotion: MotionInfo | null = null;


  /**
   * 取得單例實例
   */
  public static getInstance(): MotionController {
    if (!this.s_instance) {
      this.s_instance = new MotionController();
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
    LAppPal.printLog('MotionController 已初始化');
  }

  /**
   * 啟用/禁用動作控制
   */
  public setEnabled(enabled: boolean): void {
    this._isEnabled = enabled;
    LAppPal.printLog(`動作控制器: ${enabled ? '啟用' : '禁用'}`);
  }

  /**
   * 是否啟用
   */
  public isEnabled(): boolean {
    return this._isEnabled;
  }

  /**
   * 手動播放隨機待機片段
   */
  public playRandomIdle(): void {
    if (!this._isEnabled) return;
    
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    
    if (model) {
      model.startRandomMotion('Idle', Priority.Idle);
      this._currentMotion = { group: 'Idle', index: -1, priority: Priority.Idle };
    }
  }

  /**
   * 播放指定動作
   * @param group 動作群組名稱
   * @param index 動作索引
   * @param priority 優先級
   */
  public playMotion(group: string, index: number, priority: number = Priority.Normal): void {
    if (!this._isEnabled) return;
    
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    
    if (model) {
      model.startMotion(group, index, priority);
      this._currentMotion = { group, index, priority };
      LAppPal.printLog(`播放動作: ${group}[${index}], 優先級: ${priority}`);
    }
  }

  /**
   * 播放隨機動作
   * @param group 動作群組名稱
   * @param priority 優先級
   */
  public playRandomMotion(group: string, priority: number = Priority.Normal): void {
    if (!this._isEnabled) return;
    
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    
    if (model) {
      model.startRandomMotion(group, priority);
      this._currentMotion = { group, index: -1, priority };
      LAppPal.printLog(`播放隨機動作: ${group}, 優先級: ${priority}`);
    }
  }

  /**
   * 強制播放動作（最高優先級）
   */
  public forcePlayMotion(group: string, index: number): void {
    this.playMotion(group, index, Priority.Force);
  }

  /**
   * 取得當前動作資訊
   */
  public getCurrentMotion(): MotionInfo | null {
    return this._currentMotion;
  }

  /**
   * 取得可用的動作群組列表
   */
  public getAvailableMotionGroups(): string[] {
    // 基於 Hiyori/Haru 模型的標準動作群組
    return ['Idle', 'TapBody', 'TapHead'];
  }
}
