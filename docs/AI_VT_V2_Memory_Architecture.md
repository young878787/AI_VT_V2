# AI_VT V2 長期記憶架構決策

> 版本：v2.0
> 狀態：已定案，作為後端長期記憶改造的上位設計
> 實作細節：[AI_VT_Memory_System_Architecture.md](./AI_VT_Memory_System_Architecture.md)

## 1. 定案摘要

長期記憶採用兩個 AI 角色：

1. **JEV 分類器**接收本輪對話，判斷資訊是否需要進入長期記憶流程，並提供類型與重要性提示。
2. **Memory LLM**只接收 JEV 選出的內容、必要上下文及相關既有記憶，負責判斷如何建立、強化、更新、合併、取代、標記衝突或忽略記憶。

本次改造只處理後端長期記憶。既有近期對話、Session Summary、context compression、即時情緒與 expression state 均維持原狀。

Embedding 固定使用 `Qwen/Qwen3-Embedding-0.6B` 的完整 1024 維輸出；第一版不使用 MRL 降維。

```text
每輪 User Input
      │
      ├──→ 既有短期對話／JEV 情緒／Chat 流程（不修改）
      │
      └──→ JEV 記憶分類
               │
               ├── NONE ────→ 結束
               ├── BUFFER ──→ 候選暫存
               └── PROCESS ─→ 背景 Memory Job
                                      │
                                      ▼
                            Matcher 找相關既有記憶
                                      │
                                      ▼
                                  Memory LLM
                                      │
                                      ▼
                         DB Manager 驗證並寫入 PostgreSQL
```

## 2. 系統邊界

### 2.1 本次會修改

- 後端 JEV 回應增加長期記憶分類欄位。
- 新增長期記憶 routing policy、候選暫存與背景工作流程。
- Memory LLM 的輸入、輸出與可執行動作契約。
- PostgreSQL／pgvector 的長期記憶資料、版本、來源、稽核與工作狀態。
- 長期記憶檢索、測試隔離與舊檔案資料匯入。

### 2.2 本次不修改

- 近期對話的保存方式與長度。
- Session Summary 與既有 context compression。
- JEV 即時情緒及 Live2D Action 的既有語意。
- Chat 回覆串流與既有 WebSocket payload。
- 前端、Live2D expression plan 與 `Hiyori.json`。

JEV 記憶分類可以讀取本輪輸入及既有近期對話的唯讀快照，但不得改寫短期記憶。Memory 候選暫存屬於長期記憶寫入管線，不是新的 Chat short-term memory。

## 3. JEV：接收對話並分類

### 3.1 定位

JEV 是長期記憶的 **Gatekeeper／Router**。擴充既有每輪 JEV decision，使同一次呼叫同時回傳既有情緒／表演結果與記憶分類；不再為 Memory 另做一次 JEV 呼叫。

單次 JEV 呼叫沿用目前共用 state，因此各 questions 在技術上會看到同一份有界 context。Memory questions 的判斷證據必須明定只使用：

- `current_user_input`
- 必要且有界的 `recent_dialogue` 唯讀快照

共用 state 中為 Emotion／Action 保留的 persona、即時狀態或 relevant memory，不得單獨構成記憶分類證據。JEV 不取得完整長期記憶、Vector Search 候選集、Merge History 或資料庫操作能力。

### 3.2 輸出契約

```json
{
  "memory_route": {
    "choice": "none | buffer | process",
    "confidence": 0.0
  },
  "memory_type": {
    "choice": "profile | preference | project | event | special | correction | none",
    "confidence": 0.0
  },
  "explicit_memory": {
    "noul": 0.0
  },
  "importance": {
    "score": 0.0,
    "confidence": 0.0
  }
}
```

`importance.score` 使用 0～4：

| 分數 | 意義 |
| --- | --- |
| 0 | 無長期價值 |
| 1 | 短暫資訊 |
| 2 | 普通資訊 |
| 3 | 未來很可能有用 |
| 4 | 重要且應長期保存 |

這些欄位都是提示，不是資料庫命令。最終 route 由後端 deterministic policy 驗證與解析。

### 3.3 Route 定義

| Route | 使用時機 | 後端行為 |
| --- | --- | --- |
| `NONE` | 寒暄、一般問答、重複或無長期價值資訊 | 保留不含對話原文的 terminal routing record，不呼叫 Memory LLM。 |
| `BUFFER` | 可能值得記，但資訊片面、未確認或缺少上下文 | 持久化為候選；等待相關後續資訊或明確觸發。 |
| `PROCESS` | 已足以形成記憶候選，或使用者明確要求記住、修改、忘記 | 建立背景 Memory Job，交給 Memory LLM。 |

`PROCESS` 只表示「應交給 Memory LLM」，不表示一定寫入資料庫。

每個有效輸入仍產生穩定 `event_id` 並維持既有 `input_accepted` 契約。`NONE` 的 routing record 只保存 owner、conversation／message ID、route、confidence 與時間，不保存 user text 或 recent dialogue，也不是可執行的 Memory Job。

### 3.4 後端 Policy

後端不得直接相信未驗證的 JEV 輸出。第一版規則固定為：

1. 明確記憶操作的規則判斷，或 `explicit_memory >= 0.80` 時，強制 `PROCESS`。
2. `memory_route=process` 且 route confidence `>= 0.65` 時為 `PROCESS`。
3. `memory_route=buffer`，或 `importance >= 2.5` 但 process 信心不足時為 `BUFFER`。
4. 其他情況為 `NONE`。
5. JEV 回應無效或逾時時，不做自動長期記憶 mutation；但規則已辨識出的明確記憶操作仍走 `PROCESS`。

門檻由單一後端 policy module 管理並以 boundary tests 固定，不散落在 prompt 或 route handler。日後調整門檻屬 policy 版本更新，不改變 JEV schema。

## 4. 候選暫存

`BUFFER` 保存的是尚未成熟的長期記憶候選，不是正式記憶，也不會提供給 Chat 當成已知事實。

候選至少保留：

```text
owner scope
conversation_id / message_id
raw user input
必要的近期對話快照
memory_type_hint
importance_hint
JEV confidence
created_at / updated_at
source turn ids
status = buffered
```

MVP 候選只在以下情況送往 Memory LLM：

- 後續 `PROCESS` 到來時，Matcher 依 cosine 排序同 owner、同 `memory_type` 的候選；最多附帶 Top 3，不設未經校準的固定 similarity threshold。
- 使用者明確要求記住、修改或忘記相關內容。

第一版不做 topic-change 或 session-end LLM 整理。候選 24 小時後由 deterministic maintenance 標為 `discarded`；Memory LLM 不定期掃描整個候選區。

## 5. Memory LLM：維護長期記憶語意

### 5.1 定位

Memory LLM 是唯一的長期記憶語意管理角色。原先分散為 Writer 與 Maintainer 的 LLM 職責合併成單一角色與單一結構化輸出契約。

第一版以後端自有的 `submit_memory_decisions` tool schema 接收結果，不沿用 `Hiyori.json` 的 Memory tools，也不依賴自由格式 JSON parsing。

它負責：

- 從選定的對話內容抽出零到多條原子記憶。
- 補足指代並產生穩定、可檢索的 `canonical_text`。
- 比對 Matcher 提供的少量相關記憶。
- 決定建立、強化、更新版本、合併、衝突、封存、忘記或忽略。
- 提供可稽核的理由與必要 metadata。

它不負責：

- 接收或處理每一輪對話。
- 掃描完整資料庫。
- 自行產生資料庫 ID。
- 執行 SQL、transaction、刪除或繞過 owner scope。
- 管理近期對話、Session Summary 或即時情緒。

### 5.2 輸入

```json
{
  "source": {
    "current_user_input": "...",
    "recent_dialogue": [],
    "buffered_context": []
  },
  "jev_hints": {
    "memory_type": "project",
    "importance": 3,
    "explicit_memory": 0.1
  },
  "related_existing_memories": []
}
```

`related_existing_memories` 由程式／SQL Matcher 依 owner、subject、keyword 與 pgvector 查出；Memory LLM 不自行查詢完整資料庫。

### 5.3 允許的決策

| 決策 | 語意 |
| --- | --- |
| `CREATE` | 建立新的 active 記憶。 |
| `REINFORCE` | 既有記憶再次獲得證據，不建立重複內容。 |
| `SUPERSEDE` | 新狀態取代舊狀態，保留版本歷史。 |
| `MERGE` | 將語意重複的記憶合併並保留來源證據。 |
| `CONTRADICT` | 新舊資訊互斥且無法安全判定先後。 |
| `ARCHIVE` | 明確不再有效，但仍需保留歷史。 |
| `FORGET` | 使用者明確要求忘記；交由 DB policy 驗證範圍後實體刪除。 |
| `IGNORE` | 沒有新資訊、證據不足或不值得保存。 |

Memory LLM 只提出結構化決策。DB Manager 必須再次驗證 schema、owner、目標狀態、允許的 transition、idempotency key 與 `FORGET` 的明確來源，才可在 transaction 中執行。

`FORGET` 會刪除 scope 內符合目標的 memory、evidence 及不再被引用的 source；audit 只保留 operation key、時間、scope 與刪除數量，不保留被要求忘記的原文。單純「資訊已改變」不得使用 `FORGET`，必須使用 `SUPERSEDE` 或 `ARCHIVE`。

## 6. 讀寫分離

### 6.1 寫入路徑

```text
User Input
  → JEV 分類
  → 後端 Policy
  → NONE / BUFFER / PROCESS
  → 背景 Memory Job
  → Matcher
  → Memory LLM
  → DB Manager
  → PostgreSQL + pgvector
```

Memory LLM 與 DB 寫入在背景執行，不阻塞 Chat 串流。失敗由 job retry／audit 處理，不回頭修改短期對話。

### 6.2 讀取路徑

```text
User Input
  → owner／status／time filter
  → exact match + pgvector candidates
  → 版本解析、去重、排序與固定上限
  → Relevant Long-term Memory Projection
  → Chat
```

Chat 只取得有界的長期記憶投影，並與既有短期 context 一起使用；不載入整份記憶庫，也不直接管理記憶。

Embedding 契約固定為：

- model：`Qwen/Qwen3-Embedding-0.6B`
- dimension：1024
- distance：cosine
- 儲存前：L2 normalize
- query：加上固定英文 retrieval instruction
- memory document：不加 instruction

更換模型、維度、normalization 或 instruction 都視為 embedding schema 變更，必須以 migration 與完整 re-embedding 處理。

## 7. 不變條件

以下條件是實作時不可破壞的驗收基準：

1. 每輪只有既有的一次 JEV decision；記憶分類併入同一份 JEV 契約並共用有界 state。
2. JEV 只分類，不做 Merge、Replace、Conflict Resolution 或 DB mutation。
3. Memory LLM 只處理 `PROCESS` 或已成熟的 `BUFFER`，不接收所有對話。
4. Memory LLM 不直接執行 SQL；實際 mutation 只由 DB Manager 完成。
5. 長期記憶寫入失敗不得阻塞 Chat。
6. owner scope、版本、來源、證據、audit 與 idempotency 必須完整。
7. JEV emotion state 與 memory emotion metadata 分離，長期記憶不得覆寫即時情緒。
8. 不修改目前短期對話記憶的保存、壓縮與 WebSocket 行為；既有 reset 只作測試／維護用途，不進入正常記憶讀寫流程。

## 8. 一句話版本

> JEV 負責從對話中分類「是否值得進入長期記憶流程」；Memory LLM 只處理被選出的內容並決定如何維護記憶；確定性後端程式負責安全、可追蹤地讀寫 PostgreSQL。
