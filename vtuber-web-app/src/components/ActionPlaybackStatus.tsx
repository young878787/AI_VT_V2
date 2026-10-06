import type { ActionPlaybackState } from '../services/actionScheduler';
import './ActionPlaybackStatus.css';

const STATUS_LABELS: Record<ActionPlaybackState['status'], string> = {
  idle: '待機', queued: '排隊中', playing: '播放中', settling: '收尾中',
  finished: '演出完成', cancelled: '已取消', failed: '播放失敗',
};

export const ActionPlaybackStatus = ({ state }: { state: ActionPlaybackState }) => {
  const durationMs = state.durationMs ?? 0;
  const hasDuration = durationMs > 0;
  const progress = hasDuration ? Math.min(1, state.elapsedMs / durationMs) : 0;
  const elapsed = `${(state.elapsedMs / 1000).toFixed(1)} 秒`;
  const timing = state.loop ? `已播放 ${elapsed}${state.kind === 'native-motion' && hasDuration ? ` · 每輪 ${(durationMs / 1000).toFixed(1)} 秒` : ''}`
    : `${elapsed}${hasDuration ? ` / ${(durationMs / 1000).toFixed(1)} 秒` : ''}`;
  return (
    <div className={`action-playback-status action-playback-status--${state.status}`} role="status" aria-live="polite">
      <div className="action-playback-status__heading">
        <strong>{STATUS_LABELS[state.status]}{state.loop ? ' · 持續循環' : ''}</strong>
        <span>{state.id ? timing : '尚未播放'}</span>
      </div>
      {state.label && <span className="action-playback-status__label">{state.label}</span>}
      {hasDuration && !state.loop && <progress aria-label="實際播放進度" max={1} value={progress} />}
      {state.error && <span className="action-playback-status__error">{state.error}</span>}
    </div>
  );
};
