import { useMemo, useState, type CSSProperties } from 'react';
import {
  createBackendDebugExpressionIntent,
  createBackendDebugMotionIntent,
  createDebugExpressionPlan,
  createDebugMotionExpressionPlan,
  createNeutralExpressionPlan,
  DEFAULT_DEBUG_EXPRESSION_OPTIONS,
  EXPRESSION_DEBUG_PRESETS,
  MOTION_DEBUG_PRESETS,
  getRandomDebugExpressionKind,
  type DebugMotionKind,
  type DebugExpressionIntensity,
  type DebugExpressionKind,
  type DebugExpressionOptions,
} from '../dev/expressionPlanDebugFixtures';
import { compileDebugExpressionPlan } from '../services/expressionDebugService';
import { useAppStore } from '../store/appStore';
import type { ExpressionPlanPayload } from '../types/expressionPlan';
import { isExpressionPlanPayload } from '../types/expressionPlan';
import './ExpressionPlanDebugPanel.css';

interface AppliedSummary {
  label: string;
  preset: string;
  motionStyle: string;
  motionVariant: string;
  idlePlan: string;
  physicsImpulse: number;
  durationSec: number;
}

type DebugPlanSource = 'mock' | 'backend';

function summarizePlan(label: string, plan: ExpressionPlanPayload): AppliedSummary {
  return {
    label,
    preset: plan.basePose.preset,
    motionStyle: String(plan.basePose.bodyMotionProfile?.style ?? 'calm_sway'),
    motionVariant: plan.motionPlan ? `${plan.motionPlan.theme}:${plan.motionPlan.variant}` : 'none',
    idlePlan: plan.idlePlan?.name ?? 'none',
    physicsImpulse: plan.basePose.params.physicsImpulse,
    durationSec: plan.basePose.durationSec,
  };
}

export const ExpressionPlanDebugPanel = () => {
  const setExpressionPlan = useAppStore((state) => state.setExpressionPlan);
  const setBlinkControl = useAppStore((state) => state.setBlinkControl);
  const currentModelName = useAppStore((state) => state.currentModelName);
  const [options, setOptions] = useState<DebugExpressionOptions>(DEFAULT_DEBUG_EXPRESSION_OPTIONS);
  const [source, setSource] = useState<DebugPlanSource>('mock');
  const [summary, setSummary] = useState<AppliedSummary | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [isCompiling, setIsCompiling] = useState(false);

  const presetByKind = useMemo(
    () => new Map(EXPRESSION_DEBUG_PRESETS.map((preset) => [preset.kind, preset])),
    [],
  );
  const motionPresetGroups = useMemo(
    () => EXPRESSION_DEBUG_PRESETS.map((expressionPreset) => ({
      kind: expressionPreset.kind,
      label: expressionPreset.label,
      motions: MOTION_DEBUG_PRESETS.filter((motionPreset) => motionPreset.expressionKind === expressionPreset.kind),
    })).filter((group) => group.motions.length > 0),
    [],
  );

  const applyPlan = (label: string, plan: ExpressionPlanPayload, planSource: DebugPlanSource) => {
    if (!isExpressionPlanPayload(plan)) {
      setLastError(`${planSource === 'backend' ? 'backend' : 'mock'} expression_plan 格式未通過前端 validator`);
      console.warn('[ExpressionPlanDebug] invalid expression_plan:', plan);
      return;
    }

    setExpressionPlan(plan);
    for (const command of plan.blinkPlan.commands) {
      setBlinkControl(command.action, command.durationSec ?? 0, command.intervalMin, command.intervalMax);
    }

    setSummary(summarizePlan(label, plan));
    setLastError(null);
    console.log('[ExpressionPlanDebug] applied expression_plan', {
      source: planSource,
      label,
      preset: plan.basePose.preset,
      motionStyle: plan.basePose.bodyMotionProfile?.style,
      motionPlan: plan.motionPlan ? `${plan.motionPlan.theme}:${plan.motionPlan.variant}` : 'none',
      idlePlan: plan.idlePlan?.name ?? 'none',
      physicsImpulse: plan.basePose.params.physicsImpulse,
      durationSec: plan.basePose.durationSec,
    });
  };

  const compileBackendPlan = async (kind: DebugExpressionKind): Promise<ExpressionPlanPayload> => {
    const intent = createBackendDebugExpressionIntent(kind, options);
    const response = await compileDebugExpressionPlan({
      modelName: currentModelName || 'Hiyori',
      intent,
    });
    return response.plan;
  };

  const compileBackendMotionPlan = async (kind: DebugMotionKind): Promise<ExpressionPlanPayload> => {
    const intent = createBackendDebugMotionIntent(kind, options);
    const response = await compileDebugExpressionPlan({
      modelName: currentModelName || 'Hiyori',
      intent,
    });
    return response.plan;
  };

  const applyPreset = async (kind: DebugExpressionKind) => {
    const preset = presetByKind.get(kind);
    const label = preset?.label ?? kind;

    if (source === 'mock') {
      applyPlan(label, createDebugExpressionPlan(kind, options), 'mock');
      return;
    }

    setIsCompiling(true);
    setLastError(null);
    try {
      applyPlan(label, await compileBackendPlan(kind), 'backend');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 expression_plan 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const applyRandom = async () => {
    const kind = getRandomDebugExpressionKind();
    const preset = presetByKind.get(kind);
    const label = preset ? `隨機:${preset.label}` : '隨機';

    if (source === 'mock') {
      applyPlan(label, createDebugExpressionPlan(kind, options), 'mock');
      return;
    }

    setIsCompiling(true);
    setLastError(null);
    try {
      applyPlan(label, await compileBackendPlan(kind), 'backend');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 expression_plan 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend random compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const applyMotionPreset = async (kind: DebugMotionKind) => {
    const preset = MOTION_DEBUG_PRESETS.find((item) => item.kind === kind);
    const label = preset ? `動作:${preset.label}` : `動作:${kind}`;

    if (source === 'mock') {
      applyPlan(label, createDebugMotionExpressionPlan(kind, options), 'mock');
      return;
    }

    setIsCompiling(true);
    setLastError(null);
    try {
      applyPlan(label, await compileBackendMotionPlan(kind), 'backend');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 motionPlan 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend motion compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const resetNeutral = () => {
    applyPlan('中性復原', createNeutralExpressionPlan(), 'mock');
  };

  const setIntensity = (intensity: DebugExpressionIntensity) => {
    setOptions((current) => ({ ...current, intensity }));
  };

  const toggleOption = (key: keyof Pick<DebugExpressionOptions, 'includeIdle' | 'includeAmbient' | 'includeMicroEvents'>) => {
    setOptions((current) => ({ ...current, [key]: !current[key] }));
  };

  return (
    <div className="expression-debug-panel">
      <div className="expression-debug-panel__header">
        <div className="expression-debug-panel__header-left">
          <span className="expression-debug-panel__title">Expression Plan 動作測試</span>
          <span className="expression-debug-panel__badge">
            {source === 'backend' ? 'Backend compile' : 'Mock fixture'}
          </span>
        </div>
        <div className="expression-debug-panel__header-actions">
          <button type="button" className="expression-debug-panel__action-btn" onClick={applyRandom} disabled={isCompiling}>
            {isCompiling ? '編譯中' : '隨機測試'}
          </button>
          <button type="button" className="expression-debug-panel__reset-btn" onClick={resetNeutral}>
            中性復原
          </button>
        </div>
      </div>

      <div className="expression-debug-panel__body">
        <div className="expression-debug-panel__main">
          <div className="expression-debug-panel__section">
            <div className="expression-debug-panel__section-title">表情主題</div>
            <div className="expression-debug-panel__preset-grid">
              {EXPRESSION_DEBUG_PRESETS.map((preset) => (
                <button
                  key={preset.kind}
                  type="button"
                  className="expression-debug-panel__preset-btn"
                  style={{ '--preset-accent': preset.accent } as CSSProperties}
                  onClick={() => applyPreset(preset.kind)}
                  disabled={isCompiling}
                  title={`${preset.preset} / ${preset.motionStyle} / ${preset.idleName}`}
                >
                  <span className="expression-debug-panel__preset-label">{preset.label}</span>
                  <span className="expression-debug-panel__preset-meta">{preset.motionStyle}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="expression-debug-panel__section">
            <div className="expression-debug-panel__section-title">分支動作</div>
            {motionPresetGroups.map((group) => (
              <div key={group.kind} className="expression-debug-panel__motion-group">
                <div className="expression-debug-panel__motion-group-title">{group.label}</div>
                <div className="expression-debug-panel__motion-grid">
                  {group.motions.map((preset) => (
                    <button
                      key={preset.kind}
                      type="button"
                      className="expression-debug-panel__motion-btn"
                      style={{ '--preset-accent': preset.accent } as CSSProperties}
                      onClick={() => applyMotionPreset(preset.kind)}
                      disabled={isCompiling}
                      title={`${preset.theme} / ${preset.variant}`}
                    >
                      <span className="expression-debug-panel__preset-label">{preset.label}</span>
                      <span className="expression-debug-panel__preset-meta">{preset.variant}</span>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="expression-debug-panel__side">
          <div className="expression-debug-panel__controls">
            <div className="expression-debug-panel__segmented" aria-label="資料來源">
              {(['mock', 'backend'] as DebugPlanSource[]).map((nextSource) => (
                <button
                  key={nextSource}
                  type="button"
                  className={
                    source === nextSource
                      ? 'expression-debug-panel__segment expression-debug-panel__segment--active'
                      : 'expression-debug-panel__segment'
                  }
                  onClick={() => setSource(nextSource)}
                  disabled={isCompiling}
                >
                  {nextSource === 'mock' ? 'Mock' : 'Backend'}
                </button>
              ))}
            </div>

            <div className="expression-debug-panel__segmented" aria-label="動作強度">
              {(['soft', 'normal', 'strong'] as DebugExpressionIntensity[]).map((intensity) => (
                <button
                  key={intensity}
                  type="button"
                  className={
                    options.intensity === intensity
                      ? 'expression-debug-panel__segment expression-debug-panel__segment--active'
                      : 'expression-debug-panel__segment'
                  }
                  onClick={() => setIntensity(intensity)}
                  disabled={isCompiling}
                >
                  {intensity === 'soft' ? '柔' : intensity === 'normal' ? '中' : '強'}
                </button>
              ))}
            </div>

            <label className="expression-debug-panel__toggle">
              <input
                type="checkbox"
                checked={options.includeMicroEvents}
                onChange={() => toggleOption('includeMicroEvents')}
                disabled={source === 'backend' || isCompiling}
              />
              micro
            </label>
            <label className="expression-debug-panel__toggle">
              <input
                type="checkbox"
                checked={options.includeIdle}
                onChange={() => toggleOption('includeIdle')}
                disabled={source === 'backend' || isCompiling}
              />
              idle
            </label>
            <label className="expression-debug-panel__toggle">
              <input
                type="checkbox"
                checked={options.includeAmbient}
                onChange={() => toggleOption('includeAmbient')}
                disabled={source === 'backend' || isCompiling || !options.includeIdle}
              />
              ambient
            </label>
          </div>

          <div className="expression-debug-panel__summary">
            {summary ? (
              <>
                <div className="expression-debug-panel__summary-title">{summary.label}</div>
                <div className="expression-debug-panel__summary-row">
                  <span>preset</span>
                  <strong>{summary.preset}</strong>
                </div>
                <div className="expression-debug-panel__summary-row">
                  <span>motion</span>
                  <strong>{summary.motionStyle}</strong>
                </div>
                <div className="expression-debug-panel__summary-row">
                  <span>variant</span>
                  <strong>{summary.motionVariant}</strong>
                </div>
                <div className="expression-debug-panel__summary-row">
                  <span>idle</span>
                  <strong>{summary.idlePlan}</strong>
                </div>
                <div className="expression-debug-panel__summary-row">
                  <span>impulse</span>
                  <strong>{summary.physicsImpulse.toFixed(2)}</strong>
                </div>
                <div className="expression-debug-panel__summary-row">
                  <span>duration</span>
                  <strong>{summary.durationSec.toFixed(1)}s</strong>
                </div>
              </>
            ) : (
              <div className="expression-debug-panel__empty">選一個動作開始測試</div>
            )}
          </div>
        </div>
      </div>

      {lastError && (
        <div className="expression-debug-panel__error">
          {lastError}
        </div>
      )}
    </div>
  );
};
