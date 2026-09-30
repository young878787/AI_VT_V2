# AI_VT 後端長期記憶實作計畫

> 版本：v2.0（定案版）
> 狀態：PostgreSQL runtime 已收斂為唯一長期記憶路線；正式 legacy 檔案資料仍須由 importer 明確遷移
> 上位決策：[AI_VT_V2_Memory_Architecture.md](./AI_VT_V2_Memory_Architecture.md)
> 實作順序：以第 10 章為準；其餘章節是共同契約

## 1. 目標、現況與非目標

目標是把後端長期記憶收斂為以下主線：

```text
JEV 接收對話並分類
  → 後端 Policy 選擇 NONE / BUFFER / PROCESS
  → Memory LLM 接收選定內容與相關既有記憶
  → DB Manager 維護 PostgreSQL
```

目前正式與測試 runtime 都由 PostgreSQL schema、DB-backed jobs、檢索與 `MemoryRuntime` 支撐。舊 JSON／Markdown 不再由後端 runtime 讀寫；若正式資料仍在舊檔案，必須透過 legacy importer 明確匯入，本文不把尚未執行的正式匯入視為完成。

本次對照已在正式 DB 以唯讀查詢確認 Alembic revision `0004_embedding_model`、pgvector extension 與 `memory_items.embedding` 的 `vector(1024)`；隔離 DB 連線、每次 run 的 schema migration／清理與 backend integration tests 均通過。修正報告 audit event UUID 正規化後，真實 AI 20 輪 run `20260930_103618_c497cb61` 已完成 20／20，結果為 NONE 14、BUFFER 3、PROCESS 3，建立 1 筆帶 embedding 的 preference 記憶，20 輪 embedding 全部成功，CREATE audit 可在報告中追蹤；本次測試 schema 已在報告產出後清理。

目前程式已包含 PostgreSQL runtime、隔離測試入口與 legacy importer；一般與隔離 instance 都固定建立 `MemoryRuntime`，不再有 file long-term runtime 或 backend 開關。真實 AI 20 輪對話、embedding、記憶建立、DB recall、JEV fallback 與測試 schema 清理均已完成對照；正式 legacy 檔案資料是否需要匯入仍須以 importer dry-run、抽樣與正式備份流程另行驗證，這不影響 runtime 已停止讀寫舊檔案。

本計畫不重做目前的對話短期記憶。Session 對話、Session Summary、context compression、即時情緒、expression state 與既有 WebSocket payload 均維持原狀。長期記憶可以讀取其有界快照，但不得改變其生命週期或儲存格式。

Embedding 使用可設定的模型 ID 與 served model name；目前本地設定為 `jinaai/jina-embeddings-v5-text-small-retrieval`，served name 為 `jina-retrieval`。資料庫仍固定使用 1024 維、cosine distance 與 L2-normalized vectors。不同模型／維度／前綴／正規化契約的向量分開檢索；舊向量保留，重新產生後才會參與新契約的語意檢索。

MVP 完成時，長期記憶、Profile 投影來源、證據、關係、audit 與背景工作以 PostgreSQL 為唯一真值。舊記憶檔只供一次性匯入與備份，不長期雙寫。

## 2. 系統邊界與元件責任

```text
Read Path
User Input → Retriever → 有界 Long-term Memory Projection → Chat
                    ↑
             exact match + pgvector

Write Path
User Input → 既有 JEV decision + memory classification
           → Routing Policy
              ├─ NONE
              ├─ BUFFER → DB-backed candidate
              └─ PROCESS ─┐
                           ▼
                    Memory Job Worker
                           │
                  Matcher（程式／SQL）
                           │
                     Memory LLM
                           │
                 DB Manager（transaction）
                           │
                      PostgreSQL
```

| 元件 | 唯一責任 |
| --- | --- |
| JEV 分類器 | 使用本輪輸入與少量近期對話，輸出 route、type、explicit、importance；不讀完整長期記憶，不決定 DB action。 |
| Routing Policy | 驗證 JEV 輸出並以固定規則決定 `NONE`、`BUFFER` 或 `PROCESS`。 |
| Memory Job Service | 持久化候選／工作、合併相關 buffer、負責 lease、retry、idempotency 與狀態。 |
| Matcher | 依 owner、subject、keyword／entity 及 pgvector 找出少量相關記憶。 |
| Memory LLM | 原子化候選並決定允許的記憶動作與理由；不執行 SQL 或自行決定 ID。 |
| DB Manager | 驗證 action、owner、狀態與時間，在單一 transaction 寫入記憶、來源、關係及 audit。 |
| Retriever | 解析版本、狀態、時間與排名，只投影少量相關長期記憶給 Chat。 |
| Maintenance | 處理候選逾期及 temporary memory 到期等確定性作業；不讓 LLM 定期掃描全庫。 |

Chat 只負責對話，不直接寫入長期記憶。Memory schema 獨立於 Live2D 的 `Hiyori.json`。

## 3. JEV 分類與 Routing Policy

### 3.1 單次 JEV 呼叫

擴充目前每輪唯一的 JEV decision，在既有 Emotion／Action questions 之外加入 memory questions，不建立第二次 JEV 呼叫。System One 的 questions 共用同一份 state，因此不宣稱欄位在傳輸層隔離；改由 Memory questions 明確限制只能以下列內容作為分類證據：

- `current_user_input`
- 有界 `recent_dialogue`

共用 state 內為 Emotion／Action 保留的 persona、即時狀態、expression state 或 relevant memory 不得單獨構成記憶分類證據。JEV 不注入完整 Profile、完整長期記憶、Vector Search 候選集、DB state 或 Merge History。

### 3.2 分類 schema

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

`importance.score` 必須在 0～4。後端先做型別、enum、數值範圍與 confidence 驗證，再套用固定 policy：

1. 規則判定為明確的 remember／update／forget，或 `explicit_memory >= 0.80`：`PROCESS`。
2. JEV 選擇 `process` 且 route confidence `>= 0.65`：`PROCESS`。
3. JEV 選擇 `buffer`，或 `importance >= 2.5` 但 process 信心不足：`BUFFER`。
4. 其餘：`NONE`。
5. JEV 失敗或輸出無效：不自動 mutation；規則已辨識的明確操作仍為 `PROCESS`。

門檻集中於一個 versioned policy module，必須有 boundary tests。日後調整門檻只更新 policy version，不改變 JEV schema。JEV 的 type、importance 與 explicit 結果只作為 Memory LLM hint，不直接設定資料庫欄位。

### 3.3 Route 結果

| Route | 是否保存 | 是否呼叫 Memory LLM |
| --- | --- | --- |
| `NONE` | 建立不含對話原文的 terminal routing record，保留既有 `event_id`。 | 否 |
| `BUFFER` | 在 `memory_jobs` 建立 `buffered` 記錄。 | 尚未；成熟後才排入。 |
| `PROCESS` | 建立 `pending` job，附上相關 buffered records。 | 是，背景執行。 |

每個有效 input 仍產生穩定 `event_id` 並維持既有 `input_accepted` payload。`NONE` record 只保存 owner、conversation／message ID、route、confidence 與時間，不保存 user text／recent dialogue，也不進入 worker queue。

MVP 的 Buffer promotion 固定為：新 `PROCESS` 到來時，Matcher 對同 owner、同 `memory_type` 的 buffered records 做 cosine 排序，最多附帶 Top 3；不先設定未經實測校準的 similarity threshold。明確 remember／update／forget 也可帶入同樣選出的候選。第一版不實作 topic-change 或 session-end LLM 整理。Buffer 24 小時後由 maintenance 標為 `discarded`，且永不交給 Chat 當作正式事實。

## 4. Memory LLM 契約

原先 Writer 與 Maintainer 合併為單一 Memory LLM 邏輯角色與單一設定路線。Worker 在呼叫前先由 Matcher 查出相關既有記憶。

第一版透過後端自有的單一 `submit_memory_decisions` tool schema 取得結構化結果。此 schema 位於 Memory domain，不放入 `Hiyori.json`，也不以自由格式 JSON parsing 作為正式契約。

### 4.1 輸入

```json
{
  "scope": {
    "user_id": "uuid",
    "character_id": "uuid"
  },
  "source": {
    "conversation_id": "uuid",
    "message_id": "uuid",
    "current_user_input": "...",
    "recent_dialogue": [],
    "buffered_context": []
  },
  "jev_hints": {
    "memory_type": "project",
    "importance": 3.0,
    "explicit_memory": 0.1
  },
  "related_existing_memories": []
}
```

所有 context 都有固定筆數與字元上限。Memory LLM 不取得完整資料庫。

### 4.2 輸出

Memory LLM 回傳零到多個 atomic decisions。每個 decision 至少包含：

```text
action
canonical_text（IGNORE／FORGET 可依 action 省略）
memory_type
subject_key（適用時）
target_memory_ids（只能引用輸入提供的 ID）
importance / confidence / retention_class
valid_from / valid_to / expires_at（適用時）
reason
```

允許的 action：

| Action | DB 結果 |
| --- | --- |
| `CREATE` | 建立新的 active 記憶。 |
| `REINFORCE` | 增加 evidence、確認時間與次數，不建立重複記憶。 |
| `SUPERSEDE` | 舊版設為 superseded，新版 active，並建立版本關係。 |
| `MERGE` | 重複記憶標為 merged，證據轉移到保留版本。 |
| `CONTRADICT` | 無法判定先後的互斥內容標為 conflict。 |
| `ARCHIVE` | 明確已失效的內容改為 archived。 |
| `FORGET` | 只有明確 user request 且範圍可驗證時才能實體刪除；否則拒絕。 |
| `IGNORE` | 不新增記憶，保留 job decision 供診斷。 |

LLM 輸出必須通過 JSON Schema、enum、UUID、日期、分數範圍、owner 與狀態 transition 驗證。LLM 不產生新資料庫 ID、不指定 schema、不執行 SQL。

`FORGET` 刪除 scope 內符合目標的 memory、evidence，以及不再被其他 memory 引用的 source。Audit 只保留 operation key、時間、scope、理由類別與刪除數量，不保存被要求忘記的原文。事實改變使用 `SUPERSEDE`／`ARCHIVE`，不得用 `FORGET` 取代版本管理。

## 5. 資料庫契約

所有 read、write、reset 與 maintenance 必須帶 `MemoryScope(user_id, character_id, schema_name)`。前兩項是 UUID；schema 名稱先驗證為合法 identifier。第一版由 `MEMORY_DEFAULT_USER_ID`、`MEMORY_DEFAULT_CHARACTER_ID` 提供固定身分；Live2D `model_name` 不當作 character ID。一般 repository 不提供無 owner 條件的 Chat API。

WebSocket 沿用字串 `session_id`、`turn_id`；DB 邊界以固定 namespace 的 UUIDv5 映射：

```text
conversation_id = UUIDv5(memory_namespace, session_id)
message_id      = UUIDv5(conversation_id, turn_id)
```

相同 turn 的重試得到相同 message ID。外部字串不得直接成為 SQL identifier。

| 表 | 必要資料與用途 |
| --- | --- |
| `memory_items` | 每個 ID 代表一個版本；保存 group、owner、type、canonical text、subject、keywords、status、時間、importance／confidence、retention、emotion 與 embedding metadata。 |
| `memory_sources` | owner、conversation／message ID、speaker、原文與發生時間。 |
| `memory_evidence` | memory 與 source 的 `origin`／`supports`／`contradicts` 關聯。 |
| `memory_relations` | `supersedes`、`merged_into`、`contradicts` 等版本或語意關係。 |
| `memory_audit` | owner、action、target、source event、reason、model、decision 與唯一 operation key。 |
| `memory_jobs` | owner、scope generation、source、JEV route／hints、buffer 關聯、狀態、attempts、lease、LLM output、error 與不含原文／向量的 embedding diagnostics；`NONE` record 不保存 source text。 |
| `memory_scope_state` | 每個 owner scope 的 generation；Reset 用來阻止舊 job 回填。 |
| `alembic_version` | Alembic revision；Runtime 只檢查目前版本。 |

`memory_jobs` 狀態為 `buffered`、`pending`、`running`、`retry`、`done`、`ignored`、`discarded`、`failed`、`cancelled`。

`memory_items` 狀態為 `active`、`superseded`、`merged`、`conflict`、`expired`、`archived`。記錄 `observed_at`、`valid_from`／`valid_to`、`expires_at`、`created_at`／`updated_at`。保留類別為 `temporary`、`normal`、`important`、`core`；temporary 原則上需要 `expires_at`。importance 與 confidence 在 DB 中正規化為 0～1。

具狀態性的事實使用穩定 `subject_key`，例如 `game.valorant.activity`；episode 可不設。Profile 不另建表，而是以 `profile.*` atomic memories 儲存，Chat 所需 dict 由 Profile Projection 組裝。

先建立 owner／status、group／status、subject、keywords、expires_at 索引，以及 `vector(1024)` 的 pgvector cosine HNSW 索引。向量維度固定在 migration，不把環境變數直接當成可變 schema。

DB access 採 Psycopg 3 async connection pool 與 pgvector adapter；migration 由 Alembic revision 與 `alembic_version` 管理，不引入 ORM。LLM 呼叫期間不得占用 DB connection 或 transaction。

Migration 從 `backend/` 執行，預設指向 `MEMORY_TEST_DATABASE_URL`，且測試 schema 須為 `test_<32 lowercase hex>`；正式 DB 必須明確指定 `database=production`。`MEMORY_DATABASE_SCHEMA` 可供正式 schema 使用。執行前應先備份正式 DB；Runtime 只檢查 Alembic revision，不自動升級。

正式與測試使用同一份後端程式及同一套 PostgreSQL MemoryRuntime，但以不同 instance 與不同 database 隔離：

```text
同一份後端程式
├─ 一般執行 instance → MEMORY_DATABASE_URL／固定正式 schema
└─ 隔離測試 instance → MEMORY_TEST_DATABASE_URL／每次 run 的獨立 test_* schema
```

PostgreSQL database 由管理者一次建立，Alembic 不建立 database。正式 schema 由 Alembic 建立及逐版升級並長期保留；隔離 CLI 每次產生隨機測試 schema、執行 `upgrade head`，測試結束後以 `DROP SCHEMA ... CASCADE` 清理。每個 schema 內各自保存 `alembic_version`，因此正式與每次測試的結構版本可獨立驗證。測試設定缺少、測試與正式 database 名稱相同，或 schema 名稱不合法時，必須在啟動測試後端前失敗。

```bash
cd backend
alembic -c alembic.ini -x database=test -x schema=test_0123456789abcdef0123456789abcdef upgrade head
alembic -c alembic.ini -x database=production upgrade head
```

## 6. 執行流程

### 6.1 寫入流程

1. 收到有效 Chat Input，既有 JEV decision 同時產生記憶分類。
2. Routing Policy 驗證分類；`NONE` 結束，`BUFFER` 持久化，`PROCESS` 建立 job。
3. Chat 繼續既有回覆流程，不等待 Memory LLM 或 DB mutation。
4. Worker 以 lease 與 `FOR UPDATE SKIP LOCKED` 取得 job；LLM 呼叫期間不持有 DB transaction。
5. Matcher 以 subject／keyword／entity exact match 加向量檢索取得同 owner 的少量相關記憶。
6. Memory LLM 產生 atomic decisions。
7. DB Manager 在單一 transaction 內重查 job status、generation、operation key、owner 與 target status，完成 items、sources、evidence、relations、audit 及 terminal job status。

每個 candidate decision 都有穩定且唯一的 operation key。同一 turn 的多個 decision 不共用 operation key；重試、多 worker 或重啟不得重複 mutation。Embedding 失敗的 write job 可 retry，不以缺少必要向量的結果假裝完成。

### 6.2 讀取流程

```text
本輪輸入（代名詞不明時才補極短近期對話）
  → CURRENT / HISTORY query mode
  → EMBEDDING_AI + subject／keyword exact match
  → owner／status／expiry／embedding contract filter
  → 具體詞面命中或 cosine similarity ≥ 0.75 → 最多 20 candidates
  → group 版本解析、去重與排序 → 最多 8 筆一般記憶
  → Profile Projection + Relevant Memory Projection
  → Chat
```

CURRENT 只把 active 且未到期的記憶當成目前事實。HISTORY 才納入 superseded、expired、archived，依 group、relation 與有效期間還原時間線；conflict 不自動當成已確認事實。同一 group 的舊版與新版不得並列為同時有效。

`0.75` 是尚待獨立樣本校準的保守起始門檻，不以 Top N 強制補滿。特定詞面的 canonical text、subject 或 keyword 命中可在低於門檻或 embedding 失敗時返回；一般性問句詞（例如「喜歡」、「記得」）不作詞面命中。Profile 也只從這批合格候選投影；沒有命中時，Chat 收到空 Profile 與空相關記憶。每輪的後端日誌只記錄 event ID、實際注入的記憶 ID、相似度及詞面命中狀態，不記錄查詢原文。驗證 DB 記憶是否參與回覆時，須用同 owner 的全新 session 與空對話歷史，核對候選日誌；同 session 回覆提及既有話題不能單獨當作證據。

Embedding 暫時失敗時退化為 subject／keyword 查詢；DB 暫時失敗時以空長期記憶繼續 Chat 並記錄結構化錯誤。這些 fallback 不讀取舊 JSON 作為隱性第二真值。

### 6.3 Reset 與到期

Reset 不是正常記憶讀寫流程的一部分，只保留給測試與人工維護。既有 `/api/reset-memory?session_id=...` 的外部行為維持不變：清除指定 owner scope 的長期記憶，並在提供 session 時沿用目前的 Session 對話、摘要與情緒清理。WebSocket `type=reset` 也維持目前的 Runtime／Session reset 行為。

Reset transaction 鎖定 `memory_scope_state`、增加 generation 並取消 scope 的舊 jobs。舊 worker 即使已完成 LLM 呼叫，也須因 generation 不符停止寫回。

20 輪測試不以正式 reset endpoint 建立隔離，而是繼續使用每次 run 的獨立測試儲存空間。Reset 只作手動重跑或診斷便利功能。

第一版排程 maintenance 只做 buffered candidate 24 小時逾期與 temporary memory 到期。importance decay、語意整理及全庫 LLM consolidation 延後。

## 7. AI 路線與設定

Memory LLM 使用單一 `MEMORY_AI` 路線。Embedding 使用獨立 `EMBEDDING_AI` 路線與 OpenAI-compatible embeddings API；不得因缺少設定而默默沿用 `MEMORY_AI`。

| 設定 | 用途 |
| --- | --- |
| `MEMORY_DATABASE_URL`、`MEMORY_DATABASE_SCHEMA` | 正式 DB 與 schema。 |
| `MEMORY_TEST_DATABASE_URL` | 專用測試 DB，必須與正式 DB 不同。 |
| `MEMORY_AI_API_KEY`、`MEMORY_AI_BASE_URL`、`MEMORY_AI_MODEL` | 單一 Memory LLM 路線。 |
| `EMBEDDING_AI_API_KEY`、`EMBEDDING_AI_BASE_URL` | 獨立的 OpenAI-compatible embedding endpoint。 |
| `EMBEDDING_AI_MODEL` | 實際 embedding 模型 ID，寫入記憶 metadata。 |
| `EMBEDDING_AI_SERVING_MODEL` | 可選的 API served name；未設定時使用模型 ID。 |
| `EMBEDDING_AI_DIMENSION` | 固定為 `1024`，必須等於 API 輸出與 DB vector 維度。 |
| `EMBEDDING_AI_QUERY_PREFIX`、`EMBEDDING_AI_DOCUMENT_PREFIX` | 分別加在 query 與 document 前；未設定時沿用舊 Qwen query instruction 與空 document prefix。 |
| `LOCAL_VLLM_*` | 本地 vLLM pooling server 的啟動參數；vLLM host/port 與 embedding API URL 應指向同一服務。 |
| `MEMORY_DEFAULT_USER_ID`、`MEMORY_DEFAULT_CHARACTER_ID` | 第一版固定 MemoryScope。 |

目前 Jina 設定會在查詢及文件前分別加上：

```dotenv
EMBEDDING_AI_QUERY_PREFIX="Query: "
EMBEDDING_AI_DOCUMENT_PREFIX="Document: "
```

API 回傳向量由應用層驗證長度、有限值並做 L2 normalize，再寫入或計算 cosine similarity。模型、1024 維、query/document prefix 與 normalization 組成 embedding contract。資料列記錄 contract hash，檢索只比較同一 contract 的向量；切換 contract 不會刪除舊向量，但要讓舊記憶參與新模型的語意檢索，仍須完整 re-embedding。

啟動時檢查 DB、pgvector、Alembic revision、AI 設定及 embedding dimension。正式啟動不自動執行 migration。Process environment 優先於 `.env`；log／report 不得輸出 credential URL 或 key。

## 8. 測試契約

### 8.1 單元與整合測試

至少涵蓋：

- JEV schema 解析、各 route 與 confidence／importance 邊界。
- 明確 remember／update／forget 規則覆蓋低信心或失敗的 JEV 回應。
- `NONE` 只建立無原文的 terminal routing record；`BUFFER` 不呼叫 Memory LLM；`PROCESS` 非同步排入。
- Buffer promotion、expiry 與不進入 Chat projection。
- 八種 Memory LLM action、invalid output 與非法 target ID。
- owner 隔離、CURRENT／HISTORY、版本關係及 conflict 行為。
- retry／restart／multi-worker idempotency 與 reset race。
- DB／embedding／Memory LLM 故障不阻塞 Chat。
- JEV 維持每輪一次呼叫，既有 emotion／action 與 WebSocket payload 不變。
- Session 對話、summary、compression 與 Runtime reset 的 regression tests。

### 8.2 20 輪隔離測試

沿用目前隔離式 20 輪對話的 CLI、scenario 與 report 流程。儲存層切換後使用獨立 `MEMORY_TEST_DATABASE_URL`，每次 run 建立符合 `test_<32 lowercase hex>` 的 schema，套用 migration，並固定該 run 的 session、user、character。缺少測試 DB、指向正式 DB 或 schema 名稱不合法時，CLI 必須在啟動後端前失敗，不得 fallback 到正式 DB。

同一次 run 每輪等待 JEV route 完成，並等待對應 job 到 terminal status 再進下一輪；`NONE` 的初始 `ignored` 狀態不能誤認為分類完成。這由 Alembic `0002_job_route_finalized` 欄位明確標示。正式 Chat 仍維持非同步。報告從 audit／decision 產生逐輪 route、buffer、記憶變化、job status、latency 與 error；embedding 另記錄對話檢索 query、記憶比對 query、BUFFER 文件及正式記憶文件的狀態、模型、維度、正規化、耗時與錯誤類別，不記錄向量或輸入原文。匯出後清理測試 schema；中斷時保留部分報告並盡力清理。

隔離驗收包含：正式 sentinel 在測試中不可見且測試後不變、第二次 run 初始為空、平行 run 互不可見、同 run 前後輪連續、同 turn retry 不重複寫入、錯誤 DB 設定安全失敗。

## 9. 舊資料遷移與切換

Legacy importer 依序讀取 `user_profile.json`、`memory_records.json`；後者不存在才讀 `memory.md`。先備份與 dry-run，將 Profile 列表拆成 atomic memories，保留可取得的 importance、時間、狀態與來源；缺來源時標記 `legacy_import`，不偽造 user message。

Legacy ID 以固定 UUIDv5 映射，使重跑安全。Report 不輸出不必要的私人原文。

切換順序固定為：

```text
備份
→ dry-run
→ 測試 schema 匯入與抽樣比對
→ 正式 schema 匯入
→ 一次切換長期 memory read／write
→ 觀察
→ 保留舊檔案作備份，必要時明確執行 legacy importer
```

原始檔成功後仍先保留供回復與核對。Runtime 不長期雙寫，也不在 DB 失敗時 fallback 到舊檔案。

## 10. 實作順序與完成標準

| 里程碑 | 交付內容 | 驗證關卡 |
| --- | --- | --- |
| M1 契約與 DB 基礎 | 固定 JEV classification、Routing Policy、Memory LLM schema、MemoryScope；以可設定 embedding contract／1024 維完成 Alembic migration、indexes、repository 與 startup checks。 | Schema／policy boundary tests；空 schema 可建且 migration 可重跑；owner、version、model、dimension 錯誤明確失敗。 |
| M2 寫入垂直切片 | 擴充既有單次 JEV call；完成 NONE／BUFFER／PROCESS、DB-backed jobs、Matcher、Memory LLM、DB Manager、retry、generation 與 maintenance。 | 八種 action、buffer lifecycle、idempotency、restart、multi-worker 與 reset race 通過；Chat 不等待寫入。 |
| M3 讀取與 Runtime 整合 | 完成 Retriever、Profile／Relevant Memory Projection，接上 Chat、lifespan、long-term reset 與 20 輪隔離測試。 | CURRENT／HISTORY、owner 隔離與 fallback 通過；既有短期記憶、JEV emotion／action、WebSocket regression tests 通過。 |
| M4 匯入與切換 | 完成 importer、PostgreSQL-only long-term read／write 切換、舊 runtime 與 backend 開關退役，更新 `.env.example`、README 與操作文件。 | 正式 runtime 不再讀寫長期記憶 JSON；legacy importer 的匯入數量、抽樣與 owner 驗證另行執行；可用備份與 migration rollback 回復。 |

MVP 只在四個關卡全部通過後完成。Hebbian association、全資料 graph spreading、episodic summary、LLM 定期全庫 consolidation、複雜 importance decay、跨裝置登入與多租戶 UI 均不在本次範圍。Session Summary 仍屬短期對話資料，不作為長期記憶真值。

## 11. 最終驗收清單

- [ ] JEV 是每輪對話進入長期記憶流程的唯一 AI 分類入口。
- [ ] 每輪維持一次 JEV call；Memory classification 與既有 emotion／action 共用該次回應。
- [ ] `NONE` 只保留無原文的 routing record 且不耗用 Memory LLM；`BUFFER` 不會被 Chat 當作正式事實。
- [ ] Memory LLM 是唯一記憶語意管理角色，但不直接執行 SQL。
- [ ] DB Manager 是唯一長期記憶 mutation 入口。
- [ ] PostgreSQL 是完成切換後的長期記憶唯一真值。
- [ ] 長期記憶寫入、retry 或 reset race 不阻塞 Chat。
- [ ] 來源、版本、evidence、audit、owner scope 與 idempotency 可追蹤。
- [ ] 目前短期對話記憶、Session Summary、即時情緒與前端契約沒有被修改。
- [ ] 隔離式 20 輪測試沿用目前入口、scenario 與 report，並改以專用 DB／per-run schema 儲存。
- [ ] Embedding model、1024 維、normalization 與 query instruction 均與 migration 契約一致。
