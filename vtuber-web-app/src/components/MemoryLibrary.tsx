import { useEffect, useRef, useState } from 'react';
import { useAppStore } from '@store/appStore';
import { wsService } from '../services/wsService';
import { memoryLibraryService } from '../services/memoryLibraryService';
import type {
  ChatSessionDetail,
  ChatSessionSummary,
  MemoryDetail,
  MemoryItem,
  MemoryOverview,
} from '../types/memoryLibrary';
import './MemoryLibrary.css';

type LibraryTab = 'memories' | 'sessions';
type MemoryFilter = 'current' | 'history' | 'all';

const STATUS_LABELS: Record<string, string> = {
  active: '目前使用',
  conflict: '存在衝突',
  superseded: '已被更新',
  merged: '已合併',
  expired: '已過期',
  archived: '已封存',
};

const formatDate = (value: string | null | undefined) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-TW');
};

const formatPercent = (value: number) => `${Math.round(value * 100)}%`;

interface MemoryLibraryProps {
  open: boolean;
  onClose: () => void;
}

export const MemoryLibrary = ({ open, onClose }: MemoryLibraryProps) => {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const [tab, setTab] = useState<LibraryTab>('memories');
  const [filter, setFilter] = useState<MemoryFilter>('current');
  const [query, setQuery] = useState('');
  const [overview, setOverview] = useState<MemoryOverview | null>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [selectedMemory, setSelectedMemory] = useState<MemoryDetail | null>(null);
  const [selectedSession, setSelectedSession] = useState<ChatSessionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const reload = async (nextFilter = filter) => {
    setLoading(true);
    setError(null);
    try {
      const [nextOverview, nextMemories, nextSessions] = await Promise.all([
        memoryLibraryService.overview(),
        memoryLibraryService.memories({ query, status: nextFilter }),
        memoryLibraryService.sessions(),
      ]);
      setOverview(nextOverview);
      setMemories(nextMemories.items);
      setSessions(nextSessions.items);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '記憶圖書館載入失敗');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    closeButtonRef.current?.focus();
    void reload();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  // 開啟時載入一次；reload 依目前 filter/query 讀取，不把 function 放入依賴避免重複請求。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, onClose]);

  const openMemory = async (item: MemoryItem) => {
    setError(null);
    try {
      setSelectedMemory(await memoryLibraryService.memoryDetail(item.group_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '記憶詳情載入失敗');
    }
  };

  const deleteMemory = async (item: MemoryItem) => {
    const confirmed = window.confirm(
      `確定刪除「${item.canonical_text}」及其 ${item.version_count} 個版本嗎？\n\n` +
      '這會清除相關長期記憶來源，且無法從圖書館復原。',
    );
    if (!confirmed) return;
    try {
      await memoryLibraryService.deleteMemory(item.group_id);
      setSelectedMemory(null);
      await reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '記憶刪除失敗');
    }
  };

  const openSession = async (session: ChatSessionSummary) => {
    setError(null);
    try {
      setSelectedSession(await memoryLibraryService.session(session.session_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '聊天紀錄載入失敗');
    }
  };

  const deleteSession = async (session: ChatSessionSummary) => {
    if (!window.confirm(`確定刪除 session「${session.session_id}」的聊天、摘要與情緒狀態嗎？`)) return;
    try {
      await memoryLibraryService.deleteSession(session.session_id);
      if (session.session_id === wsService.getSessionId()) {
        useAppStore.getState().clearChatHistory();
      }
      setSelectedSession(null);
      await reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '聊天紀錄刪除失敗');
    }
  };

  const exportVectors = async () => {
    setExporting(true);
    setError(null);
    try {
      await memoryLibraryService.exportVectors();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '向量匯出失敗');
    } finally {
      setExporting(false);
    }
  };

  const purgeLongTerm = async () => {
    const confirmation = window.prompt(
      '這會清空目前 owner 的長期記憶、來源、工作與 audit，且無法復原。\n\n請輸入：PURGE LONG TERM MEMORY',
    );
    if (confirmation !== 'PURGE LONG TERM MEMORY') return;
    try {
      await memoryLibraryService.purgeLongTerm();
      setSelectedMemory(null);
      await reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '長期記憶清空失敗');
    }
  };

  const purgeSessions = async () => {
    const confirmation = window.prompt(
      '這會刪除所有聊天 session、摘要與情緒檔案，且無法復原。\n\n請輸入：PURGE CHAT SESSIONS',
    );
    if (confirmation !== 'PURGE CHAT SESSIONS') return;
    try {
      await memoryLibraryService.purgeSessions();
      useAppStore.getState().clearChatHistory();
      wsService.syncResetSession();
      setSelectedSession(null);
      await reload();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '聊天紀錄清空失敗');
    }
  };

  if (!open) return null;

  return (
    <div className="memory-library-backdrop" role="presentation">
      <section id="memory-library" className="memory-library" role="dialog" aria-modal="true" aria-labelledby="memory-library-title">
        <header className="memory-library__header">
          <div>
            <span className="memory-library__eyebrow">MEMORY LIBRARY</span>
            <h2 id="memory-library-title">記憶圖書館</h2>
            <p>查看長期記憶與已保存的聊天資料。</p>
          </div>
          <button ref={closeButtonRef} type="button" className="memory-library__close" onClick={onClose} aria-label="關閉記憶圖書館">×</button>
        </header>

        <div className="memory-library__body">
          {overview && (
            <div className="memory-library__stats" aria-label="記憶概況">
              <div><span>目前記憶</span><strong>{overview.long_term.current}</strong></div>
              <div><span>歷史版本</span><strong>{overview.long_term.historical}</strong></div>
              <div><span>聊天 session</span><strong>{overview.sessions.count}</strong></div>
              <div><span>向量覆蓋</span><strong>{overview.long_term.total ? formatPercent(overview.long_term.vectorized / overview.long_term.total) : '—'}</strong></div>
            </div>
          )}

          <nav className="memory-library__tabs" aria-label="記憶資料分類">
            <button type="button" className={tab === 'memories' ? 'is-active' : ''} onClick={() => { setTab('memories'); setSelectedSession(null); }}>長期記憶</button>
            <button type="button" className={tab === 'sessions' ? 'is-active' : ''} onClick={() => { setTab('sessions'); setSelectedMemory(null); }}>聊天紀錄</button>
          </nav>

          {error && <div className="memory-library__error" role="alert">{error}</div>}
          {loading && <div className="memory-library__loading">正在讀取資料…</div>}

          {tab === 'memories' ? (
            <div className="memory-library__workspace">
              <div className="memory-library__toolbar">
                <form onSubmit={event => { event.preventDefault(); void reload(); }}>
                  <input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜尋記憶內容或 subject key" aria-label="搜尋長期記憶" />
                  <button type="submit">搜尋</button>
                </form>
                <select value={filter} onChange={event => { const next = event.target.value as MemoryFilter; setFilter(next); void reload(next); }} aria-label="記憶版本篩選">
                  <option value="current">目前記憶</option>
                  <option value="history">歷史版本</option>
                  <option value="all">全部版本</option>
                </select>
                <button type="button" className="memory-library__export" onClick={() => void exportVectors()} disabled={exporting}>{exporting ? '匯出中…' : '匯出向量'}</button>
              </div>
              <div className="memory-library__columns">
                <div className="memory-library__list" aria-label="長期記憶列表">
                  {!loading && memories.length === 0 && <div className="memory-library__empty">目前沒有符合條件的長期記憶。</div>}
                  {memories.map(item => (
                    <article className={`memory-card ${selectedMemory?.group_id === item.group_id ? 'is-selected' : ''}`} key={item.group_id}>
                      <button type="button" className="memory-card__main" onClick={() => void openMemory(item)}>
                        <span className="memory-card__status">{STATUS_LABELS[item.status] ?? item.status}</span>
                        <strong>{item.canonical_text}</strong>
                        <span>{item.memory_type}{item.subject_key ? ` · ${item.subject_key}` : ''}</span>
                        <small>{item.version_count} 個版本 · {item.evidence_count} 筆來源 · {item.embedding_present ? '已向量化' : '尚無向量'}</small>
                      </button>
                      <button type="button" className="memory-card__delete" onClick={() => void deleteMemory(item)} aria-label={`刪除 ${item.canonical_text}`}>刪除</button>
                    </article>
                  ))}
                </div>
                <div className="memory-library__detail">
                  {selectedMemory ? <MemoryDetailPanel detail={selectedMemory} /> : <div className="memory-library__placeholder">選擇一筆記憶查看版本與來源。</div>}
                </div>
              </div>
            </div>
          ) : (
            <div className="memory-library__workspace">
              {overview && overview.sessions.count === 0 && <div className="memory-library__notice">目前沒有聊天 session；連線聊天後會由後端建立。</div>}
              <div className="memory-library__columns">
                <div className="memory-library__list" aria-label="聊天 session 列表">
                  {!loading && sessions.length === 0 && <div className="memory-library__empty">目前沒有可讀取的聊天紀錄。</div>}
                  {sessions.map(session => (
                    <article className={`memory-card ${selectedSession?.session_id === session.session_id ? 'is-selected' : ''}`} key={session.session_id}>
                      <button type="button" className="memory-card__main" onClick={() => void openSession(session)}>
                        <span className="memory-card__status">{session.session_id}</span>
                        <strong>{session.preview || '（沒有 user 預覽）'}</strong>
                        <small>{session.message_count} 則訊息 · 更新於 {formatDate(session.updated_at)}</small>
                      </button>
                      <button type="button" className="memory-card__delete" onClick={() => void deleteSession(session)} aria-label={`刪除 session ${session.session_id}`}>刪除</button>
                    </article>
                  ))}
                </div>
                <div className="memory-library__detail">
                  {selectedSession ? <SessionDetailPanel detail={selectedSession} /> : <div className="memory-library__placeholder">選擇一個 session 查看聊天紀錄。</div>}
                </div>
              </div>
            </div>
          )}

          <section className="memory-library__danger" aria-label="危險操作">
            <div><strong>危險操作</strong><span>資料刪除後不會由圖書館提供復原。</span></div>
            <div className="memory-library__danger-actions">
              <button type="button" onClick={() => void purgeSessions()}>清空聊天紀錄</button>
              <button type="button" onClick={() => void purgeLongTerm()}>清空長期記憶資料庫</button>
            </div>
          </section>
        </div>
      </section>
    </div>
  );
};

const MemoryDetailPanel = ({ detail }: { detail: MemoryDetail }) => (
  <div className="memory-detail-panel">
    <h3>記憶版本</h3>
    {detail.versions.map(version => (
      <div className="memory-detail-panel__version" key={version.id}>
        <div><span className="memory-card__status">{STATUS_LABELS[version.status] ?? version.status}</span><time>{formatDate(version.observed_at)}</time></div>
        <p>{version.canonical_text}</p>
        <small>信心 {formatPercent(version.confidence)} · 重要度 {formatPercent(version.importance)} · {version.embedding_present ? `向量 ${version.embedding_model ?? '已存在'}` : '尚無向量'}</small>
      </div>
    ))}
    <h3>來源摘要</h3>
    {detail.evidence.length === 0 ? <p className="memory-detail-panel__muted">沒有可顯示的來源。</p> : detail.evidence.map(source => (
      <blockquote key={`${source.memory_id}-${source.source_id}-${source.kind}`}>
        <span>{source.kind} · {formatDate(source.occurred_at)}</span>
        <p>{source.excerpt || '（來源原文已清除）'}</p>
      </blockquote>
    ))}
  </div>
);

const SessionDetailPanel = ({ detail }: { detail: ChatSessionDetail }) => (
  <div className="memory-detail-panel">
    <h3>Session {detail.session_id}</h3>
    <p className="memory-detail-panel__muted">最後更新：{formatDate(detail.updated_at)}</p>
    {detail.summary && <div className="memory-session-summary"><strong>摘要</strong><p>{detail.summary}</p></div>}
    <div className="memory-session-messages">
      {detail.messages.map((message, index) => (
        <div className={`memory-session-message is-${message.role}`} key={`${message.role}-${index}`}>
          <span>{message.role === 'user' ? '使用者' : '露西亞'}</span>
          <p>{message.content}</p>
          {message.status === 'interrupted' && <small>已中斷</small>}
        </div>
      ))}
    </div>
  </div>
);
