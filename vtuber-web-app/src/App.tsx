/** Rushia 主舞台；調整工具保留在不影響取景的側邊抽屜。 */
import { useEffect, useRef, useState } from 'react';
import { Live2DCanvas } from '@components/Live2DCanvas';
import { ControlPanel } from '@components/ControlPanel';
import { AIChatPanel } from '@components/AIChatPanel';
import { HitAreaOverlay } from '@components/HitAreaOverlay';
import { ExpressionPlanDebugPanel } from '@components/ExpressionPlanDebugPanel';
import { NativeParamPanel } from '@components/NativeParamPanel';
import { EmotionSidebar } from '@components/EmotionSidebar';
import { useAppStore } from '@store/appStore';
import './App.css';

type Drawer = 'settings' | 'expressions' | null;
type SettingsTab = 'appearance' | 'parameters' | 'emotion';

function App() {
  const modelLoaded = useAppStore(s => s.modelLoaded);
  const modelLoading = useAppStore(s => s.modelLoading);
  const modelError = useAppStore(s => s.modelError);
  const isAiTyping = useAppStore(s => s.isAiTyping);
  const isSpeaking = useAppStore(s => s.isSpeaking);
  const resetModelTransform = useAppStore(s => s.resetModelTransform);
  const [drawer, setDrawer] = useState<Drawer>(null);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('appearance');
  const drawerTrigger = useRef<HTMLButtonElement | null>(null);
  const closeButton = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!drawer) return;
    closeButton.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setDrawer(null);
        drawerTrigger.current?.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [drawer]);

  const toggleDrawer = (next: Exclude<Drawer, null>, trigger: HTMLButtonElement) => {
    drawerTrigger.current = trigger;
    setDrawer(current => current === next ? null : next);
  };

  const status = modelError ? '載入遇到問題' : modelLoading ? '準備中' : isSpeaking ? '正在說話' : isAiTyping ? '思考中' : modelLoaded ? '已就緒' : '等待載入';

  return (
    <div className="app-layout">
      <header className="room-header">
        <div className="room-brand">
          <span className="room-brand__mark" aria-hidden="true">✦</span>
          <div><span className="room-brand__name">Rushia</span><span className="room-brand__caption">與露西亞的日常</span></div>
        </div>
        <div className="room-header__actions">
          <span className={`room-status ${modelError ? 'room-status--error' : ''}`} role="status">
            <span className={modelLoaded ? 'room-status__dot room-status__dot--ready' : 'room-status__dot'} />{status}
          </span>
          <button type="button" className={`room-button ${drawer === 'expressions' ? 'room-button--active' : ''}`}
            aria-expanded={drawer === 'expressions'} aria-controls="room-tools"
            onClick={event => toggleDrawer('expressions', event.currentTarget)}>表情工作室</button>
          <button type="button" className={`room-button room-button--subtle ${drawer === 'settings' ? 'room-button--active' : ''}`}
            aria-expanded={drawer === 'settings'} aria-controls="room-tools"
            onClick={event => toggleDrawer('settings', event.currentTarget)}>設定</button>
        </div>
      </header>

      <main className="app-layout__center" aria-label="露西亞角色舞台">
        <Live2DCanvas />
        <HitAreaOverlay />
        <div className="stage-caption" aria-hidden="true"><span>RUSHIA</span><span>STAY A LITTLE LONGER</span></div>
        <div className="stage-footer">
          <span className="stage-footer__state" role="status">{isSpeaking ? '露西亞正在說話…' : isAiTyping ? '正在想要怎麼回覆你…' : '今天，想聊些什麼？'}</span>
          <button type="button" className="stage-reset" onClick={resetModelTransform} disabled={!modelLoaded}>重置構圖</button>
        </div>
      </main>

      <section className="app-layout__chat" aria-label="與露西亞對話"><AIChatPanel /></section>

      <aside id="room-tools" className={`room-drawer ${drawer === 'expressions' ? 'room-drawer--studio' : ''}`}
        aria-label={drawer === 'expressions' ? '表情工作室' : '舞台設定'} hidden={!drawer}>
        <header className="room-drawer__header">
          <div><span className="room-drawer__eyebrow">{drawer === 'expressions' ? 'EXPRESSION STUDIO' : 'ROOM SETTINGS'}</span>
            <h2>{drawer === 'expressions' ? '表情工作室' : '舞台設定'}</h2></div>
          <button ref={closeButton} type="button" className="room-drawer__close" aria-label="關閉工具面板"
            onClick={() => { setDrawer(null); drawerTrigger.current?.focus(); }}>×</button>
        </header>
        <div className="room-drawer__studio" hidden={drawer !== 'expressions'}><ExpressionPlanDebugPanel /></div>
        <div className="room-drawer__settings" hidden={drawer !== 'settings'}>
          <nav className="room-tabs" aria-label="設定分類">
            {([['appearance', '畫面與互動'], ['parameters', '原生參數'], ['emotion', '情緒狀態']] as const).map(([tab, label]) => (
              <button key={tab} type="button" className={settingsTab === tab ? 'room-tabs__tab room-tabs__tab--active' : 'room-tabs__tab'}
                aria-pressed={settingsTab === tab} onClick={() => setSettingsTab(tab)}>{label}</button>
            ))}
          </nav>
          {/* 保持掛載，讓麥克風、視線與自動播放的生命週期不受抽屜開關影響。 */}
          <div className="room-drawer__panel" hidden={settingsTab !== 'appearance'}><ControlPanel /></div>
          {drawer === 'settings' && settingsTab === 'parameters' && <div className="room-drawer__panel room-drawer__parameters"><NativeParamPanel /></div>}
          {drawer === 'settings' && settingsTab === 'emotion' && <div className="room-drawer__panel room-drawer__emotion"><EmotionSidebar /></div>}
        </div>
      </aside>
    </div>
  );
}

export default App;
