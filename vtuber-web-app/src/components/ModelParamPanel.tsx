import React, { useState, useEffect, useCallback, useRef } from 'react';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import {
  PARAM_DEFS,
  MISSING_PARAMS,
  DEFAULT_PARAMS,
  EYE_SYNC_DEFAULT,
  COVERAGE_INFO,
} from '../generated/tools';
import './ModelParamPanel.css';

type ParamValues = Record<string, number>;

export const ModelParamPanel: React.FC = () => {
  const [params, setParams] = useState<ParamValues>(DEFAULT_PARAMS);
  const [eyeSync, setEyeSync] = useState(EYE_SYNC_DEFAULT);
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
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          if (model && typeof (model as any).getAiParams === 'function') {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const raw = (model as any).getAiParams();
            setParams(prev => ({
              ...prev,
              headIntensity: raw.headIntensity,
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
            }));
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
    if (eyeSync && key === 'browLAngle') next.browRAngle = -value;
    if (eyeSync && key === 'browRAngle') next.browLAngle = -value;
    setParams(next);

    const model = LAppLive2DManager.getInstance().getActiveModel();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if (model && typeof (model as any).setAiBehavior === 'function') {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (model as any).setAiBehavior(
        next.headIntensity, next.blushLevel,
        next.eyeLOpen, next.eyeROpen,
        next.durationSec,
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
    manualTimerRef.current = setTimeout(() => setIsManual(false), 3000);
  }, []);

  const handleReset = useCallback(() => {
    setParams(DEFAULT_PARAMS);
    setIsManual(false);
    const model = LAppLive2DManager.getInstance().getActiveModel();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if (model && typeof (model as any).setAiBehavior === 'function') {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (model as any).setAiBehavior(0, 0, 1, 1, DEFAULT_PARAMS.durationSec, 0, 0, 0, 0, 0, 0, 0, true);
    }
  }, []);

  return (
    <div className="param-panel">
      {/* 標題列 */}
      <div className="param-panel__header">
        <div className="param-panel__header-left">
          <span className="param-panel__title">🎛️ 目前Agent B能啟用的動作工具</span>
          {timerLeft > 0 && <span className="param-panel__timer">⏱ {timerLeft}s</span>}
          {isManual && <span className="param-panel__manual-badge">手動鎖定</span>}
        </div>
        <div className="param-panel__header-right">
          <span className="param-panel__coverage" title="與後端 set_ai_behavior 工具參數對照">
            {COVERAGE_INFO.connected}/{COVERAGE_INFO.total} 已接入
          </span>
          <button className="param-panel__reset-btn" onClick={handleReset}>
            ↺ 預設復原
          </button>
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

      {/* 參數滑桿列表 */}
      <div className="param-panel__body">
        {PARAM_DEFS.map(def => {
          const val = params[def.key] ?? 0;
          const pct = def.min < 0
            ? ((val - def.min) / (def.max - def.min)) * 100
            : (val / def.max) * 100;

          return (
            <div key={def.key} className="param-row" title={`後端欄位: ${def.backendKey}`}>
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

      {/* 缺失參數提示區 */}
      {MISSING_PARAMS.length > 0 && (
        <div className="param-panel__missing">
          <div className="param-panel__missing-title">⚠️ 後端有定義但前端尚未接入</div>
          <div className="param-panel__missing-list">
            {MISSING_PARAMS.map(p => (
              <div key={p.backendKey} className="param-panel__missing-item" title={p.reason}>
                <span className="param-panel__missing-emoji">{p.emoji}</span>
                <span className="param-panel__missing-label">{p.label}</span>
                <code className="param-panel__missing-key">{p.backendKey}</code>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};
