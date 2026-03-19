/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

/**
 * プラットフォーム依存機能を抽象化する Cubism Platform Abstraction Layer.
 *
 * ファイル読み込みや時刻取得等のプラットフォームに依存する関数をまとめる。
 */
export class LAppPal {
  /**
   * ファイルをバイトデータとして読みこむ
   *
   * @param filePath 読み込み対象ファイルのパス
   * @return
   * {
   *      buffer,   読み込んだバイトデータ
   *      size        ファイルサイズ
   * }
   */
  public static loadFileAsBytes(
    filePath: string,
    callback: (arrayBuffer: ArrayBuffer, size: number) => void
  ): void {
    fetch(filePath)
      .then(response => response.arrayBuffer())
      .then(arrayBuffer => callback(arrayBuffer, arrayBuffer.byteLength));
  }

  /**
   * デルタ時間（前回フレームとの差分）を取得する
   * @return デルタ時間[ms]
   */
  public static getDeltaTime(): number {
    return this.deltaTime;
  }

  public static updateTime(): void {
    this.currentFrame = Date.now();
    this.deltaTime = (this.currentFrame - this.lastFrame) / 1000;
    this.lastFrame = this.currentFrame;
  }

  /**
   * メッセージを出力する
   * @param message 文字列
   */
  public static printMessage(message: string): void {
    console.log(message);
  }

  /**
   * 日誌輸出（一般訊息）
   */
  public static printLog(...args: unknown[]): void {
    console.log('[Live2D]', ...args);
  }

  /**
   * 日誌輸出別名
   */
  public static log(...args: unknown[]): void {
    console.log('[Live2D]', ...args);
  }

  /**
   * 警告訊息輸出
   */
  public static printWarning(...args: unknown[]): void {
    console.warn('[Live2D]', ...args);
  }

  /**
   * 錯誤訊息輸出
   */
  public static printError(...args: unknown[]): void {
    console.error('[Live2D]', ...args);
  }

  static lastUpdate = Date.now();

  static currentFrame = 0.0;
  static lastFrame = 0.0;
  static deltaTime = 0.0;
}
