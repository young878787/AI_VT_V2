/**
 * 舞台設定 — 保留互動、構圖與輸出控制
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useAppStore } from '@store/appStore';
import { useBackgroundStore, type BackgroundType, type BackgroundFit } from '../store/backgroundStore';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import { MotionController } from '../live2d/MotionController';
import { LipSyncManager } from '../audio/LipSyncManager';
import { NativePreviewPanel } from './NativePreviewPanel';
import './ControlPanel.css';

/** 可折疊區塊 key */
type SectionKey = 'controls' | 'blink' | 'transform' | 'background' | 'motion';

const SECTION_LABELS: Record<SectionKey, string> = {
  controls:   '功能控制',
  blink:      '眨眼控制',
  transform:  '模型調整',
  background: 'OBS 輸出設定',
  motion:     '動作測試',
};

const SECTION_ICONS: Record<SectionKey, string> = {
  controls:   '⚡',
  blink:      '👁',
  transform:  '🔧',
  background: '🖥',
  motion:     '▶',
};

export const ControlPanel = ({ debugApiBaseUrl }: { debugApiBaseUrl?: string }) => {
  const {
    microphoneEnabled,
    microphonePermission,
    eyeTrackingEnabled,
    autoPlayEnabled,
    modelLoaded,
    modelLoading,
    modelError,
    modelScale,
    modelDragEnabled,
    toggleModelDrag,
    hitAreaDebug,
    toggleMicrophone,
    toggleEyeTracking,
    toggleAutoPlay,
    toggleHitAreaDebug,
    scaleModelUp,
    scaleModelDown,
    resetModelTransform,
    setMicrophonePermission,
  } = useAppStore();

  // 折疊狀態：預設展開互動控制
  const [collapsed, setCollapsed] = useState<Record<SectionKey, boolean>>({
    controls:   false,
    blink:      true,
    transform:  true,
    background: true,
    motion:     true,
  });

  const toggleSection = (key: SectionKey) =>
    setCollapsed(prev => ({ ...prev, [key]: !prev[key] }));

  const [lipSyncVolume, setLipSyncVolume] = useState<number>(0);

  // 眨眼控制狀態
  const [blinkPaused, setBlinkPaused] = useState(false);
  const [blinkInterval, setBlinkInterval] = useState({ min: 1.0, max: 4.0 });

  // 背景設定狀態
  const {
    backgroundType,
    backgroundColor,
    backgroundImageUrl,
    backgroundImageFit,
    outputWidth,
    outputHeight,
    setBackgroundType,
    setBackgroundColor,
    setBackgroundImageUrl,
    setBackgroundImageFit,
    setOutputResolution,
  } = useBackgroundStore();

  const PRESETS = [
    { label: '1920×1080', w: 1920, h: 1080 },
    { label: '1280×720',  w: 1280, h: 720  },
    { label: '2560×1440', w: 2560, h: 1440 },
  ] as const;
  const isCustomResolution = !PRESETS.some(p => p.w === outputWidth && p.h === outputHeight);

  const [customW, setCustomW] = useState(String(outputWidth));
  const [customH, setCustomH] = useState(String(outputHeight));

  const handleApplyCustomResolution = useCallback(() => {
    const w = parseInt(customW, 10);
    const h = parseInt(customH, 10);
    if (w > 0 && h > 0) setOutputResolution(w, h);
  }, [customW, customH, setOutputResolution]);

  const [imageUrlInput, setImageUrlInput] = useState(
    backgroundImageUrl.startsWith('data:') ? '[本地圖片]' : backgroundImageUrl
  );
  const fileInputRef = useRef<HTMLInputElement>(null);
  const lipSyncLoopRef = useRef<number | null>(null);

  // 自動播放控制
  useEffect(() => {
    const mc = MotionController.getInstance();
    if (autoPlayEnabled && modelLoaded) mc.startAutoPlay();
    else mc.stopAutoPlay();
    return () => mc.stopAutoPlay();
  }, [autoPlayEnabled, modelLoaded]);

  // 視線追蹤控制
  useEffect(() => {
    if (!modelLoaded) return;
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model) model.setEyeTrackingEnabled(eyeTrackingEnabled);
  }, [eyeTrackingEnabled, modelLoaded]);

  // LipSync 循環
  useEffect(() => {
    const lm = LipSyncManager.getInstance();
    const tick = () => {
      if (microphoneEnabled) {
        lm.update();
        setLipSyncVolume(lm.getCurrentMouthValue());
      }
      lipSyncLoopRef.current = requestAnimationFrame(tick);
    };
    if (microphoneEnabled) lipSyncLoopRef.current = requestAnimationFrame(tick);
    return () => {
      if (lipSyncLoopRef.current) cancelAnimationFrame(lipSyncLoopRef.current);
    };
  }, [microphoneEnabled]);

  // 麥克風開關
  const handleMicrophoneToggle = useCallback(async () => {
    const lm = LipSyncManager.getInstance();
    if (!microphoneEnabled) {
      try {
        const ok = await lm.enable();
        if (ok) { setMicrophonePermission('granted'); toggleMicrophone(); }
        else setMicrophonePermission('denied');
      } catch { setMicrophonePermission('denied'); }
    } else {
      lm.disable();
      toggleMicrophone();
      const model = LAppLive2DManager.getInstance().getActiveModel();
      if (model) model.setLipSyncValue(0);
    }
  }, [microphoneEnabled, toggleMicrophone, setMicrophonePermission]);

  // 眨眼控制
  const handleForceBlink = useCallback(() => {
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model) {
      model.forceBlink(1.5);
    }
  }, []);

  const handlePauseBlink = useCallback(() => {
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model) {
      model.pauseAutoBlink(3.0);
      setBlinkPaused(true);
      setTimeout(() => setBlinkPaused(false), 3000);
    }
  }, []);

  const handleResumeBlink = useCallback(() => {
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model) {
      model.resumeAutoBlink();
      setBlinkPaused(false);
    }
  }, []);

  const handleSetBlinkInterval = useCallback((min: number, max: number) => {
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model) {
      model.setBlinkInterval(min, max);
      setBlinkInterval({ min, max });
    }
  }, []);

  // 圖片上傳
  const handleFileUpload = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = ev => {
      const url = ev.target?.result as string;
      setBackgroundImageUrl(url);
      setImageUrlInput('[本地圖片]');
      setBackgroundType('image');
    };
    reader.readAsDataURL(file);
  }, [setBackgroundImageUrl, setBackgroundType]);

  const handleApplyImageUrl = useCallback(() => {
    if (imageUrlInput.trim() && imageUrlInput !== '[本地圖片]') {
      setBackgroundImageUrl(imageUrlInput.trim());
      setBackgroundType('image');
    }
  }, [imageUrlInput, setBackgroundImageUrl, setBackgroundType]);

  /** 區塊標題 */
  const sectionHeader = (id: SectionKey, extra?: React.ReactNode) => (
    <button
      className={`section-header ${collapsed[id] ? 'collapsed' : ''}`}
      onClick={() => toggleSection(id)}
      aria-expanded={!collapsed[id]}
    >
      <span className="section-header__icon">{SECTION_ICONS[id]}</span>
      <span className="section-header__label">{SECTION_LABELS[id]}</span>
      {extra && <span className="section-header__extra">{extra}</span>}
      <span className="section-header__arrow">›</span>
    </button>
  );

  return (
    <div className="ctrl-panel">
      {/* ── 頂部品牌列 ── */}
      <div className="ctrl-panel__topbar">
        <div className="ctrl-panel__brand">
          <span className="ctrl-panel__brand-title">Rushia · 半身舞台</span>
        </div>
        {/* 模型狀態指示器 */}
        <div className={`ctrl-panel__status-chip ${
          modelLoading ? 'loading' : modelError ? 'error' : modelLoaded ? 'ready' : 'idle'
        }`}>
          <span className="chip-dot" />
          <span className="chip-label">
            {modelLoading ? '載入中' : modelError ? '錯誤' : modelLoaded ? '就緒' : '等待'}
          </span>
        </div>
      </div>

      {/* ── 錯誤訊息 ── */}
      {modelError && (
        <div className="ctrl-panel__error">
          <span>⚠</span> {modelError}
        </div>
      )}

      {/* ── 可滾動內容 ── */}
      <div className="ctrl-panel__body">

        {/* ━━ 功能控制 ━━ */}
        <div className="ctrl-section">
          {sectionHeader('controls')}
          {!collapsed.controls && (
            <div className="ctrl-section__content">

              {/* 麥克風 */}
              <div className="cp-toggle-row">
                <div className="cp-toggle-info">
                  <span className="cp-toggle-icon">🎤</span>
                  <span className="cp-toggle-label">麥克風嘴型同步</span>
                </div>
                <button
                  className={`cp-toggle ${microphoneEnabled ? 'active' : ''}`}
                  role="switch" aria-label="麥克風嘴型同步" aria-checked={microphoneEnabled}
                  onClick={handleMicrophoneToggle}
                  disabled={!modelLoaded || microphonePermission === 'denied'}
                >
                  <span className="cp-toggle__thumb" />
                </button>
              </div>
              {microphoneEnabled && (
                <div className="cp-volume">
                  <span className="cp-volume__label">音量</span>
                  <div className="cp-volume__bar">
                    <div className="cp-volume__fill" style={{ width: `${lipSyncVolume * 100}%` }} />
                  </div>
                  <span className="cp-volume__val">{(lipSyncVolume * 100).toFixed(0)}%</span>
                </div>
              )}
              {microphonePermission === 'denied' && (
                <div className="cp-warn">請在瀏覽器允許麥克風權限</div>
              )}

              {/* 視線追蹤 */}
              <div className="cp-toggle-row">
                <div className="cp-toggle-info">
                  <span className="cp-toggle-icon">👁</span>
                  <span className="cp-toggle-label">滑鼠視線追蹤</span>
                </div>
                <button
                  className={`cp-toggle ${eyeTrackingEnabled ? 'active' : ''}`}
                  role="switch" aria-label="滑鼠視線追蹤" aria-checked={eyeTrackingEnabled}
                  onClick={toggleEyeTracking}
                  disabled={!modelLoaded}
                >
                  <span className="cp-toggle__thumb" />
                </button>
              </div>

              {/* 自動播放 */}
              <div className="cp-toggle-row">
                <div className="cp-toggle-info">
                  <span className="cp-toggle-icon">🎬</span>
                  <span className="cp-toggle-label">自動播放動作</span>
                </div>
                <button
                  className={`cp-toggle ${autoPlayEnabled ? 'active' : ''}`}
                  role="switch" aria-label="自動播放動作" aria-checked={autoPlayEnabled}
                  onClick={toggleAutoPlay}
                  disabled={!modelLoaded}
                >
                  <span className="cp-toggle__thumb" />
                </button>
              </div>

            </div>
          )}
        </div>

        {/* ━━ 眨眼控制 ━━ */}
        <div className="ctrl-section">
          {sectionHeader('blink')}
          {!collapsed.blink && (
            <div className="ctrl-section__content">
              {/* 狀態指示 */}
              <div className="cp-blink-status">
                <span className={`cp-blink-dot ${blinkPaused ? 'paused' : 'active'}`} />
                <span className="cp-blink-label">{blinkPaused ? '暫停中' : '自動眨眼運行中'}</span>
              </div>

              {/* 控制按鈕 */}
              <div className="cp-blink-buttons">
                <button className="cp-blink-btn" onClick={handleForceBlink} title="立即眨眼一次">
                  👁 強制眨眼
                </button>
                <button className="cp-blink-btn" onClick={handlePauseBlink} disabled={blinkPaused} title="暫停自動眨眼3秒">
                  ⏸ 暫停
                </button>
                <button className="cp-blink-btn" onClick={handleResumeBlink} disabled={!blinkPaused} title="恢復自動眨眼">
                  ▶ 恢復
                </button>
              </div>

              {/* 間隔調整 */}
              <div className="cp-blink-interval">
                <span className="cp-blink-interval-label">間隔：</span>
                <button className={`cp-blink-int-chip ${blinkInterval.min === 0.8 && blinkInterval.max === 1.5 ? 'active' : ''}`} onClick={() => handleSetBlinkInterval(0.8, 1.5)}>
                  快
                </button>
                <button className={`cp-blink-int-chip ${blinkInterval.min === 1.0 && blinkInterval.max === 4.0 ? 'active' : ''}`} onClick={() => handleSetBlinkInterval(1.0, 4.0)}>
                  正常
                </button>
                <button className={`cp-blink-int-chip ${blinkInterval.min === 2.0 && blinkInterval.max === 6.0 ? 'active' : ''}`} onClick={() => handleSetBlinkInterval(2.0, 6.0)}>
                  慢
                </button>
              </div>
              <div className="cp-hint">目前：{blinkInterval.min.toFixed(1)}s ~ {blinkInterval.max.toFixed(1)}s</div>
            </div>
          )}
        </div>

        {/* ━━ 模型調整 ━━ */}
        <div className="ctrl-section">
          {sectionHeader('transform')}
          {!collapsed.transform && (
            <div className="ctrl-section__content">

              {/* Hit Area */}
              <div className="cp-toggle-row">
                <div className="cp-toggle-info">
                  <span className="cp-toggle-icon">🎯</span>
                  <span className="cp-toggle-label">點擊區域顯示</span>
                </div>
                <button
                  className={`cp-toggle ${hitAreaDebug ? 'active' : ''}`}
                  role="switch" aria-label="點擊區域顯示" aria-checked={hitAreaDebug}
                  onClick={toggleHitAreaDebug}
                  disabled={!modelLoaded}
                >
                  <span className="cp-toggle__thumb" />
                </button>
              </div>

              <div className="cp-toggle-row">
                <span className="cp-toggle-label">允許拖動構圖</span>
                <button className={`cp-toggle ${modelDragEnabled ? 'active' : ''}`}
                  onClick={toggleModelDrag} disabled={!modelLoaded}
                  role="switch" aria-label="允許拖動構圖" aria-checked={modelDragEnabled}>
                  <span className="cp-toggle__thumb" />
                </button>
              </div>
              <div className="cp-hint cp-hint--info">{modelDragEnabled ? '拖動模型調整位置；重置可回到半身構圖。' : '構圖已鎖定，對話時保持穩定。'}</div>

              {/* 縮放 */}
              <div className="cp-scale-row">
                <span className="cp-scale-row__label">縮放</span>
                <div className="cp-scale-row__controls">
                  <button
                    className="cp-scale-btn"
                    onClick={scaleModelDown}
                    disabled={!modelLoaded || modelScale <= 0.75}
                  >−</button>
                  <span className="cp-scale-row__val">{Math.round(modelScale * 100)}%</span>
                  <button
                    className="cp-scale-btn"
                    onClick={scaleModelUp}
                    disabled={!modelLoaded || modelScale >= 1.25}
                  >+</button>
                  <button
                    className="cp-scale-btn cp-scale-btn--reset"
                    onClick={resetModelTransform}
                    disabled={!modelLoaded}
                  >↺</button>
                </div>
              </div>

            </div>
          )}
        </div>

        {/* ━━ OBS 設定 ━━ */}
        <div className="ctrl-section">
          {sectionHeader('background')}
          {!collapsed.background && (
            <div className="ctrl-section__content">

              {/* OBS URL */}
              <div className="cp-obs-url">
                <span className="cp-obs-url__label">Browser Source URL</span>
                <div className="cp-obs-url__row">
                  <code className="cp-obs-url__code">{window.location.origin}/display</code>
                  <button
                    className="cp-btn cp-btn--sm"
                    onClick={() => navigator.clipboard?.writeText(`${window.location.origin}/display`)}
                  >複製</button>
                </div>
              </div>

              {/* 解析度 */}
              <div className="cp-field">
                <label className="cp-field__label">輸出解析度</label>
                <div className="cp-chip-group">
                  {PRESETS.map(p => (
                    <button
                      key={`${p.w}x${p.h}`}
                      className={`cp-chip ${outputWidth === p.w && outputHeight === p.h ? 'active' : ''}`}
                      onClick={() => { setOutputResolution(p.w, p.h); setCustomW(String(p.w)); setCustomH(String(p.h)); }}
                    >{p.label}</button>
                  ))}
                  <button
                    className={`cp-chip ${isCustomResolution ? 'active' : ''}`}
                  >自訂</button>
                </div>
                <div className="cp-res-custom">
                  <input
                    className="cp-input cp-input--sm"
                    type="number" value={customW} min={320} max={7680}
                    onChange={e => setCustomW(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && handleApplyCustomResolution()}
                    placeholder="寬"
                  />
                  <span className="cp-res-sep">×</span>
                  <input
                    className="cp-input cp-input--sm"
                    type="number" value={customH} min={240} max={4320}
                    onChange={e => setCustomH(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && handleApplyCustomResolution()}
                    placeholder="高"
                  />
                  <button className="cp-btn cp-btn--sm" onClick={handleApplyCustomResolution}>套用</button>
                </div>
                <div className="cp-hint">目前：{outputWidth} × {outputHeight} px</div>
              </div>

              {/* 背景類型 */}
              <div className="cp-field">
                <label className="cp-field__label">背景類型</label>
                <div className="cp-chip-group">
                  {(['transparent', 'color', 'image'] as BackgroundType[]).map(t => (
                    <button
                      key={t}
                      className={`cp-chip ${backgroundType === t ? 'active' : ''}`}
                      onClick={() => setBackgroundType(t)}
                    >
                      {t === 'transparent' ? '透明' : t === 'color' ? '純色' : '圖片'}
                    </button>
                  ))}
                </div>
              </div>

              {/* 純色 */}
              {backgroundType === 'color' && (
                <div className="cp-color-row">
                  <input
                    type="color" value={backgroundColor}
                    onChange={e => setBackgroundColor(e.target.value)}
                    className="cp-color-picker"
                  />
                  <code className="cp-color-code">{backgroundColor}</code>
                  <button className="cp-chip" onClick={() => setBackgroundColor('#00b140')}>綠幕</button>
                  <button className="cp-chip" onClick={() => setBackgroundColor('#0047ab')}>藍幕</button>
                </div>
              )}

              {/* 圖片 */}
              {backgroundType === 'image' && (
                <div className="cp-image-settings">
                  <div className="cp-row">
                    <input
                      className="cp-input"
                      type="text"
                      placeholder="圖片 URL (https://...)"
                      value={imageUrlInput === '[本地圖片]' ? '' : imageUrlInput}
                      onChange={e => setImageUrlInput(e.target.value)}
                      onKeyDown={e => e.key === 'Enter' && handleApplyImageUrl()}
                    />
                    <button className="cp-btn cp-btn--sm" onClick={handleApplyImageUrl}>套用</button>
                  </div>
                  <div className="cp-row">
                    <button className="cp-btn cp-btn--upload" onClick={() => fileInputRef.current?.click()}>
                      ↑ 上傳本地圖片
                    </button>
                    {backgroundImageUrl && <span className="cp-badge cp-badge--success">已設定</span>}
                    <input ref={fileInputRef} type="file" accept="image/*" style={{ display: 'none' }} onChange={handleFileUpload} />
                  </div>
                  <div className="cp-chip-group">
                    {(['cover', 'contain', 'fill'] as BackgroundFit[]).map(f => (
                      <button
                        key={f}
                        className={`cp-chip ${backgroundImageFit === f ? 'active' : ''}`}
                        onClick={() => setBackgroundImageFit(f)}
                      >
                        {f === 'cover' ? '填滿' : f === 'contain' ? '包含' : '拉伸'}
                      </button>
                    ))}
                  </div>
                </div>
              )}

            </div>
          )}
        </div>

        {/* ━━ 動作測試 ━━ */}
        <div className="ctrl-section">
          {sectionHeader('motion')}
          {!collapsed.motion && (
            <div className="ctrl-section__content">
              <NativePreviewPanel apiBaseUrl={debugApiBaseUrl} />
            </div>
          )}
        </div>

      </div>

      {/* ── 底部資訊列 ── */}
      <div className="ctrl-panel__footer">
        <span className="cp-sdk-badge">Live2D SDK 5-r.5-beta.3</span>
      </div>
    </div>
  );
};
