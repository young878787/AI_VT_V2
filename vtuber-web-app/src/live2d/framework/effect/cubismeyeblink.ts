/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

import { ICubismModelSetting } from '../icubismmodelsetting';
import { CubismIdHandle } from '../id/cubismid';
import { CubismModel } from '../model/cubismmodel';

/**
 * 自動まばたき機能
 *
 * 自動まばたき機能を提供する。
 */
export class CubismEyeBlink {
  /**
   * インスタンスを作成する
   * @param modelSetting モデルの設定情報
   * @return 作成されたインスタンス
   * @note 引数がNULLの場合、パラメータIDが設定されていない空のインスタンスを作成する。
   */
  public static create(
    modelSetting: ICubismModelSetting = null
  ): CubismEyeBlink {
    return new CubismEyeBlink(modelSetting);
  }

  /**
   * インスタンスの破棄
   * @param eyeBlink 対象のCubismEyeBlink
   */
  public static delete(eyeBlink: CubismEyeBlink): void {
    if (eyeBlink != null) {
      eyeBlink = null;
    }
  }

  /**
   * まばたきの間隔の設定
   * @param blinkingInterval まばたきの間隔の時間[秒]
   */
  public setBlinkingInterval(blinkingInterval: number): void {
    this._blinkingIntervalSeconds = blinkingInterval;
  }

  /**
   * まばたきの間隔の範囲を設定
   * @param minInterval 最小間隔[秒]
   * @param maxInterval 最大間隔[秒]
   */
  public setBlinkingIntervalRange(minInterval: number, maxInterval: number): void {
    this._blinkingIntervalMin = minInterval;
    this._blinkingIntervalMax = maxInterval;
  }

  /**
   * 自動まばたきを一時停止
   * @param durationSec 停止時間[秒]（0=無期限）
   */
  public pause(durationSec: number = 0): void {
    this._paused = true;
    this._pauseEndTime = durationSec > 0 ? this._userTimeSeconds + durationSec : 0;
  }

  /**
   * 自動まばたきを再開
   */
  public resume(): void {
    this._paused = false;
    this._pauseEndTime = 0;
    this._nextBlinkingTime = this.determinNextBlinkingTiming();
  }

  /**
   * 強制まばたき（即座に1回まばたきを実行）
   * @param pauseAfterSec 眨眼完成後暫停自動眨眼的時間（秒），0=不暫停
   */
  public forceBlink(pauseAfterSec: number = 0): void {
    this._blinkingState = EyeState.EyeState_Closing;
    this._stateStartTimeSeconds = this._userTimeSeconds;
    if (pauseAfterSec > 0) {
      this._pendingPauseDuration = pauseAfterSec;
    }
  }

  /**
   * 一時停止中かどうか
   */
  public isPaused(): boolean {
    return this._paused;
  }

  /**
   * まばたきのモーションの詳細設定
   * @param closing   まぶたを閉じる動作の所要時間[秒]
   * @param closed    まぶたを閉じている動作の所要時間[秒]
   * @param opening   まぶたを開く動作の所要時間[秒]
   */
  public setBlinkingSetting(
    closing: number,
    closed: number,
    opening: number
  ): void {
    this._closingSeconds = closing;
    this._closedSeconds = closed;
    this._openingSeconds = opening;
  }

  /**
   * まばたきさせるパラメータIDのリストの設定
   * @param parameterIds パラメータのIDのリスト
   */
  public setParameterIds(parameterIds: Array<CubismIdHandle>): void {
    this._parameterIds = parameterIds;
  }

  /**
   * まばたきさせるパラメータIDのリストの取得
   * @return パラメータIDのリスト
   */
  public getParameterIds(): Array<CubismIdHandle> {
    return this._parameterIds;
  }

  /**
   * モデルのパラメータの更新
   * @param model 対象のモデル
   * @param deltaTimeSeconds デルタ時間[秒]
   */
  public updateParameters(model: CubismModel, deltaTimeSeconds: number): void {
    this._userTimeSeconds += deltaTimeSeconds;

    // 暫停中自動恢復檢查
    if (this._paused && this._pauseEndTime > 0 && this._userTimeSeconds >= this._pauseEndTime) {
      this.resume();
    }

    // 暫停中跳過更新
    if (this._paused) {
      return;
    }

    let parameterValue: number;
    let t = 0.0;
    const blinkingState: EyeState = this._blinkingState;

    switch (blinkingState) {
      case EyeState.EyeState_Closing:
        t =
          (this._userTimeSeconds - this._stateStartTimeSeconds) /
          this._closingSeconds;

        if (t >= 1.0) {
          t = 1.0;
          this._blinkingState = EyeState.EyeState_Closed;
          this._stateStartTimeSeconds = this._userTimeSeconds;
        }

        parameterValue = 1.0 - t;

        break;
      case EyeState.EyeState_Closed:
        t =
          (this._userTimeSeconds - this._stateStartTimeSeconds) /
          this._closedSeconds;

        if (t >= 1.0) {
          this._blinkingState = EyeState.EyeState_Opening;
          this._stateStartTimeSeconds = this._userTimeSeconds;
        }

        parameterValue = 0.0;

        break;
      case EyeState.EyeState_Opening:
        t =
          (this._userTimeSeconds - this._stateStartTimeSeconds) /
          this._openingSeconds;

        if (t >= 1.0) {
          t = 1.0;
          this._blinkingState = EyeState.EyeState_Interval;
          this._nextBlinkingTime = this.determinNextBlinkingTiming();

          // 眨眼完成後，執行待處理的暫停請求
          if (this._pendingPauseDuration > 0) {
            this.pause(this._pendingPauseDuration);
            this._pendingPauseDuration = 0;
          }
        }

        parameterValue = t;

        break;
      case EyeState.EyeState_Interval:
        if (this._nextBlinkingTime < this._userTimeSeconds) {
          this._blinkingState = EyeState.EyeState_Closing;
          this._stateStartTimeSeconds = this._userTimeSeconds;
        }

        parameterValue = 1.0;

        break;
      case EyeState.EyeState_First:
      default:
        this._blinkingState = EyeState.EyeState_Interval;
        this._nextBlinkingTime = this.determinNextBlinkingTiming();

        parameterValue = 1.0;
        break;
    }

    if (!CubismEyeBlink.CloseIfZero) {
      parameterValue = -parameterValue;
    }

    for (let i = 0; i < this._parameterIds.length; ++i) {
      model.setParameterValueById(this._parameterIds[i], parameterValue);
    }
  }

  /**
   * コンストラクタ
   * @param modelSetting モデルの設定情報
   */
  public constructor(modelSetting: ICubismModelSetting) {
    this._blinkingState = EyeState.EyeState_First;
    this._nextBlinkingTime = 0.0;
    this._stateStartTimeSeconds = 0.0;
    this._blinkingIntervalSeconds = 2.5;
    this._blinkingIntervalMin = 1.0;
    this._blinkingIntervalMax = 4.0;
    this._closingSeconds = 0.1;
    this._closedSeconds = 0.05;
    this._openingSeconds = 0.15;
    this._userTimeSeconds = 0.0;
    this._parameterIds = new Array<CubismIdHandle>();
    this._paused = false;
    this._pauseEndTime = 0.0;
    this._pendingPauseDuration = 0.0;

    if (modelSetting == null) {
      return;
    }

    this._parameterIds.length = modelSetting.getEyeBlinkParameterCount();
    for (let i = 0; i < modelSetting.getEyeBlinkParameterCount(); ++i) {
      this._parameterIds[i] = modelSetting.getEyeBlinkParameterId(i);
    }
  }

  /**
   * 次の瞬きのタイミングの決定
   *
   * @return 次のまばたきを行う時刻[秒]
   */
  public determinNextBlinkingTiming(): number {
    const r: number = Math.random();
    return this._userTimeSeconds + this._blinkingIntervalMin + r * (this._blinkingIntervalMax - this._blinkingIntervalMin);
  }

  _blinkingState: number; // 現在の状態
  _parameterIds: Array<CubismIdHandle>; // 操作対象のパラメータのIDのリスト
  _nextBlinkingTime: number; // 次のまばたきの時刻[秒]
  _stateStartTimeSeconds: number; // 現在の状態が開始した時刻[秒]
  _blinkingIntervalSeconds: number; // まばたきの間隔[秒]（互換用）
  _blinkingIntervalMin: number; // まばたきの最小間隔[秒]
  _blinkingIntervalMax: number; // まばたきの最大間隔[秒]
  _closingSeconds: number; // まぶたを閉じる動作の所要時間[秒]
  _closedSeconds: number; // まぶたを閉じている動作の所要時間[秒]
  _openingSeconds: number; // まぶたを開く動作の所要時間[秒]
  _userTimeSeconds: number; // デルタ時間の積算値[秒]
  _paused: boolean; // 自動まばたき一時停止フラグ
  _pauseEndTime: number; // 一時停止終了時刻（0=無期限）
  _pendingPauseDuration: number; // 眨眼完成後待處理的暫停時間[秒]

  /**
   * IDで指定された目のパラメータが、0のときに閉じるなら true 、1の時に閉じるなら false 。
   */
  static readonly CloseIfZero: boolean = true;
}

/**
 * まばたきの状態
 *
 * まばたきの状態を表す列挙型
 */
export enum EyeState {
  EyeState_First = 0, // 初期状態
  EyeState_Interval, // まばたきしていない状態
  EyeState_Closing, // まぶたが閉じていく途中の状態
  EyeState_Closed, // まぶたが閉じている状態
  EyeState_Opening // まぶたが開いていく途中の状態
}

// Namespace definition for compatibility.
import * as $ from './cubismeyeblink';
// eslint-disable-next-line @typescript-eslint/no-namespace
export namespace Live2DCubismFramework {
  export const CubismEyeBlink = $.CubismEyeBlink;
  export type CubismEyeBlink = $.CubismEyeBlink;
  export const EyeState = $.EyeState;
  export type EyeState = $.EyeState;
}
