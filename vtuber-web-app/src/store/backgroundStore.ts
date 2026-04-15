/**
 * 背景設定狀態管理 - 使用 Zustand + localStorage 持久化
 * 控制頁 (/) 和 OBS 顯示頁 (/display) 共享同一份設定
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type BackgroundType = 'transparent' | 'color' | 'image';
export type BackgroundFit = 'cover' | 'contain' | 'fill';

interface BackgroundState {
  backgroundType: BackgroundType;
  backgroundColor: string;
  backgroundImageUrl: string;
  backgroundImageFit: BackgroundFit;

  /** OBS Browser Source 輸出固定寬度（px），display canvas 使用此值作為 CSS 尺寸 */
  outputWidth: number;
  /** OBS Browser Source 輸出固定高度（px），display canvas 使用此值作為 CSS 尺寸 */
  outputHeight: number;

  setBackgroundType: (type: BackgroundType) => void;
  setBackgroundColor: (color: string) => void;
  setBackgroundImageUrl: (url: string) => void;
  setBackgroundImageFit: (fit: BackgroundFit) => void;
  setOutputResolution: (width: number, height: number) => void;
}

export const useBackgroundStore = create<BackgroundState>()(
  persist(
    (set) => ({
      // 預設：透明 (適合 OBS 透明擷取 / chroma key)
      backgroundType: 'transparent',
      backgroundColor: '#00b140',   // 預設綠幕色，方便切換
      backgroundImageUrl: '',
      backgroundImageFit: 'cover',

      // OBS 輸出固定分辨率（預設 Full HD）
      outputWidth: 1920,
      outputHeight: 1080,

      setBackgroundType: (type) => set({ backgroundType: type }),
      setBackgroundColor: (color) => set({ backgroundColor: color }),
      setBackgroundImageUrl: (url) => set({ backgroundImageUrl: url }),
      setBackgroundImageFit: (fit) => set({ backgroundImageFit: fit }),
      setOutputResolution: (width, height) => set({ outputWidth: width, outputHeight: height }),
    }),
    {
      name: 'vtuber-background-settings',   // localStorage key
    }
  )
);
