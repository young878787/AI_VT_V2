import { useEffect, useRef, useState } from 'react';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import type { NativeExpressionInfo, NativeMotionInfo } from '../live2d/LAppModel';
import { actionScheduler } from '../services/actionScheduler';
import { useActionPlaybackState } from '../services/useActionPlaybackState';
import { compileDebugExpressionPlan } from '../services/expressionDebugService';
import { useAppStore } from '../store/appStore';
import { isExpressionPlanPayload } from '../types/expressionPlan';
import { ActionPlaybackStatus } from './ActionPlaybackStatus';
import './NativePreviewPanel.css';

type Selection = { kind: 'motion'; group: string; index: number } | { kind: 'expression'; id: string };
interface Catalog { motions: NativeMotionInfo[]; expressions: NativeExpressionInfo[] }

const readCatalog = (): Catalog => {
  const model = LAppLive2DManager.getInstance().getActiveModel();
  return { motions: model?.getNativeMotionCatalog() ?? [], expressions: model?.getNativeExpressionCatalog() ?? [] };
};

export const NativePreviewPanel = ({ apiBaseUrl }: { apiBaseUrl?: string }) => {
  const modelLoaded = useAppStore(state => state.modelLoaded);
  const modelError = useAppStore(state => state.modelError);
  const playback = useActionPlaybackState();
  const [catalog, setCatalog] = useState<Catalog>(readCatalog);
  const [group, setGroup] = useState('all');
  const [selection, setSelection] = useState<Selection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const resetVersion = useRef(0);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const next = readCatalog();
      setCatalog(current => JSON.stringify(current) === JSON.stringify(next) ? current : next);
    }, 500);
    return () => { window.clearInterval(timer); resetVersion.current += 1; };
  }, [modelLoaded]);

  const stop = () => {
    resetVersion.current += 1;
    setResetting(false);
    actionScheduler.stopPreview();
  };
  const play = (next: Selection) => {
    if (!modelLoaded) return;
    resetVersion.current += 1;
    setResetting(false);
    setSelection(next);
    setError(null);
    const started = next.kind === 'motion'
      ? actionScheduler.playNativeMotion(next.group, next.index)
      : actionScheduler.playNativeExpression(next.id);
    if (!started) setError(actionScheduler.getPlaybackState().error ?? '原生素材未能開始播放。');
  };
  const resetCalm = async () => {
    stop();
    const version = ++resetVersion.current;
    const schedulerRevision = actionScheduler.getPreviewRevision();
    setResetting(true);
    setError(null);
    try {
      const { plan } = await compileDebugExpressionPlan({ modelName: 'Rushia', kind: 'calm', blinkStyle: 'normal', seed: 7 }, apiBaseUrl);
      if (version !== resetVersion.current || !useAppStore.getState().modelLoaded
        || schedulerRevision !== actionScheduler.getPreviewRevision()) return;
      if (!isExpressionPlanPayload(plan)) throw new Error('平靜表情資料格式不完整。');
      actionScheduler.submit(plan, 'debug');
    } catch (reason) {
      if (version === resetVersion.current) setError(reason instanceof Error ? reason.message : '無法恢復平靜表情。');
    } finally {
      if (version === resetVersion.current) setResetting(false);
    }
  };
  const groups = [...new Set(catalog.motions.map(motion => motion.group))];
  const motions = catalog.motions.filter(motion => group === 'all' || motion.group === group);
  const loadedMotions = catalog.motions.filter(motion => motion.status === 'loaded').length;
  const loadedExpressions = catalog.expressions.filter(expression => expression.status === 'loaded').length;
  const disabled = !modelLoaded || resetting;

  return (
    <div className="native-preview-panel">
      <div className="native-preview-panel__heading"><h3>原生動作與表情</h3><span>動作 {loadedMotions}/{catalog.motions.length} · 表情 {loadedExpressions}/{catalog.expressions.length}</span></div>
      {!modelLoaded && <p className="native-preview-panel__hint">{modelError ?? '等待 Rushia 載入…'}</p>}
      <label className="native-preview-panel__field">動作群組<select value={group} onChange={event => setGroup(event.target.value)} disabled={!modelLoaded}>
        <option value="all">全部群組</option>{groups.map(name => <option key={name} value={name}>{name}</option>)}
      </select></label>
      <div className="native-preview-panel__catalog" aria-label="原生動作清單">
        {motions.map(motion => <button key={`${motion.group}-${motion.index}`} type="button" className={`native-preview-panel__item ${selection?.kind === 'motion' && selection.group === motion.group && selection.index === motion.index ? 'active' : ''}`}
          aria-label={`播放原生動作 ${motion.name}`} disabled={disabled || motion.status !== 'loaded'}
          onClick={() => play({ kind: 'motion', group: motion.group, index: motion.index })}>
          <span className="native-preview-panel__item-heading"><strong>{motion.name}</strong><span>{motion.status === 'loaded' ? '已載入' : '載入失敗'}</span></span>
          <code>{motion.group}[{motion.index}]</code><span>{motion.durationSec.toFixed(2)} 秒 · {motion.loop ? '循環，需手動停止' : '單次演出'}</span>
          <span className="native-preview-panel__file">{motion.file}</span>
          {motion.error && <span className="native-preview-panel__error">{motion.error}</span>}
        </button>)}
      </div>
      <h4>原生表情 · {catalog.expressions.length}</h4>
      <div className="native-preview-panel__catalog" aria-label="原生表情清單">
        {catalog.expressions.map(expression => <button key={expression.id} type="button" aria-label={`播放原生表情 ${expression.id}`}
          className={`native-preview-panel__item ${selection?.kind === 'expression' && selection.id === expression.id ? 'active' : ''}`}
          disabled={disabled || expression.status !== 'loaded'} onClick={() => play({ kind: 'expression', id: expression.id })}>
          <span className="native-preview-panel__item-heading"><strong>{expression.id}</strong><span>{expression.status === 'loaded' ? '已載入' : '載入失敗'}</span></span>
          <span>持續表情，直到切換或停止</span><span className="native-preview-panel__file">{expression.file}</span>
          {expression.error && <span className="native-preview-panel__error">{expression.error}</span>}
        </button>)}
      </div>
      <div className="native-preview-panel__controls" aria-label="原生預覽控制">
        <button type="button" disabled={disabled || !selection} onClick={() => selection && play(selection)}>重播選定素材</button>
        <button type="button" disabled={!modelLoaded} onClick={stop}>停止</button>
        <button type="button" disabled={!modelLoaded} onClick={() => { actionScheduler.clearNativeExpression(); setError(null); }}>清除原生表情</button>
        <button type="button" disabled={disabled} onClick={() => void resetCalm()}>回到平靜</button>
      </div>
      {resetting ? <p className="native-preview-panel__hint" role="status">正在編譯平靜表情…</p> : <ActionPlaybackStatus state={playback} />}
      {(error || modelError) && <p className="native-preview-panel__error" role="alert">{error ?? modelError}</p>}
    </div>
  );
};
