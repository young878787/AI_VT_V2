import React, { useState, useEffect, useCallback, useRef } from 'react';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import { useAppStore } from '@store/appStore';
import './ModelParamPanel.css';

interface ParamDef {
  key: string;
  label: string;
  min: number;
  max: number;
  step: number;
  emoji: string;
  color: string;
}

const PARAM_DEFS: ParamDef[] = [
  { key: 'blushLevel',    label: '臉紅',     min: 0,  max: 1,  step: 0.01, emoji: '🌸', color: '#f9a8d4' },
  { key: 'eyeLOpen',      label: '左眼',     min: 0,  max: 1,  step: 0.01, emoji: '👁',  color: '#7dd3fc' },
  { key: 'eyeROpen',      label: '右眼',     min: 0,  max: 1,  step: 0.01, emoji: '👁',  color: '#7dd3fc' },
  { key: 'mouthForm',     label: '嘴形',     min: -1, max: 1,  step: 0.01, emoji: '😊', color: '#86efac' },
  { key: 'browLY',        label: '左眉高',   min: -1, max: 1,  step: 0.01, emoji: '⬆',  color: '#fde68a' },
  { key: 'browRY',        label: '右眉高',   min: -1, max: 1,  step: 0.01, emoji: '⬆',  color: '#fde68a' },
  { key: 'browLAngle',    label: '左角度',   min: -1, max: 1,  step: 0.01, emoji: '↗',  color: '#fde68a' },
  { key: 'browRAngle',    label: '右角度',   min: -1, max: 1,  step: 0.01, emoji: '↗',  color: '#fde68a' },
  { key: 'browLForm',     label: '左弧度',   min: -1, max: 1,  step: 0.01, emoji: '〜', color: '#fde68a' },
  { key: 'browRForm',     label: '右弧度',   min: -1, max: 1,  step: 0.01, emoji: '〜', color: '#fde68a' },
  { key: 'headIntensity', label: '頭部動作', min: 0,  max: 1,  step: 0.01, emoji: '🎭', color: '#c4b5fd' },
];

type ParamValues = Record<string, number>;

const DEFAULT_PARAMS: ParamValues = {
  blushLevel: 0, eyeLOpen: 1, eyeROpen: 1, mouthForm: 0,
  browLY: 0, browRY: 0, browLAngle: 0, browRAngle: 0, browLForm: 0, browRForm: 0, headIntensity: 0,
};

export const ModelParamPanel: React.FC = () => {
  const { showControls } = useAppStore();
  const [params, setParams] = useState<ParamValues>(DEFAULT_PARAMS);
  const [eyeSync, setEyeSync] = useState(true);
  const [isManual, setIsManual] = useState(false);
  const [timerLeft, setTimerLeft] = useState(0);
  
  // Throttle 更新：每 100ms 拉一次資料就好，避免 60fps 佔用主執行緒導致滑桿卡死
  useEffect(() => {
    let lastTime = 0;
    let rafId: number;
    
    const tick = (time: number) => {
      if (time - lastTime > 100) { // 10fps
        lastTime = time;
        if (!isManual) {
          const model = LAppLive2DManager.getInstance().getActiveModel();
          if (model && typeof (model as any).getAiParams === 'function') {
            const raw = (model as any).getAiParams();
            setParams({
              blushLevel:    raw.blushLevel,
              eyeLOpen:      raw.eyeLOpen,
              eyeROpen:      raw.eyeROpen,
              mouthForm:     raw.mouthForm,
              browLY:         raw.browLY,
              browRY:         raw.browRY,
              browLAngle:     raw.browLAngle,
              browRAngle:     raw.browRAngle,
              browLForm:      raw.browLForm,
              browRForm:      raw.browRForm,
              headIntensity: raw.headIntensity,
            });
            setEyeSync(raw.eyeSync);
            setTimerLeft(Math.max(0, Math.round(raw.timerRemaining)));
          }
        }
      }
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [isManual]);

  const handleSliderChange = useCallback((key: string, value: number) => {
    const next = { ...params, [key]: value };
    if (eyeSync && key === 'eyeLOpen') next.eyeROpen = value;
    if (eyeSync && key === 'eyeROpen') next.eyeLOpen = value;
    if (eyeSync && key === 'browLY') next.browRY = value;
    if (eyeSync && key === 'browRY') next.browLY = value;
    if (eyeSync && key === 'browLForm') next.browRForm = value;
    if (eyeSync && key === 'browRForm') next.browLForm = value;
    // Angle needs mirroring for some models, but we handle it linearly in the setAiBehavior explicitly for now
    if (eyeSync && key === 'browLAngle') next.browRAngle = -value;
    if (eyeSync && key === 'browRAngle') next.browLAngle = -value;
    setParams(next);

    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model && typeof (model as any).setAiBehavior === 'function') {
      // 在手動模式下，強制 AI 接管長達 8 秒，這樣即使使用者鬆手，表情也還在
      // 直到時間被模型跑完後才會 Lerp 退回去。
      (model as any).setAiBehavior(
        next.headIntensity, next.blushLevel,
        next.eyeLOpen, next.eyeROpen,
        8.0,
        next.mouthForm, 
        next.browLY, next.browRY, 
        next.browLAngle, next.browRAngle, 
        next.browLForm, next.browRForm,
        eyeSync
      );
    }
  }, [params, eyeSync]);

  const manualTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const startManual = useCallback(() => {
    setIsManual(true);
    if (manualTimerRef.current) clearTimeout(manualTimerRef.current);
  }, []);

  const endManual = useCallback(() => {
    if (manualTimerRef.current) clearTimeout(manualTimerRef.current);
    // 延遲 3 秒後回到自動輪詢，避免拉完立刻跳動
    manualTimerRef.current = setTimeout(() => setIsManual(false), 3000);
  }, []);

  const handleReset = useCallback(() => {
    setParams(DEFAULT_PARAMS);
    setIsManual(false);
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model && typeof (model as any).setAiBehavior === 'function') {
      (model as any).setAiBehavior(0, 0, 1, 1, 0.5, 0, 0, 0, 0, 0, 0, 0, true);
    }
  }, []);

  if (!showControls) return null;

  return (
    <div className="param-panel">
      {/* 標題列 */}
      <div className="param-panel__header">
        <div className="param-panel__header-left">
          <span className="param-panel__title">🎛️ 即時表情參數</span>
          {timerLeft > 0 && <span className="param-panel__timer">⏱ {timerLeft}s</span>}
          {isManual && <span className="param-panel__manual-badge">手動鎖定</span>}
        </div>
        <div className="param-panel__header-right">
          <label className="param-panel__sync-label">
            <input
              type="checkbox"
              checked={eyeSync}
              onChange={e => setEyeSync(e.target.checked)}
              className="param-panel__sync-checkbox"
            />
            左右同步
          </label>
        </div>
      </div>

      {/* 直向滑桿列表 */}
      <div className="param-panel__body">
        {PARAM_DEFS.map(def => {
          const val = params[def.key] ?? 0;
          const pct = def.min < 0
            ? ((val - def.min) / (def.max - def.min)) * 100
            : (val / def.max) * 100;

          return (
            <div key={def.key} className="param-row">
              <div className="param-row__left">
                <span className="param-row__emoji">{def.emoji}</span>
                <span className="param-row__label">{def.label}</span>
                <span className="param-row__value" style={{ color: def.color }}>
                  {val >= 0 && def.min >= 0 ? '' : (val >= 0 ? '+' : '')}{val.toFixed(2)}
                </span>
              </div>
              <div className="param-row__track">
                {def.min < 0 && <div className="param-row__center" />}
                <input
                  type="range"
                  min={def.min}
                  max={def.max}
                  step={def.step}
                  value={val}
                  onPointerDown={startManual}
                  onPointerUp={endManual}
                  onChange={e => handleSliderChange(def.key, parseFloat(e.target.value))}
                  className="param-row__slider"
                  style={{
                    '--fill': def.color,
                    '--pct': `${pct}%`,
                  } as React.CSSProperties}
                />
              </div>
            </div>
          );
        })}
      </div>
      
      {/* 底部功能列 */}
      <div className="param-panel__footer">
        <button className="param-panel__reset-btn" onClick={handleReset}>
          ↺ 預設復原
        </button>
      </div>
    </div>
  );
};
