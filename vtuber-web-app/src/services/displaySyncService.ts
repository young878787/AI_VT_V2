/**
 * Display 同步服務
 *
 * 供 /display 頁面使用，連接後端 /ws/display WebSocket 端點，
 * 接收 AI 行為廣播（behavior 訊息），並直接套用到本頁面的 Live2D 模型。
 *
 * 架構優點（業界標準方案）：
 *   - /display 頁面自行渲染獨立 Live2D，60fps 原生幀率
 *   - 表情/眼部/眉毛等 AI 行為參數與主頁面完全同步
 *   - 支援 OBS Browser Source（獨立進程，不依賴 BroadcastChannel）
 *   - 背景透明通道由 WebGL 原生保留，無壓縮損失
 */

import { useAppStore } from '../store/appStore';

class DisplaySyncService {
  private ws: WebSocket | null = null;
  private retryCount = 0;
  private readonly MAX_RETRIES = 20;
  private readonly RETRY_DELAY_MS = 3000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private isConnecting = false;

  /**
   * 連線到後端 /ws/display，開始接收行為廣播。
   * 支援自動重連（最多 MAX_RETRIES 次）。
   */
  connect(): void {
    if (this.isConnecting) return;
    if (this.ws?.readyState === WebSocket.OPEN || this.ws?.readyState === WebSocket.CONNECTING) return;

    this.isConnecting = true;
    const port = import.meta.env.BACKEND_PORT ?? '9999';

    try {
      this.ws = new WebSocket(`ws://localhost:${port}/ws/display`);
    } catch (e) {
      console.error('[DisplaySync] WebSocket 建立失敗:', e);
      this.isConnecting = false;
      this._scheduleRetry();
      return;
    }

    this.ws.onopen = () => {
      console.log('[DisplaySync] 已連線到 /ws/display');
      this.retryCount = 0;
      this.isConnecting = false;
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data as string);
        if (data.type === 'behavior') {
          useAppStore.getState().setAiBehavior(
            data.headIntensity  ?? 0.3,
            data.blushLevel     ?? 0.0,
            data.eyeLOpen       ?? 1.0,
            data.eyeROpen       ?? 1.0,
            data.durationSec    ?? 5.0,
            data.mouthForm      ?? 0.0,
            data.browLY         ?? 0.0,
            data.browRY         ?? 0.0,
            data.browLAngle     ?? 0.0,
            data.browRAngle     ?? 0.0,
            data.browLForm      ?? 0.0,
            data.browRForm      ?? 0.0,
            data.eyeSync        ?? true,
          );
        }
        // 忽略 ping 等其他訊息類型
      } catch (e) {
        console.error('[DisplaySync] 訊息解析失敗:', e);
      }
    };

    this.ws.onclose = () => {
      this.ws = null;
      this.isConnecting = false;
      this._scheduleRetry();
    };

    this.ws.onerror = () => {
      // onerror 後必定觸發 onclose，由 onclose 處理重連
      this.isConnecting = false;
    };
  }

  /**
   * 中斷連線並取消自動重連。
   * 元件卸載時呼叫（useEffect cleanup）。
   */
  disconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    // 重置重試計數，避免下次重新 mount 時直接達到上限
    this.retryCount = 0;
    this.isConnecting = false;
    if (this.ws) {
      this.ws.onclose = null; // 防止 onclose 觸發重連
      this.ws.close();
      this.ws = null;
    }
  }

  private _scheduleRetry(): void {
    this.retryCount++;
    if (this.retryCount > this.MAX_RETRIES) {
      console.warn(`[DisplaySync] 重連 ${this.MAX_RETRIES} 次後仍失敗，停止重連。後端啟動後請重新整理頁面。`);
      return;
    }
    console.log(`[DisplaySync] ${this.RETRY_DELAY_MS / 1000}s 後重連 (${this.retryCount}/${this.MAX_RETRIES})...`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, this.RETRY_DELAY_MS);
  }
}

export const displaySyncService = new DisplaySyncService();
