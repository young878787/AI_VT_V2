import type {
  ChatSessionDetail,
  ChatSessionSummary,
  MemoryDetail,
  MemoryItem,
  MemoryOverview,
} from '../types/memoryLibrary';

const backendPort = import.meta.env.BACKEND_PORT || '9999';
const BACKEND = `http://localhost:${backendPort}`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BACKEND}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const payload = await response.json() as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // 保留 HTTP status 作為錯誤訊息。
    }
    throw new Error(message);
  }
  return await response.json() as T;
}

export const memoryLibraryService = {
  overview: () => request<MemoryOverview>('/api/memory-library/overview'),
  memories: (options: { query?: string; status?: string; offset?: number } = {}) => {
    const params = new URLSearchParams({
      query: options.query ?? '',
      status: options.status ?? 'current',
      limit: '50',
      offset: String(options.offset ?? 0),
    });
    return request<{ items: MemoryItem[]; limit: number; offset: number }>(
      `/api/memory-library/memories?${params.toString()}`,
    );
  },
  memoryDetail: (groupId: string) =>
    request<MemoryDetail>(`/api/memory-library/memories/${encodeURIComponent(groupId)}`),
  deleteMemory: (groupId: string) =>
    request<{ status: string; deleted_memory_count: number }>(
      `/api/memory-library/memories/${encodeURIComponent(groupId)}`,
      { method: 'DELETE' },
    ),
  sessions: () => request<{ items: ChatSessionSummary[] }>('/api/memory-library/sessions'),
  session: (sessionId: string) =>
    request<ChatSessionDetail>(`/api/memory-library/sessions/${encodeURIComponent(sessionId)}`),
  deleteSession: (sessionId: string) =>
    request<{ status: string }>(`/api/memory-library/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
    }),
  purgeSessions: () => request<{ status: string; session_count: number; deleted_files: number }>(
    '/api/memory-library/purge-chat-sessions',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmation: 'PURGE CHAT SESSIONS' }),
    },
  ),
  purgeLongTerm: () => request<{ status: string; purged: Record<string, number> }>(
    '/api/memory-library/purge-long-term',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmation: 'PURGE LONG TERM MEMORY' }),
    },
  ),
  exportVectors: async () => {
    const response = await fetch(`${BACKEND}/api/memory-library/export`, {
      headers: { Accept: 'application/x-ndjson' },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'memory-library.jsonl';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  },
};
