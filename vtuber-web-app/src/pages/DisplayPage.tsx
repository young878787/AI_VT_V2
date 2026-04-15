/**
 * OBS 顯示頁 (/display)
 *
 * 此頁面不運行任何 Live2D 實例。
 * 透過 BroadcastChannel 接收主頁面 (/) 的 canvas 幀廣播，
 * 直接顯示主頁面渲染的結果，確保動作、表情、物理模擬完全像素同步，
 * 不會有任何動作不同步或重複計算的問題。
 *
 * 技術：createImageBitmap + ImageBitmapRenderingContext.transferFromImageBitmap()
 *   - 無 CPU 壓縮/解壓縮（相較舊版 toBlob WebP）
 *   - 幀率從 7–20fps 提升到 40–60fps
 *   - 完整保留 alpha 透明通道
 *
 * 使用方式：
 *   1. 先開啟主頁面 http://localhost:5173/  (AI 控制台)
 *   2. OBS 使用 Browser Source：http://localhost:5173/display（勾選「允許透明」）
 *      或分頁擷取：另開 Chrome 分頁 http://localhost:5173/display
 *
 * 背景設定（透明/純色/圖片）在主頁面控制台設定後自動同步。
 */
import { useEffect, useRef, CSSProperties } from 'react';
import { useBackgroundStore } from '../store/backgroundStore';
import { canvasSyncService } from '../services/canvasSyncService';
import './DisplayPage.css';

export const DisplayPage = () => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const ctxRef = useRef<ImageBitmapRenderingContext | null>(null);

  const {
    backgroundType,
    backgroundColor,
    backgroundImageUrl,
    backgroundImageFit,
    outputWidth,
    outputHeight,
  } = useBackgroundStore();

  // 讓 body/html 背景透明，避免壓過自訂背景
  useEffect(() => {
    document.documentElement.classList.add('display-mode');
    document.body.classList.add('display-mode');
    return () => {
      document.documentElement.classList.remove('display-mode');
      document.body.classList.remove('display-mode');
    };
  }, []);

  // 接收主頁面的 canvas 幀廣播並顯示
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    // bitmaprenderer context：直接將 ImageBitmap 提交給 canvas，無需解碼
    ctxRef.current = canvas.getContext('bitmaprenderer') as ImageBitmapRenderingContext | null;

    const stopReceiving = canvasSyncService.startReceiving((bitmap) => {
      const ctx = ctxRef.current;
      if (!ctx) {
        // 不支援 bitmaprenderer（極少數環境），釋放 bitmap
        bitmap.close();
        return;
      }
      // transferFromImageBitmap：零拷貝提交，bitmap 在此後被消耗（自動釋放）
      ctx.transferFromImageBitmap(bitmap);
    });

    return stopReceiving;
  }, []);

  // 背景樣式：由 backgroundStore（localStorage 持久化）決定
  const bgStyle: CSSProperties = (() => {
    if (backgroundType === 'color') {
      return { backgroundColor };
    }
    if (backgroundType === 'image' && backgroundImageUrl) {
      return {
        backgroundImage: `url("${backgroundImageUrl}")`,
        backgroundSize: backgroundImageFit === 'fill' ? '100% 100%' : backgroundImageFit,
        backgroundPosition: 'center',
        backgroundRepeat: 'no-repeat',
      };
    }
    // transparent — OBS 開啟「允許透明」即可透明擷取角色
    return { backgroundColor: 'transparent' };
  })();

  return (
    <div className="display-page" style={bgStyle}>
      {/*
       * bitmaprenderer canvas：接收主頁面 Live2D 渲染的 ImageBitmap 幀。
       * transferFromImageBitmap 直接消耗 bitmap，顯示端無需任何解碼。
       * alpha 透明通道由 ImageBitmap 原生保留，OBS 透明擷取可正常使用。
       */}
      <canvas
        ref={canvasRef}
        className="display-mirror"
        style={{ width: outputWidth, height: outputHeight }}
      />
    </div>
  );
};
