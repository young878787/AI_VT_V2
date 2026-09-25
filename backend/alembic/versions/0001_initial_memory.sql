CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE memory_scope_state (
    user_id uuid NOT NULL,
    character_id uuid NOT NULL,
    generation bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, character_id)
);

CREATE TABLE memory_items (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    character_id uuid NOT NULL,
    group_id uuid NOT NULL,
    memory_type text NOT NULL CHECK (memory_type IN ('profile', 'preference', 'project', 'event', 'special', 'correction')),
    canonical_text text NOT NULL,
    subject_key text,
    keywords text[] NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (status IN ('active', 'superseded', 'merged', 'conflict', 'expired', 'archived')),
    importance real NOT NULL CHECK (importance BETWEEN 0 AND 1),
    confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    retention_class text NOT NULL CHECK (retention_class IN ('temporary', 'normal', 'important', 'core')),
    emotion_metadata jsonb NOT NULL DEFAULT '{}',
    embedding vector(1024),
    embedding_model text CHECK (embedding_model IS NULL OR embedding_model = 'Qwen/Qwen3-Embedding-0.6B'),
    observed_at timestamptz NOT NULL,
    valid_from timestamptz,
    valid_to timestamptz,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, user_id, character_id),
    CHECK (retention_class <> 'temporary' OR expires_at IS NOT NULL)
);
CREATE INDEX memory_items_owner_status_idx ON memory_items (user_id, character_id, status);
CREATE INDEX memory_items_group_status_idx ON memory_items (user_id, character_id, group_id, status);
CREATE INDEX memory_items_subject_idx ON memory_items (user_id, character_id, subject_key);
CREATE INDEX memory_items_keywords_idx ON memory_items USING gin (keywords);
CREATE INDEX memory_items_expiry_idx ON memory_items (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX memory_items_embedding_idx ON memory_items USING hnsw (embedding vector_cosine_ops);

CREATE TABLE memory_sources (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    character_id uuid NOT NULL,
    conversation_id uuid,
    message_id uuid,
    speaker text NOT NULL CHECK (speaker IN ('user', 'assistant', 'legacy_import')),
    raw_text text,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (id, user_id, character_id)
);
CREATE INDEX memory_sources_owner_message_idx ON memory_sources (user_id, character_id, message_id);

CREATE TABLE memory_evidence (
    user_id uuid NOT NULL,
    character_id uuid NOT NULL,
    memory_id uuid NOT NULL,
    source_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('origin', 'supports', 'contradicts')),
    PRIMARY KEY (user_id, character_id, memory_id, source_id, kind),
    FOREIGN KEY (memory_id, user_id, character_id) REFERENCES memory_items (id, user_id, character_id) ON DELETE CASCADE,
    FOREIGN KEY (source_id, user_id, character_id) REFERENCES memory_sources (id, user_id, character_id) ON DELETE CASCADE
);

CREATE TABLE memory_relations (
    user_id uuid NOT NULL,
    character_id uuid NOT NULL,
    from_id uuid NOT NULL,
    to_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('supersedes', 'merged_into', 'contradicts')),
    PRIMARY KEY (user_id, character_id, from_id, to_id, kind),
    FOREIGN KEY (from_id, user_id, character_id) REFERENCES memory_items (id, user_id, character_id) ON DELETE CASCADE,
    FOREIGN KEY (to_id, user_id, character_id) REFERENCES memory_items (id, user_id, character_id) ON DELETE CASCADE
);

CREATE TABLE memory_jobs (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    character_id uuid NOT NULL,
    generation bigint NOT NULL,
    conversation_id uuid NOT NULL,
    message_id uuid NOT NULL,
    route text NOT NULL CHECK (route IN ('none', 'buffer', 'process')),
    route_confidence real NOT NULL DEFAULT 0,
    memory_type_hint text,
    importance_hint real,
    explicit_memory real,
    source_text text,
    recent_dialogue jsonb,
    embedding vector(1024),
    buffered_job_ids uuid[] NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (status IN ('buffered', 'pending', 'running', 'retry', 'done', 'ignored', 'discarded', 'failed', 'cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    lease_until timestamptz,
    decisions jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, user_id, character_id),
    UNIQUE (user_id, character_id, message_id),
    CHECK (route <> 'none' OR (status = 'ignored' AND source_text IS NULL AND recent_dialogue IS NULL))
);
CREATE INDEX memory_jobs_queue_idx ON memory_jobs (status, lease_until, created_at);
CREATE INDEX memory_jobs_buffer_idx ON memory_jobs (user_id, character_id, memory_type_hint, created_at) WHERE status = 'buffered';

CREATE TABLE memory_audit (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    character_id uuid NOT NULL,
    operation_key text NOT NULL,
    action text NOT NULL,
    target_id uuid,
    source_event_id uuid,
    reason_class text,
    model text,
    decision jsonb,
    deleted_count integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, character_id, operation_key)
);
CREATE INDEX memory_audit_owner_idx ON memory_audit (user_id, character_id, created_at);
