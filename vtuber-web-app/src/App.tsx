/**
 * 主應用程式 — CSS Grid 三欄佈局
 *
 * ┌───────────┬───────────────────┬───────────┐
 * │           │                   │  AI Chat  │
 * │  Control  │    Live2D         │           │
 * │  Panel    │    (含背景預覽)    ├───────────┤
 * │           │                   │  Emotion  │
 * │           ├───────────────────┤  Params   │
 * │           │  預留空白          │           │
 * └───────────┴───────────────────┴───────────┘
 */
import { useEffect } from 'react';
import { Live2DCanvas } from '@components/Live2DCanvas';
import { ControlPanel } from '@components/ControlPanel';
import { AIChatPanel } from '@components/AIChatPanel';
import { AnimeDecoration } from '@components/AnimeDecoration';
import { HitAreaOverlay } from '@components/HitAreaOverlay';
import { ModelParamPanel } from '@components/ModelParamPanel';
import { NativeParamPanel } from '@components/NativeParamPanel';
import { useAppStore } from '@store/appStore';
import './App.css';

function App() {
  const loadAvailableModels = useAppStore(s => s.loadAvailableModels);

  // 啟動時從後端載入模型清單（含匯入的模型）
  useEffect(() => {
    loadAvailableModels();
  }, []);

  return (
    <div className="app-layout">
      {/* 背景裝飾 */}
      <AnimeDecoration />

      {/* ── 左側：控制面板 ── */}
      <aside className="app-layout__sidebar">
        <ControlPanel />
      </aside>

      {/* ── 中央：Live2D 即時預覽 ── */}
      <main className="app-layout__center">
        <Live2DCanvas />
        <HitAreaOverlay />
      </main>

      {/* ── 中下：原生 Live2D 參數面板 ── */}
      <div className="app-layout__bottom-center">
        <NativeParamPanel />
      </div>

      {/* ── 右上：AI 對話 ── */}
      <section className="app-layout__chat">
        <AIChatPanel />
      </section>

      {/* ── 右下：情緒參數 ── */}
      <section className="app-layout__params">
        <ModelParamPanel />
      </section>
    </div>
  );
}

export default App;
