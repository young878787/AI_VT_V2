import { useRef, useState, type CSSProperties } from 'react';
import { EXPRESSION_DEBUG_PRESETS, MOTION_DEBUG_PRESETS, type DebugExpressionIntensity } from '../dev/expressionPlanDebugFixtures';
import {
  compileDebugExpressionPlan,
  type CompileExpressionPlanRequest,
  type StudioExpressionKind,
  type StudioExpressionPlan,
} from '../services/expressionDebugService';
import { actionScheduler } from '../services/actionScheduler';
import { useAppStore } from '../store/appStore';
import { isExpressionPlanPayload } from '../types/expressionPlan';
import './ExpressionPlanDebugPanel.css';

const EVERYDAY_PRESETS: Array<{ kind: StudioExpressionKind; label: string; description: string }> = [
  { kind: 'calm', label: '平靜', description: '放鬆地陪在身邊' },
  { kind: 'listening', label: '專注聆聽', description: '眼神與輕微點頭' },
  { kind: 'thinking', label: '思考', description: '短暫移開視線' },
  { kind: 'soft_smile', label: '柔和微笑', description: '低強度的溫柔回應' },
  { kind: 'closed_smile', label: '閉眼笑', description: '短暫笑眼再自然睜開' },
];

type PreviewRequest = Omit<CompileExpressionPlanRequest, 'modelName' | 'intensity' | 'seed' | 'previousState'>;
interface Preview {
  label: string;
  plan: StudioExpressionPlan;
  request: PreviewRequest;
  seed: number;
  intensity: DebugExpressionIntensity;
}

export const ExpressionPlanDebugPanel = () => {
  const modelLoaded = useAppStore(state => state.modelLoaded);
  const currentModelName = useAppStore(state => state.currentModelName);
  const [intensity, setIntensity] = useState<DebugExpressionIntensity>('normal');
  const [seed, setSeed] = useState(7);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [isCompiling, setIsCompiling] = useState(false);
  const inFlight = useRef(false);

  const compile = async (request: PreviewRequest, label: string, nextSeed = seed, vary = false) => {
    if (inFlight.current || !modelLoaded) return;
    inFlight.current = true;
    setIsCompiling(true);
    setLastError(null);
    try {
      const response = await compileDebugExpressionPlan({
        modelName: currentModelName,
        ...request,
        intensity,
        seed: nextSeed,
        ...(vary && preview?.plan.carryState ? { previousState: preview.plan.carryState } : {}),
      });
      if (!isExpressionPlanPayload(response.plan)) throw new Error('表情資料格式不完整，請檢查後端與前端版本。');
      const next = { label, plan: response.plan, request, seed: nextSeed, intensity };
      setPreview(next);
      setSeed(nextSeed);
      actionScheduler.submit(structuredClone(response.plan), 'debug');
    } catch (error) {
      setLastError(error instanceof Error ? error.message : '目前無法產生表情，請稍後重試。');
    } finally {
      inFlight.current = false;
      setIsCompiling(false);
    }
  };

  const replay = () => {
    if (!preview || !modelLoaded) return;
    actionScheduler.submit(structuredClone(preview.plan), 'debug');
    setLastError(null);
  };

  const disabled = isCompiling || !modelLoaded;
  const params = preview?.plan.basePose.params;
  const variant = preview?.plan.debug?.expressionVariant ?? preview?.plan.motionPlan?.variant;
  const family = preview?.plan.debug?.expressionFamily ?? preview?.plan.basePose.preset;
  const events = preview?.plan.sequence.filter(event => !event.kind.includes('_gap_')) ?? [];

  return (
    <div className="expression-debug-panel">
      <div className="expression-debug-panel__intro">
        <p>選一個心情，看她如何回應。</p>
        <span>同一演出可重播；換個演法，再比較細微差別。</span>
      </div>
      <div className="expression-debug-panel__body">
        <div className="expression-debug-panel__controls">
          <span>表現強度</span>
          <div className="expression-debug-panel__segmented" role="group" aria-label="表現強度">
            {(['soft', 'normal', 'strong'] as const).map(value => (
              <button key={value} type="button" aria-pressed={intensity === value}
                className={`expression-debug-panel__segment ${intensity === value ? 'expression-debug-panel__segment--active' : ''}`}
                disabled={isCompiling} onClick={() => setIntensity(value)}>
                {value === 'soft' ? '輕柔' : value === 'normal' ? '自然' : '鮮明'}
              </button>
            ))}
          </div>
        </div>

        <section className="expression-debug-panel__section" aria-label="日常互動表情">
          <h3>日常互動</h3>
          <div className="expression-debug-panel__everyday-grid">
            {EVERYDAY_PRESETS.map(preset => (
              <button key={preset.kind} type="button" aria-pressed={preview?.request.kind === preset.kind}
                className={`expression-debug-panel__preset-btn ${preview?.request.kind === preset.kind ? 'expression-debug-panel__preset-btn--active' : ''}`}
                onClick={() => void compile({ kind: preset.kind }, preset.label)} disabled={disabled}>
                <span className="expression-debug-panel__preset-label">{preset.label}</span>
                <span className="expression-debug-panel__preset-description">{preset.description}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="expression-debug-panel__section" aria-label="情緒表情">
          <h3>情緒變化</h3>
          <div className="expression-debug-panel__preset-grid">
            {EXPRESSION_DEBUG_PRESETS.map(preset => (
              <button key={preset.kind} type="button" aria-pressed={preview?.request.kind === preset.kind}
                className={`expression-debug-panel__emotion-btn ${preview?.request.kind === preset.kind ? 'expression-debug-panel__emotion-btn--active' : ''}`}
                style={{ '--preset-accent': preset.accent } as CSSProperties}
                disabled={disabled} onClick={() => void compile({ kind: preset.kind }, preset.label)}>
                {preset.label}
              </button>
            ))}
          </div>
        </section>

        <div className="expression-debug-panel__playback" aria-label="表情重播控制">
          <button type="button" className="expression-debug-panel__action-btn expression-debug-panel__action-btn--primary"
            disabled={disabled || !preview} onClick={replay}>重播這次表情</button>
          <button type="button" className="expression-debug-panel__action-btn" disabled={disabled || !preview}
            onClick={() => preview && void compile(preview.request, preview.label, seed + 1, true)}>換個演法</button>
          <button type="button" className="expression-debug-panel__reset-btn" disabled={disabled}
            onClick={() => void compile({ kind: 'calm' }, '平靜')}>回到平靜</button>
        </div>
        <div className="expression-debug-panel__playback-status" role="status">
          {!modelLoaded ? '等待角色載入' : isCompiling ? '準備表情中…' : preview
            ? `${preview.label} · 演出 #${preview.seed} · ${preview.intensity === 'soft' ? '輕柔' : preview.intensity === 'normal' ? '自然' : '鮮明'}`
            : '選擇表情後，可反覆重播與比較。'}
        </div>

        {preview && params && (
          <section className="expression-debug-panel__summary" aria-label="目前表情摘要">
            <div className="expression-debug-panel__summary-heading"><h3>這次演出</h3><span>{preview.plan.basePose.durationSec.toFixed(1)} 秒</span></div>
            <div className="expression-debug-panel__summary-row"><span>家族</span><strong>{String(family)}</strong></div>
            <div className="expression-debug-panel__summary-row"><span>變體</span><strong>{String(variant ?? '—')}</strong></div>
            <div className="expression-debug-panel__summary-row"><span>眼睛開合（左／右）</span><strong>{params.eyeLOpen.toFixed(2)} / {params.eyeROpen.toFixed(2)}</strong></div>
            <div className="expression-debug-panel__summary-row"><span>眉毛高度（左／右）</span><strong>{params.browLY.toFixed(2)} / {params.browRY.toFixed(2)}</strong></div>
            <div className="expression-debug-panel__summary-row"><span>嘴形／臉紅</span><strong>{params.mouthForm.toFixed(2)} / {params.blushLevel.toFixed(2)}</strong></div>
            <div className="expression-debug-panel__summary-row"><span>視線（X／Y）</span><strong>{params.eyeBallX.toFixed(2)} / {params.eyeBallY.toFixed(2)}</strong></div>
            <p className="expression-debug-panel__summary-note">上方為基準姿態；短暫閉眼、點頭等變化接續播放。</p>
            {events.length > 0 && <details className="expression-debug-panel__details"><summary>演出序列 · {events.length} 個片段</summary>
              <ol>{events.map((event, index) => <li key={`${event.kind}-${index}`}><span>{event.kind}</span><span>{event.durationMs} ms</span></li>)}</ol>
            </details>}
          </section>
        )}

        <details className="expression-debug-panel__details">
          <summary>更多動作與組合</summary>
          <div className="expression-debug-panel__motion-grid">
            {MOTION_DEBUG_PRESETS.map(preset => <button key={preset.kind} type="button" className="expression-debug-panel__motion-btn"
              disabled={disabled} onClick={() => void compile({ motionKind: preset.kind }, preset.label)}>{preset.label}</button>)}
            <button type="button" className="expression-debug-panel__motion-btn" disabled={disabled}
              onClick={() => void compile({ scenario: 'brow_eye_micro' }, '眉眼微動')}>眉眼微動</button>
            <button type="button" className="expression-debug-panel__motion-btn" disabled={disabled}
              onClick={() => void compile({ scenario: 'speaking_micro' }, '說話微表情')}>說話微表情</button>
          </div>
        </details>
      </div>
      {lastError && <div className="expression-debug-panel__error" role="alert">{lastError}</div>}
    </div>
  );
};
