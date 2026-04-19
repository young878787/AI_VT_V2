import { useAppStore } from '@store/appStore';
import './JPAFSidebar.css';

const PERSONA_LABELS: Record<string, string> = {
  tsundere: '傲嬌可愛',
  happy: '開朗快樂',
  angry: '暴躁生氣',
  seductive: '魅惑神秘',
};

const PERSONA_COLORS: Record<string, string> = {
  tsundere: '#e879a0',
  happy: '#f0b429',
  angry: '#ef4444',
  seductive: '#a855f7',
};

const FUNCTION_ORDER = ['Ti', 'Ne', 'Fi', 'Si', 'Fe', 'Te', 'Se', 'Ni'];

export const JPAFSidebar = () => {
  const jpafState = useAppStore(s => s.jpafState);
  const clearChatHistory = useAppStore(s => s.clearChatHistory);

  const handleReset = async () => {
    if (!confirm('確定要還原所有記憶和 JPAF 狀態嗎？這將清除對話歷史、用戶畫像和記憶筆記。')) return;

    try {
      const backendPort = import.meta.env.BACKEND_PORT || '9999';
      const res = await fetch(`http://localhost:${backendPort}/api/reset-memory`, {
        method: 'POST',
      });
      if (res.ok) {
        clearChatHistory();
      } else {
        console.error('Reset failed:', await res.text());
      }
    } catch (e) {
      console.error('Reset request failed:', e);
    }
  };

  const persona = jpafState?.persona ?? '---';
  const personaLabel = PERSONA_LABELS[persona] ?? persona;
  const personaColor = PERSONA_COLORS[persona] ?? 'var(--primary)';
  const dominant = jpafState?.dominant ?? '---';
  const auxiliary = jpafState?.auxiliary ?? '---';
  const turnCount = jpafState?.turnCount ?? 0;
  const baseWeights = jpafState?.baseWeights ?? {};

  return (
    <div className="jpaf-sidebar">
      <div className="jpaf-sidebar__header">
        <h3>JPAF</h3>
      </div>

      <div className="jpaf-sidebar__content">
        {/* Persona */}
        <div className="jpaf-sidebar__field">
          <span className="jpaf-sidebar__label">Persona</span>
          <span
            className="jpaf-sidebar__value jpaf-sidebar__persona-badge"
            style={{ '--persona-color': personaColor } as React.CSSProperties}
          >
            {personaLabel}
          </span>
        </div>

        {/* Dominant + Auxiliary inline */}
        <div className="jpaf-sidebar__field">
          <span className="jpaf-sidebar__label">Dominant / Aux</span>
          <div className="jpaf-sidebar__func-row">
            <span className="jpaf-sidebar__func-badge jpaf-sidebar__func-badge--dominant">
              {dominant}
            </span>
            <span className="jpaf-sidebar__func-sep">/</span>
            <span className="jpaf-sidebar__func-badge jpaf-sidebar__func-badge--auxiliary">
              {auxiliary}
            </span>
          </div>
        </div>

        {/* Turn */}
        <div className="jpaf-sidebar__field">
          <span className="jpaf-sidebar__label">Turn</span>
          <span className="jpaf-sidebar__value">{turnCount}</span>
        </div>

        {/* Base Weights bar chart */}
        <div className="jpaf-sidebar__field jpaf-sidebar__field--weights">
          <span className="jpaf-sidebar__label">Weights</span>
          <div className="jpaf-weights">
            {FUNCTION_ORDER.map(fn => {
              const w = baseWeights[fn] ?? 0;
              const pct = Math.round(w * 100);
              const isDominant = fn === dominant;
              const isAuxiliary = fn === auxiliary;
              return (
                <div
                  key={fn}
                  className={[
                    'jpaf-weights__row',
                    isDominant ? 'jpaf-weights__row--dominant' : '',
                    isAuxiliary ? 'jpaf-weights__row--auxiliary' : '',
                  ].join(' ').trim()}
                >
                  <span className="jpaf-weights__fn">{fn}</span>
                  <div className="jpaf-weights__bar-track">
                    <div
                      className="jpaf-weights__bar-fill"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="jpaf-weights__pct">{pct}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="jpaf-sidebar__footer">
        <button className="jpaf-sidebar__reset-btn" onClick={handleReset}>
          還原記憶
        </button>
      </div>
    </div>
  );
};
