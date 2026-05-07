import { useMemo, useState, type CSSProperties } from 'react';
import {
  DEFAULT_DEBUG_EXPRESSION_OPTIONS,
  EXPRESSION_DEBUG_PRESETS,
  MOTION_DEBUG_PRESETS,
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
  sequenceEvents: number;
  sequencePreview: string;
}

type DebugPlanSource = 'backend' | 'reset';

function summarizePlan(label: string, plan: ExpressionPlanPayload): AppliedSummary {
  return {
    label,
    preset: plan.basePose.preset,
    motionStyle: String(plan.basePose.bodyMotionProfile?.style ?? 'calm_sway'),
    motionVariant: plan.motionPlan ? `${plan.motionPlan.theme}:${plan.motionPlan.variant}` : 'none',
    idlePlan: plan.idlePlan?.name ?? 'none',
    physicsImpulse: plan.basePose.params.physicsImpulse,
    durationSec: plan.basePose.durationSec,
    sequenceEvents: plan.sequence.filter((event) => (
      !event.kind.startsWith('debug_brow_eye_gap_') &&
      !event.kind.startsWith('debug_speaking_micro_gap_')
    )).length,
    sequencePreview: plan.sequence
      .filter((event) => !event.kind.startsWith('debug_brow_eye_gap_') && !event.kind.startsWith('debug_speaking_micro_gap_'))
      .slice(0, 4)
      .map((event) => event.kind)
      .join(',') || 'none',
  };
}

export const ExpressionPlanDebugPanel = () => {
  const setExpressionPlan = useAppStore((state) => state.setExpressionPlan);
  const setBlinkControl = useAppStore((state) => state.setBlinkControl);
  const currentModelName = useAppStore((state) => state.currentModelName);
  const [options, setOptions] = useState<DebugExpressionOptions>(DEFAULT_DEBUG_EXPRESSION_OPTIONS);
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
      setLastError('backend expression_plan 格式未通過前端 validator');
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

  const compileBackendPlan = async (
    kind: DebugExpressionKind,
  ): Promise<{ plan: ExpressionPlanPayload; label?: string }> => {
    const response = await compileDebugExpressionPlan({
      modelName: currentModelName || 'Hiyori',
      kind,
      intensity: options.intensity,
    });
    return { plan: response.plan, label: response.summary?.label };
  };

  const compileBackendRandomPlan = async (): Promise<{ plan: ExpressionPlanPayload; label?: string }> => {
    const response = await compileDebugExpressionPlan({
      modelName: currentModelName || 'Hiyori',
      random: true,
      intensity: options.intensity,
    });
    return { plan: response.plan, label: response.summary?.label };
  };

  const compileBackendMotionPlan = async (
    kind: DebugMotionKind,
  ): Promise<{ plan: ExpressionPlanPayload; label?: string }> => {
    const response = await compileDebugExpressionPlan({
      modelName: currentModelName || 'Hiyori',
      motionKind: kind,
      intensity: options.intensity,
    });
    return { plan: response.plan, label: response.summary?.label };
  };

  const compileBackendScenarioPlan = async (
    scenario: 'speaking_micro' | 'brow_eye_micro',
  ): Promise<{ plan: ExpressionPlanPayload; label?: string }> => {
    const response = await compileDebugExpressionPlan({
      modelName: currentModelName || 'Hiyori',
      scenario,
      intensity: options.intensity,
    });
    return { plan: response.plan, label: response.summary?.label };
  };

  const applyPreset = async (kind: DebugExpressionKind) => {
    const preset = presetByKind.get(kind);
    setIsCompiling(true);
    setLastError(null);
    try {
      const response = await compileBackendPlan(kind);
      applyPlan(response.label ?? preset?.label ?? kind, response.plan, 'backend');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 expression_plan 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const applyRandom = async () => {
    setIsCompiling(true);
    setLastError(null);
    try {
      const response = await compileBackendRandomPlan();
      applyPlan(response.label ? `隨機:${response.label}` : '隨機', response.plan, 'backend');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 expression_plan 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend random compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const applyBrowEyeRandom = async () => {
    setIsCompiling(true);
    setLastError(null);
    try {
      const response = await compileBackendScenarioPlan('brow_eye_micro');
      applyPlan(response.label ?? '眉毛:隨機微動', response.plan, 'backend');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 brow/eye micro 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend brow/eye micro compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const applySpeakingMicroTest = async () => {
    setIsCompiling(true);
    setLastError(null);
    try {
      const response = await compileBackendScenarioPlan('speaking_micro');
      applyPlan(response.label ?? '說話:微表情', response.plan, 'backend');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 speaking micro 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend speaking micro compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const applyMotionPreset = async (kind: DebugMotionKind) => {
    const preset = MOTION_DEBUG_PRESETS.find((item) => item.kind === kind);
    setIsCompiling(true);
    setLastError(null);
    try {
      const response = await compileBackendMotionPlan(kind);
      applyPlan(response.label ? `動作:${response.label}` : preset ? `動作:${preset.label}` : `動作:${kind}`, response.plan, 'backend');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 motionPlan 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend motion compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const resetNeutral = async () => {
    setIsCompiling(true);
    setLastError(null);
    try {
      const response = await compileDebugExpressionPlan({
        modelName: currentModelName || 'Hiyori',
        kind: 'neutral',
        intensity: 'normal',
      });
      applyPlan(response.summary?.label ?? '中性復原', response.plan, 'reset');
    } catch (error) {
      const message = error instanceof Error ? error.message : '後端 neutral 編譯失敗';
      setLastError(message);
      console.warn('[ExpressionPlanDebug] backend neutral compile failed:', error);
    } finally {
      setIsCompiling(false);
    }
  };

  const setIntensity = (intensity: DebugExpressionIntensity) => {
    setOptions((current) => ({ ...current, intensity }));
  };

  return (
    <div className="expression-debug-panel">
      <div className="expression-debug-panel__header">
        <div className="expression-debug-panel__header-left">
          <span className="expression-debug-panel__title">Expression Plan 動作測試</span>
          <span className="expression-debug-panel__badge">
            Backend fake AI
          </span>
        </div>
        <div className="expression-debug-panel__header-actions">
          <button type="button" className="expression-debug-panel__action-btn" onClick={applyRandom} disabled={isCompiling}>
            {isCompiling ? '編譯中' : '隨機測試'}
          </button>
          <button type="button" className="expression-debug-panel__action-btn" onClick={applyBrowEyeRandom} disabled={isCompiling}>
            眉毛
          </button>
          <button type="button" className="expression-debug-panel__action-btn" onClick={applySpeakingMicroTest} disabled={isCompiling}>
            說話
          </button>
          <button type="button" className="expression-debug-panel__reset-btn" onClick={resetNeutral} disabled={isCompiling}>
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
                <div className="expression-debug-panel__summary-row">
                  <span>sequence</span>
                  <strong>{summary.sequenceEvents}</strong>
                </div>
                <div className="expression-debug-panel__summary-row">
                  <span>events</span>
                  <strong>{summary.sequencePreview}</strong>
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
