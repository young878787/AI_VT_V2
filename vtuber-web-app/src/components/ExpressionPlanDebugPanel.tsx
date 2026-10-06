import { useEffect, useRef, useState } from 'react';
import type { DebugExpressionIntensity } from '../dev/expressionPlanDebugFixtures';
import {
  compileDebugExpressionPlan, fetchExpressionDebugCatalog,
  type CompileExpressionPlanRequest, type ExpressionDebugCatalog,
  type StudioExpressionKind, type StudioExpressionPlan,
} from '../services/expressionDebugService';
import { actionScheduler } from '../services/actionScheduler';
import { useActionPlaybackState } from '../services/useActionPlaybackState';
import { useAppStore } from '../store/appStore';
import { isExpressionPlanPayload } from '../types/expressionPlan';
import { ActionPlaybackStatus } from './ActionPlaybackStatus';
import './ExpressionPlanDebugPanel.css';

type PreviewRequest = Omit<CompileExpressionPlanRequest, 'modelName' | 'intensity' | 'seed' | 'previousState'>;
type Section = 'expression' | 'motion' | 'eyes' | 'idle';
interface Preview {
  label: string;
  plan: StudioExpressionPlan;
  request: PreviewRequest;
  seed: number;
  intensity: DebugExpressionIntensity;
  actionId: string;
}
const SECTIONS: Array<{ id: Section; label: string }> = [
  { id: 'expression', label: '表情' }, { id: 'motion', label: '身體動作' },
  { id: 'eyes', label: '眼神・眨眼' }, { id: 'idle', label: '序列・待機' },
];

export const ExpressionPlanDebugPanel = ({ apiBaseUrl }: { apiBaseUrl?: string }) => {
  const modelLoaded = useAppStore(state => state.modelLoaded);
  const currentModelName = useAppStore(state => state.currentModelName);
  const playback = useActionPlaybackState();
  const [catalog, setCatalog] = useState<ExpressionDebugCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogRetry, setCatalogRetry] = useState(0);
  const [section, setSection] = useState<Section>('expression');
  const [family, setFamily] = useState<StudioExpressionKind>('calm');
  const [variant, setVariant] = useState('');
  const [eyeStyle, setEyeStyle] = useState('');
  const [blinkStyle, setBlinkStyle] = useState('');
  const [intensity, setIntensity] = useState<DebugExpressionIntensity>('normal');
  const [seed, setSeed] = useState(7);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [selection, setSelection] = useState<{ request: PreviewRequest; label: string } | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [isCompiling, setIsCompiling] = useState(false);
  const inFlight = useRef(false);
  const requestVersion = useRef(0);

  useEffect(() => {
    let active = true;
    void fetchExpressionDebugCatalog(apiBaseUrl).then(result => {
      if (active) {
        setCatalog(result);
        setCatalogError(null);
        setFamily(current => result.expressionFamilies.some(item => item.id === current) ? current : 'calm');
        setVariant('');
      }
    }).catch(error => {
      if (active) setCatalogError(error instanceof Error ? error.message : '無法載入表情清單。');
    });
    return () => { active = false; };
  }, [apiBaseUrl, catalogRetry]);
  useEffect(() => () => { requestVersion.current += 1; }, []);

  const familyRequest = (nextFamily = family, nextVariant = variant): PreviewRequest => ({
    kind: nextFamily,
    ...(nextVariant ? { expressionVariant: nextVariant } : {}),
    ...(eyeStyle ? { eyeMotionStyle: eyeStyle } : {}),
    ...(blinkStyle ? { blinkStyle } : {}),
  });

  const compile = async (request: PreviewRequest, label: string, nextSeed = seed, vary = false) => {
    if (inFlight.current || !modelLoaded) return;
    inFlight.current = true;
    const version = ++requestVersion.current;
    const schedulerRevision = actionScheduler.getPreviewRevision();
    setSelection({ request, label });
    setIsCompiling(true);
    setLastError(null);
    try {
      const response = await compileDebugExpressionPlan({
        modelName: currentModelName, ...request, intensity, seed: nextSeed,
        ...(vary && preview?.plan.carryState ? { previousState: preview.plan.carryState } : {}),
      }, apiBaseUrl);
      if (version !== requestVersion.current || !useAppStore.getState().modelLoaded
        || schedulerRevision !== actionScheduler.getPreviewRevision()) return;
      if (!isExpressionPlanPayload(response.plan)) throw new Error('表情資料格式不完整，請核對前後端版本。');
      const plan = response.plan;
      const checks = [
        [request.expressionVariant, plan.debug?.expressionVariant],
        [request.motionKind, plan.motionPlan?.variant],
        [request.eyeMotionStyle, plan.eyeMotionPlan?.style],
        [request.blinkStyle, plan.blinkPlan.style],
        [request.idleStyle, plan.idlePlan?.name],
      ];
      for (const [requested, resolved] of checks) {
        if (requested && requested !== resolved) throw new Error(`要求 ${requested}，實際編譯為 ${resolved ?? '未提供'}，已停止播放。`);
      }
      const actionId = actionScheduler.submit(structuredClone(plan), 'debug');
      setPreview({ label, plan, request, seed: nextSeed, intensity, actionId });
      setSeed(nextSeed);
    } catch (error) {
      if (version === requestVersion.current) setLastError(error instanceof Error ? error.message : '目前無法產生表情。');
    } finally {
      if (version === requestVersion.current) { inFlight.current = false; setIsCompiling(false); }
    }
  };
  const stop = () => {
    requestVersion.current += 1;
    inFlight.current = false;
    setIsCompiling(false);
    actionScheduler.stopPreview();
  };
  const replay = () => {
    if (!preview || !modelLoaded) return;
    const actionId = actionScheduler.submit(structuredClone(preview.plan), 'debug');
    setPreview({ ...preview, actionId });
    setLastError(null);
  };

  const disabled = isCompiling || !modelLoaded || !catalog;
  const selectedFamily = catalog?.expressionFamilies.find(item => item.id === family);
  const variantCount = catalog?.expressionFamilies.reduce((total, item) => total + item.variants.length, 0) ?? 0;
  const params = preview?.plan.basePose.params;
  const plan = preview?.plan;
  let sequenceStart = 0;
  const sequence = plan?.sequence.map((event, index) => {
    const startMs = sequenceStart;
    const endMs = startMs + event.durationMs;
    sequenceStart += Math.max(1, event.durationMs - Math.min(event.fadeOutMs ?? 0, plan.sequence[index + 1]?.fadeInMs ?? 0));
    return { event, startMs, endMs };
  }) ?? [];
  const fullDurationMs = plan ? Math.max(plan.basePose.durationSec * 1000,
    ...sequence.map(item => item.endMs), ...plan.microEvents.map(event => event.durationMs),
    plan.motionPlan ? plan.motionPlan.durationMs + plan.motionPlan.blendOutMs : 0,
    plan.eyeMotionPlan ? plan.eyeMotionPlan.durationMs + plan.eyeMotionPlan.blendOutMs : 0,
    plan.idlePlan?.enterAfterMs ?? 0) : 0;
  const isCurrentPreview = preview?.actionId === playback.id && playback.kind === 'procedural';

  return (
    <div className="expression-debug-panel">
      <div className="expression-debug-panel__intro">
        <p>Rushia 表情與動作工作室</p>
        <span>{catalog ? `${catalog.expressionFamilies.length} 個家族・${variantCount} 個表情變體・${catalog.motions.length} 個身體動作` : '正在讀取可用表情清單…'}</span>
      </div>
      <div className="expression-debug-panel__body">
        {catalogError && <div className="expression-debug-panel__catalog-error" role="alert">{catalogError}
          <button type="button" onClick={() => setCatalogRetry(value => value + 1)}>重新載入清單</button>
        </div>}
        <div className="expression-debug-panel__controls">
          <span>表現強度</span>
          <div className="expression-debug-panel__segmented" role="group" aria-label="表現強度">
            {(['soft', 'normal', 'strong'] as const).map(value => <button key={value} type="button" aria-pressed={intensity === value}
              className={`expression-debug-panel__segment ${intensity === value ? 'expression-debug-panel__segment--active' : ''}`}
              disabled={isCompiling} onClick={() => setIntensity(value)}>{value === 'soft' ? '輕柔' : value === 'normal' ? '自然' : '鮮明'}</button>)}
          </div>
        </div>
        <div className="expression-debug-panel__tabs" role="group" aria-label="動作分類">
          {SECTIONS.map(item => <button key={item.id} type="button" aria-pressed={section === item.id}
            className={section === item.id ? 'active' : ''} onClick={() => setSection(item.id)}>{item.label}</button>)}
        </div>
        {catalog && section === 'expression' && <section className="expression-debug-panel__section" aria-label="完整程序化表情">
          <h3>表情家族 · {catalog.expressionFamilies.length}</h3>
          <div className="expression-debug-panel__preset-grid">
            {catalog.expressionFamilies.map(item => <button key={item.id} type="button" disabled={disabled} aria-pressed={family === item.id}
              className={`expression-debug-panel__emotion-btn ${family === item.id ? 'expression-debug-panel__emotion-btn--active' : ''}`}
              onClick={() => { setFamily(item.id); setVariant(''); void compile(familyRequest(item.id, ''), item.label); }}>{item.label}</button>)}
          </div>
          <div className="expression-debug-panel__variant-heading"><h3>{selectedFamily?.label} · 指定變體</h3><code>{family}</code></div>
          <div className="expression-debug-panel__item-grid">
            {selectedFamily?.variants.map(item => <button key={item.id} type="button" disabled={disabled} aria-pressed={variant === item.id}
              className={`expression-debug-panel__item ${variant === item.id ? 'active' : ''}`}
              onClick={() => { setVariant(item.id); void compile(familyRequest(family, item.id), `${selectedFamily.label} · ${item.label}`); }}>
              <strong>{item.label}</strong><code>{item.id}</code>
            </button>)}
          </div>
        </section>}
        {catalog && section === 'motion' && <section className="expression-debug-panel__section" aria-label="完整程序化身體動作">
          <h3>身體動作 · {catalog.motions.length}</h3>
          <div className="expression-debug-panel__item-grid">{catalog.motions.map(item => <button key={item.id} type="button" disabled={disabled}
            className={`expression-debug-panel__item ${preview?.request.motionKind === item.id ? 'active' : ''}`}
            onClick={() => void compile({ ...familyRequest(item.expressionKind, ''), motionKind: item.id }, item.label)}>
            <strong>{item.label}</strong><code>{item.id}</code><span>{item.theme} · {item.expressionKind}</span>
          </button>)}</div>
        </section>}
        {catalog && section === 'eyes' && <section className="expression-debug-panel__section" aria-label="眼神與眨眼策略">
          <label className="expression-debug-panel__field">搭配表情家族<select value={family} disabled={disabled}
            onChange={event => {
              const nextFamily = event.target.value as StudioExpressionKind;
              setFamily(nextFamily); setVariant('');
              setSelection({ request: familyRequest(nextFamily, ''), label: catalog.expressionFamilies.find(item => item.id === nextFamily)?.label ?? nextFamily });
            }}>
            {catalog.expressionFamilies.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select></label>
          <h3>眼神 · {catalog.eyeStyles.length}</h3>
          <div className="expression-debug-panel__item-grid">{catalog.eyeStyles.map(item => <button key={item.id} type="button" disabled={disabled}
            className={`expression-debug-panel__item ${eyeStyle === item.id ? 'active' : ''}`}
            onClick={() => { setEyeStyle(item.id); void compile({ ...familyRequest(), eyeMotionStyle: item.id }, item.label); }}>
            <strong>{item.label}</strong><code>{item.id}</code>
          </button>)}</div>
          <h3 className="expression-debug-panel__subheading">眨眼 · {catalog.blinkStyles.length}</h3>
          <div className="expression-debug-panel__item-grid">{catalog.blinkStyles.map(item => <button key={item.id} type="button" disabled={disabled}
            className={`expression-debug-panel__item ${blinkStyle === item.id ? 'active' : ''}`}
            onClick={() => { setBlinkStyle(item.id); void compile({ ...familyRequest(), blinkStyle: item.id }, item.label); }}>
            <strong>{item.label}</strong><code>{item.id}</code>
          </button>)}</div>
          <button className="expression-debug-panel__action-btn" type="button" disabled={disabled}
            onClick={() => { setEyeStyle(''); setBlinkStyle(''); void compile({ kind: family, blinkStyle: 'normal' }, '恢復自然眼神與眨眼'); }}>恢復自然眼神與眨眼</button>
        </section>}
        {catalog && section === 'idle' && <section className="expression-debug-panel__section" aria-label="序列與待機入口">
          <h3>收尾待機 · {catalog.idleStyles.length}</h3>
          <div className="expression-debug-panel__item-grid">{catalog.idleStyles.map(item => <button key={item.id} type="button" disabled={disabled}
            className={`expression-debug-panel__item ${preview?.request.idleStyle === item.id ? 'active' : ''}`}
            onClick={() => void compile({ ...familyRequest(item.family, ''), idleStyle: item.id }, item.label)}>
            <strong>{item.label}</strong><code>{item.id}</code><span>搭配 {item.family} · 演出後進入循環</span>
          </button>)}</div>
          <h3 className="expression-debug-panel__subheading">Rushia 序列組合</h3>
          <div className="expression-debug-panel__item-grid">{catalog.scenarios.map(item => <button key={item.id} type="button" disabled={disabled}
            className="expression-debug-panel__item" onClick={() => void compile({ scenario: item.id }, item.label)}>
            <strong>{item.label}</strong><code>{item.id}</code><span>{item.description}</span>
          </button>)}</div>
        </section>}
        <div className="expression-debug-panel__playback" aria-label="統一預覽控制">
          <button type="button" className="expression-debug-panel__action-btn expression-debug-panel__action-btn--primary" disabled={disabled}
            onClick={() => void compile(selection?.request ?? familyRequest(), selection?.label ?? selectedFamily?.label ?? family)}>播放選定項目</button>
          <button type="button" className="expression-debug-panel__action-btn" disabled={disabled || !preview} onClick={replay}>固定重播</button>
          <button type="button" className="expression-debug-panel__action-btn" disabled={!modelLoaded} onClick={stop}>停止</button>
          <button type="button" className="expression-debug-panel__reset-btn" disabled={disabled}
            onClick={() => { setFamily('calm'); setVariant(''); setEyeStyle(''); setBlinkStyle(''); void compile({ kind: 'calm', blinkStyle: 'normal' }, '平靜'); }}>回到平靜</button>
        </div>
        <div className="expression-debug-panel__playback-status" role="status">
          {!modelLoaded ? '等待角色載入' : isCompiling ? '編譯中… 尚未開始播放' : lastError ? '編譯失敗' : preview ? `已編譯 · ${preview.label} · seed ${preview.seed}` : '點選項目，即可編譯並播放。'}
        </div>
        <ActionPlaybackStatus state={playback} />
        {preview && plan && params && <section className="expression-debug-panel__summary" aria-label="編譯結果與演出序列">
          <div className="expression-debug-panel__summary-heading"><h3>這次編譯結果</h3><span>{plan.idlePlan ? '演出至待機' : '完整演出'} {(fullDurationMs / 1000).toFixed(2)} 秒</span></div>
          <div className="expression-debug-panel__summary-row"><span>家族／要求變體</span><strong>{String(plan.debug?.expressionFamily ?? plan.basePose.preset)} / {preview.request.expressionVariant ?? '自動選擇'}</strong></div>
          <div className="expression-debug-panel__summary-row"><span>實際表情變體</span><strong>{String(plan.debug?.expressionVariant ?? '—')}</strong></div>
          <div className="expression-debug-panel__summary-row"><span>實際身體動作</span><strong>{plan.motionPlan?.variant ?? '未使用'}</strong></div>
          <div className="expression-debug-panel__summary-row"><span>眼神／眨眼</span><strong>{plan.eyeMotionPlan?.style ?? '未使用'} / {plan.blinkPlan.style}</strong></div>
          {plan.eyeMotionPlan && <div className="expression-debug-panel__summary-row"><span>眼神幅度 X／Y</span><strong>{plan.eyeMotionPlan.amplitudeX.toFixed(3)} / {plan.eyeMotionPlan.amplitudeY.toFixed(3)}</strong></div>}
          <div className="expression-debug-panel__summary-row"><span>基準姿態持續</span><strong>{plan.basePose.durationSec.toFixed(2)} 秒</strong></div>
          <div className="expression-debug-panel__summary-row"><span>眼睛開合 左／右</span><strong>{params.eyeLOpen.toFixed(2)} / {params.eyeROpen.toFixed(2)}</strong></div>
          <div className="expression-debug-panel__summary-row"><span>眉高 左／右・嘴形・臉紅</span><strong>{params.browLY.toFixed(2)} / {params.browRY.toFixed(2)} · {params.mouthForm.toFixed(2)} · {params.blushLevel.toFixed(2)}</strong></div>
          <button type="button" className="expression-debug-panel__action-btn" disabled={disabled}
            onClick={() => void compile(preview.request, preview.label, seed + 1, true)}>調整演出細節</button>
          {sequence.length > 0 && <details className="expression-debug-panel__details" open><summary>演出序列 · {sequence.length} 個片段</summary>
            <ol>{sequence.map(({ event, startMs, endMs }, index) => <li key={`${event.kind}-${index}`}
              className={isCurrentPreview && playback.elapsedMs >= startMs && playback.elapsedMs < endMs ? 'active' : ''}>
              <div><strong>{event.kind}</strong><span>{Object.keys(event.patch).join(' · ') || '銜接間隔'}</span></div>
              <span>{(startMs / 1000).toFixed(2)}–{(endMs / 1000).toFixed(2)} 秒</span>
            </li>)}</ol>
          </details>}
          {plan.idlePlan && <details className="expression-debug-panel__details" open><summary>收尾・{plan.idlePlan.name}</summary>
            <p>{(plan.idlePlan.enterAfterMs / 1000).toFixed(2)} 秒後進入收尾；待機每 {(plan.idlePlan.loopIntervalMs / 1000).toFixed(2)} 秒循環，可用停止或新演出中斷。</p>
            <p>收尾姿態：{plan.idlePlan.settlePose.preset}</p>
            <ol>{plan.idlePlan.loopEvents.map((event, index) => <li key={`${event.kind}-${index}`}><span>{event.kind}</span><span>{event.durationMs} ms</span></li>)}</ol>
          </details>}
        </section>}
      </div>
      {lastError && <div className="expression-debug-panel__error" role="alert">{lastError}</div>}
    </div>
  );
};
