export type MemoryStatus = 'active' | 'conflict' | 'superseded' | 'merged' | 'expired' | 'archived';

export interface MemoryOverview {
  contract_version: number;
  long_term: {
    total: number;
    current: number;
    historical: number;
    conflict: number;
    vectorized: number;
    source_count: number;
  };
  sessions: { count: number; message_count: number };
  queue: {
    active_jobs: number;
    oldest_age_sec: number;
    buffered_jobs: number;
    expired_leases: number;
    retry_exhausted_jobs: number;
  };
}

export interface MemoryItem {
  id: string;
  group_id: string;
  memory_type: string;
  canonical_text: string;
  subject_key: string | null;
  keywords: string[];
  status: MemoryStatus;
  importance: number;
  confidence: number;
  retention_class: string;
  valid_from: string | null;
  valid_to: string | null;
  expires_at: string | null;
  observed_at: string;
  updated_at: string;
  version_count: number;
  has_conflict: boolean;
  embedding_present: boolean;
  embedding_model: string | null;
  embedding_contract: string | null;
  evidence_count: number;
}

export interface MemoryEvidence {
  memory_id: string;
  source_id: string;
  kind: string;
  speaker: string;
  excerpt: string;
  occurred_at: string;
}

export interface MemoryRelation {
  from_id: string;
  to_id: string;
  kind: string;
}

export interface MemoryDetail {
  group_id: string;
  versions: Array<Omit<MemoryItem, 'version_count' | 'has_conflict' | 'evidence_count'> & {
    emotion_metadata: Record<string, unknown>;
    created_at: string;
  }>;
  evidence: MemoryEvidence[];
  relations: MemoryRelation[];
}

export interface ChatSessionSummary {
  session_id: string;
  message_count: number;
  preview: string;
  updated_at: string;
  has_summary: boolean;
  has_emotion_state: boolean;
}

export interface ChatMessageRecord {
  role: 'user' | 'assistant';
  content: string;
  status?: 'interrupted';
}

export interface ChatSessionDetail {
  session_id: string;
  messages: ChatMessageRecord[];
  summary: string;
  emotion_state: Record<string, number> | null;
  updated_at: string;
}
