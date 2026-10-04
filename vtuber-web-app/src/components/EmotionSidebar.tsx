import { useAppStore } from '@store/appStore';
import { EMOTION_FIELDS, type EmotionField } from '../types/emotionState';
import { wsService } from '../services/wsService';
import './EmotionSidebar.css';

const LABELS: Record<EmotionField, string> = {
  shy: '害羞',
  pleased: '開心',
  genuinely_angry: '真的生氣',
  sad_or_hurt: '難過／受傷',
  masking_positive_feeling: '掩飾正面情緒',
  wants_continue_interaction: '想繼續互動',
};

const SOURCE_LABELS = {
  jev: 'JEV 判斷',
  previous_fallback: '沿用上一輪',
  neutral_fallback: '中性預設',
};

export const EmotionSidebar = () => {
  const emotionState = useAppStore(s => s.emotionState);
  const emotionSource = useAppStore(s => s.emotionSource);
  const clearChatHistory = useAppStore(s => s.clearChatHistory);

  const handleReset = async () => {
    if (!confirm('確定要清除記憶、對話與情緒狀態嗎？')) return;
    try {
      const backendPort = import.meta.env.BACKEND_PORT || '9999';
      const sessionId = wsService.getSessionId();
      const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
      const res = await fetch(`http://localhost:${backendPort}/api/reset-memory${query}`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error(await res.text());
      clearChatHistory();
      useAppStore.getState().setEmotionState({
        shy: 0, pleased: 0, genuinely_angry: 0, sad_or_hurt: 0,
        masking_positive_feeling: 0, wants_continue_interaction: 0.5,
      }, 'neutral_fallback');
      wsService.syncResetSession();
    } catch (error) {
      console.error('Reset failed:', error);
    }
  };

  return (
    <div className="emotion-sidebar">
      <h3>露西亞情緒</h3>
      <p className="emotion-sidebar__source">
        {emotionSource ? SOURCE_LABELS[emotionSource] : '等待對話'}
      </p>
      <div className="emotion-sidebar__scores">
        {EMOTION_FIELDS.map(field => {
          const score = emotionState?.[field] ?? 0;
          return (
            <div className="emotion-sidebar__row" key={field}>
              <div className="emotion-sidebar__heading">
                <span>{LABELS[field]}</span>
                <span>{emotionState ? `${Math.round(score * 100)}%` : '—'}</span>
              </div>
              <div className="emotion-sidebar__track" role="meter" aria-label={LABELS[field]} aria-valuenow={Math.round(score * 100)} aria-valuemin={0} aria-valuemax={100}>
                <div className="emotion-sidebar__fill" style={{ width: `${score * 100}%` }} />
              </div>
            </div>
          );
        })}
      </div>
      <button className="emotion-sidebar__reset" onClick={handleReset}>還原記憶</button>
    </div>
  );
};
