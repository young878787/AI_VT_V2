import type { ActionPlaybackState } from '../services/actionScheduler';
import { useAppStore } from '../store/appStore';
import './ActionPlaybackStatus.css';

const STATUS_LABELS: Record<ActionPlaybackState['status'], string> = {
  idle: '待機', queued: '排隊中', playing: '播放中', settling: '收尾中',
  finished: '演出完成', cancelled: '已取消', failed: '播放失敗',
};

const RESPONSE_LABELS = {
  reaction: '即時反應', waiting: '等待說話演出', speaking: '說話演出', settling: '收尾中', idle: '待機',
};

export const ActionPlaybackStatus = ({ state }: { state: ActionPlaybackState }) => {
  const debug = useAppStore(store => store.lastExpressionDebug);
  const durationMs = state.durationMs ?? 0;
  const hasDuration = durationMs > 0;
  const progress = hasDuration ? Math.min(1, state.elapsedMs / durationMs) : 0;
  const elapsed = `${(state.elapsedMs / 1000).toFixed(1)} 秒`;
  const timing = state.loop ? `已播放 ${elapsed}${state.kind === 'native-motion' && hasDuration ? ` · 每輪 ${(durationMs / 1000).toFixed(1)} 秒` : ''}`
    : `${elapsed}${hasDuration ? ` / ${(durationMs / 1000).toFixed(1)} 秒` : ''}`;
  return (
    <div className={`action-playback-status action-playback-status--${state.status}`} role="status" aria-live="polite">
      <div className="action-playback-status__heading">
        <strong>{state.responsePhase ? RESPONSE_LABELS[state.responsePhase] : STATUS_LABELS[state.status]}{state.loop ? ' · 持續循環' : ''}</strong>
        <span>{state.id ? timing : '尚未播放'}</span>
      </div>
      {state.label && <span className="action-playback-status__label">{state.label}</span>}
      {state.responsePhase === 'speaking' && <span className="action-playback-status__label">
        {state.timingSource === 'audio' ? '依語音進度' : '依文字安排節奏'}
        {state.segmentId !== undefined ? ` · 第 ${state.segmentId + 1} 段` : ''}
      </span>}
      {hasDuration && !state.loop && <progress aria-label="實際播放進度" max={1} value={progress} />}
      {debug?.jevDecisionSource && <details>
        <summary>表情判定（最近 JEV）</summary>
        <table><thead><tr><th>軸</th><th>JEV 原始選擇</th><th>信心</th><th>機率（前三）</th><th>採用結果</th><th>回退原因</th></tr></thead>
          <tbody>{([
            ['情緒', 'BaseEmotion', 'jevResolvedEmotion'],
            ['態度', 'InteractionAttitude', 'jevResolvedAttitude'],
            ['轉折', 'Arc', 'arc'],
          ] as const).map(([label, key, resolved]) => {
            const confidence = debug[`jev${key}Confidence`];
            const reason = debug[`jev${key}FallbackReason`];
            const prefix = `jev${key}Probability_`;
            const probabilities = Object.entries(debug)
              .filter((entry): entry is [string, number] => entry[0].startsWith(prefix) && typeof entry[1] === 'number')
              .sort((left, right) => right[1] - left[1]).slice(0, 3)
              .map(([option, probability]) => `${option.slice(prefix.length)} ${(probability * 100).toFixed(0)}%`).join('、');
            return <tr key={key}><td>{label}</td><td>{String(debug[`jev${key}Choice`] ?? '—')}</td>
              <td>{typeof confidence === 'number' ? confidence.toFixed(2) : '—'}</td>
              <td>{probabilities || '—'}</td>
              <td>{String(debug[resolved] ?? '—')}</td><td>{reason === 'none' ? '無' : String(reason ?? '—')}</td></tr>;
          })}</tbody>
        </table>
        <p>實際階段：{state.stage === 'speech' ? '說話演出' : state.stage === 'reaction' ? '即時反應' : '無'}；
          有效事件：{state.activeEvents?.join('、') || '無短暫事件'}</p>
      </details>}
      {state.error && <span className="action-playback-status__error">{state.error}</span>}
    </div>
  );
};
