/**
 * 畫布同步服務 — ImageBitmap 版本（render-loop hook）
 *
 * 使用 createImageBitmap + BroadcastChannel 將主頁面的 Live2D canvas 鏡像到 /display 頁面。
 * /display 頁面不運行任何 Live2D 實例，只接收並顯示主頁面的渲染結果，
 * 確保兩者動作、表情、物理模擬像素完全一致。
 *
 * 架構說明：
 *   使用 LAppDelegate.setPostRenderCallback() hook 進 Live2D 渲染迴圈，
 *   在每幀 render() 完成後立即擷取畫面，消除獨立 rAF 帶來的 0–1 幀延遲。
 *
 *   只有 /display 宣告接收時才讀回 GPU 畫面；無接收端時保留 hook，略過擷取。
 *   接收端以心跳維持短租期，正常關閉立即取消，崩潰時由租期到期停止擷取。
 *
 * 需求前提：LAppDelegate 的 WebGL context 必須設定 preserveDrawingBuffer: true，
 * 否則 createImageBitmap 在 WebGL 下讀取的畫面可能是空白。
 *
 * 顯示端需求：
 *   使用 canvas.getContext('bitmaprenderer') + transferFromImageBitmap()，
 *   將 bitmap 直接提交給顯示端 canvas，無需中間解碼步驟。
 */

import type { LAppDelegate } from '../live2d/LAppDelegate';

const CHANNEL_NAME = 'live2d-canvas-sync';
const RECEIVER_HEARTBEAT_MS = 2000;
const RECEIVER_LEASE_MS = 6000;

class CanvasSyncService {
  private readonly channel: BroadcastChannel;
  private canvas: HTMLCanvasElement | null = null;
  private delegate: LAppDelegate | null = null;
  /** 上一次 createImageBitmap 是否仍在進行，防止同時發起多個 GPU 讀回 */
  private isPending = false;
  private generation = 0;
  private readonly receivers = new Map<string, number>();

  constructor() {
    this.channel = new BroadcastChannel(CHANNEL_NAME);
    this.channel.addEventListener('message', (event: MessageEvent) => {
      const data = event.data;
      if (!this.delegate || typeof data?.receiverId !== 'string') return;
      if (data.type === 'receiver_presence') {
        this.receivers.set(data.receiverId, performance.now() + RECEIVER_LEASE_MS);
      } else if (data.type === 'receiver_closed') {
        this.receivers.delete(data.receiverId);
      }
    });
  }

  // ─── 主頁面端 ────────────────────────────────────────────────────────────

  /**
   * 主頁面呼叫：hook 進 LAppDelegate 渲染迴圈，在每幀 render() 後立即擷取畫面廣播。
   * 相較於 startBroadcasting 的獨立 rAF，此方法消除 0–1 幀延遲。
   *
   * @param delegate LAppDelegate 實例（已初始化且 run() 尚未呼叫，或已在運行）
   */
  attachToRenderLoop(delegate: LAppDelegate): void {
    this.stopBroadcasting();
    this.delegate = delegate;
    delegate.setPostRenderCallback((canvas: HTMLCanvasElement) => {
      this.canvas = canvas;
      this.captureFrame();
    });
    // 已開啟的顯示端立即重新宣告，主頁重新載入時不用等待下一次心跳。
    this.channel.postMessage({ type: 'source_ready' });
  }

  /**
   * 主頁面呼叫：停止廣播，清除 render loop hook（元件卸載時呼叫）。
   */
  stopBroadcasting(): void {
    this.generation += 1;
    if (this.delegate) {
      this.delegate.setPostRenderCallback(null);
      this.delegate = null;
    }
    this.canvas = null;
    this.isPending = false;
    this.receivers.clear();
  }

  /**
   * 每幀渲染完成後由 delegate 呼叫。
   * isPending 旗標作為自然背壓：若上一幀 GPU 讀回未完成，跳過此幀。
   */
  private captureFrame(): void {
    if (!this.canvas || this.isPending || !this.hasReceivers()) return;

    this.isPending = true;
    const generation = this.generation;

    // createImageBitmap：從 WebGL canvas 讀取像素，建立 uncompressed bitmap
    // （需要 preserveDrawingBuffer: true，否則 WebGL 已清除 framebuffer）
    createImageBitmap(this.canvas)
      .then((bitmap) => {
        try {
          if (generation === this.generation && this.delegate && this.hasReceivers()) {
            // BroadcastChannel 使用 structured clone；顯示端持有獨立副本。
            this.channel.postMessage({ type: 'frame', bitmap });
          }
        } finally {
          bitmap.close();
        }
      })
      .catch(() => undefined)
      .finally(() => {
        // 舊擷取完成時不能解除重新掛載後的新擷取背壓。
        if (generation === this.generation) this.isPending = false;
      });
  }

  private hasReceivers(): boolean {
    const now = performance.now();
    for (const [receiverId, expiresAt] of this.receivers) {
      if (expiresAt <= now) this.receivers.delete(receiverId);
    }
    return this.receivers.size > 0;
  }

  // ─── 顯示頁面端 ───────────────────────────────────────────────────────────

  /**
   * 顯示頁面呼叫：開始接收主頁面的幀廣播。
   * @param onFrame 每當收到新幀時呼叫，傳入 ImageBitmap（由呼叫端負責消耗）
   * @returns cleanup 函數，在元件卸載時呼叫以移除監聽器
   */
  startReceiving(onFrame: (bitmap: ImageBitmap) => void): () => void {
    const receiverId = crypto.randomUUID();
    let stopped = false;
    let paused = false;
    let lastPresenceAt = Number.NEGATIVE_INFINITY;
    const announce = () => {
      if (stopped || paused) return;
      lastPresenceAt = performance.now();
      this.channel.postMessage({ type: 'receiver_presence', receiverId });
    };
    const handler = (e: MessageEvent) => {
      if (e.data?.type === 'source_ready') announce();
      if (e.data?.type === 'frame' && e.data.bitmap instanceof ImageBitmap) {
        if (stopped || paused) {
          e.data.bitmap.close();
          return;
        }
        // 背景分頁的 timer 可能被節流；持續收到畫面時仍能更新租期。
        if (performance.now() - lastPresenceAt >= RECEIVER_HEARTBEAT_MS) announce();
        onFrame(e.data.bitmap);
      }
    };
    const pageHide = () => {
      paused = true;
      this.channel.postMessage({ type: 'receiver_closed', receiverId });
    };
    const pageShow = () => {
      paused = false;
      announce();
    };
    this.channel.addEventListener('message', handler);
    window.addEventListener('pagehide', pageHide);
    window.addEventListener('pageshow', pageShow);
    const heartbeat = window.setInterval(announce, RECEIVER_HEARTBEAT_MS);
    announce();

    return () => {
      if (stopped) return;
      stopped = true;
      window.clearInterval(heartbeat);
      this.channel.removeEventListener('message', handler);
      window.removeEventListener('pagehide', pageHide);
      window.removeEventListener('pageshow', pageShow);
      this.channel.postMessage({ type: 'receiver_closed', receiverId });
    };
  }
}

export const canvasSyncService = new CanvasSyncService();
