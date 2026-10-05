# AI VTuber

一套以大型語言模型驅動的虛擬主播系統，讓 AI 即時操控 Live2D 角色的表情與動作，並透過持久化記憶系統在多次對話間記住使用者的喜好與共同回憶。

> 本專案持續維護與擴充中。

---

## 核心能力

- AI 根據對話內容即時驅動 Live2D 模型的表情參數（眼睛、眉毛、嘴角、臉紅、頭部動作）。
- JEV 決定情緒與表演意圖，後端編譯 expression plan，前端 Scheduler 控制播放。
- PostgreSQL／pgvector `MemoryRuntime` 能跨對話記住使用者的個性特徵、喜好及重要事件；舊 JSON／Markdown 只供一次性匯入。
- Chat 使用有界近期對話與相關記憶；對話摘要保存在 session 專用檔。

---

## 架構

```
AI_VT_V2/
├── backend/                   # Python FastAPI 後端
│   ├── main.py                # WebSocket 伺服器、LLM 協調、記憶系統
│   ├── requirements.txt       # 舊版 pip／相容安裝清單
│   └── memory/                # 短期 session／summary／emotion state（已 gitignore）
│
└── vtuber-web-app/            # React + TypeScript + Vite 前端
    └── src/
        ├── components/        # UI 面板與互動層
        │   ├── AIChatPanel    # 聊天輸入與串流文字顯示
        │   ├── ControlPanel   # 手動表情與參數控制面板
        │   ├── HitAreaOverlay # Live2D 模型上的可點擊互動區域
        │   ├── Live2DCanvas   # 渲染 Live2D 模型的 WebGL 畫布
        │   └── ModelParamPanel # 即時參數檢視面板
        ├── live2d/            # Cubism SDK 整合層
        │   ├── LAppModel.ts   # 核心模型控制器（表情、物理演算）
        │   ├── LAppDelegate.ts
        │   ├── LAppView.ts
        │   └── MotionController.ts
        ├── services/
        │   └── wsService.ts   # 連接後端的 WebSocket 客戶端
        └── store/
            └── appStore.ts    # 全域狀態管理（Zustand）
```

---

## 技術堆疊

| 層次 | 技術 |
|---|---|
| 前端 | React 19、TypeScript、Vite（rolldown）、Zustand |
| Live2D | Cubism SDK for Web 5 |
| 後端 | Python、FastAPI、WebSocket |
| LLM | OpenRouter / NVIDIA / Google AI Studio（模型可設定） |
| 記憶 | PostgreSQL、pgvector、Alembic |

---

## 運作流程

1. 前端將文字或 ASR 完稿與 `turn_id` 送到 `/ws/chat`；後端固定本輪上下文快照並建立背景記憶待辦。
2. JEV Emotion 更新六欄即時情緒；Chat 逐段產生可唸對白，JEV Action 並行決定表演意圖。
3. expression compiler 產生 `expression_plan`，前端 Action Scheduler 仲裁後交給 Live2D 播放。
4. `stream_end` 代表文字完成；Memory worker 之後可獨立判斷、去重並保存記憶。

---

## 安裝與啟動

### 前置需求

- Python 3.12
- uv
- Node.js 18+
- Cubism SDK for Web（放置於專案根目錄，命名為 `CubismSdkForWeb-5-r.5-beta.3/`）
- JEV 與 CHAT／MEMORY 路線所需的 API 金鑰（JEV 使用 [OpenRouter](https://openrouter.ai) SystemOne）

### 環境變數設定

將 `.env.example` 複製為 `.env` 並填入金鑰：

```
OPENROUTER_API_KEY=your_openrouter_key  # JEV_AI_API_KEY 留空時沿用
CHAT_AI_API_KEY=your_chat_key
CHAT_AI_BASE_URL=https://api.openai.com/v1
CHAT_AI_MODEL=gpt-4o-mini
MEMORY_AI_API_KEY=your_memory_key
MEMORY_AI_BASE_URL=https://api.openai.com/v1
MEMORY_AI_MODEL=gpt-4o-mini
# JEV 使用 SystemOne；可用 JEV_AI_BASE_URL、JEV_AI_MODEL 覆寫預設。
```

### 後端啟動

```bash
# 從專案根目錄執行；主環境固定在 .venv/
uv sync
source .venv/bin/activate   # Linux/macOS
cd backend
python main.py
```

Windows PowerShell 請改用：

```powershell
uv sync
.\.venv\Scripts\Activate.ps1
cd backend
python main.py
```

Python 依賴的唯一主要來源是根目錄 `pyproject.toml`，精確解析結果保存在 `uv.lock`；`backend/requirements.txt` 僅保留給既有 pip／相容流程。日後更新依賴時，請在專案根目錄執行 `uv add` 或修改 `pyproject.toml` 後執行 `uv lock`，不要在 `backend/` 另外建立新的 `.venv`。

WebSocket 伺服器啟動於 `ws://localhost:${BACKEND_PORT}/ws/chat`。

### 前端啟動

```bash
cd vtuber-web-app
bun install
bun run dev
```

在瀏覽器開啟 `http://localhost:${FRONTEND_PORT}`。

### CI 與安全掃描

push／PR 分別執行 `backend-tests`（Ruff、unittest）、`frontend-tests`（Bun frozen install、codegen 同步、lint、契約／runtime、型別與 build）及 `CodeQL`（Python、JavaScript／TypeScript 安全掃描）。CodeQL 另有每週排程，掃描 job 成功不代表沒有漏洞，合併阻擋需另設定 code-scanning gate。

Rushia 素材未納入 Git，CI 執行純程式檢查；有素材的本機須另在 `vtuber-web-app/` 執行 `bun run check:rushia-assets`。build 通過不代表模型可載入，也不能取代瀏覽器視覺驗收。

### Headless Chat 測試（不開前端）

```bash
# 從 repository 根目錄執行完整 23＋5 cases
.venv/bin/python backend/tools/chat_test_cli.py

# 精確重播已接受的案例快照
.venv/bin/python backend/tools/chat_test_cli.py --scenario backend/log/chat_test_runs/latest/cases.json

# 既有 TXT 表情回歸素材
.venv/bin/python backend/tools/chat_test_cli.py --scenario backend/tools/chat_test_scenarios.txt --max-turns 5
```

長期記憶由單一 Memory Agent 逐步搜尋、讀取、提出操作與結案，後端準備小批候選並原子提交；沒有獨立 intake agent。程式 migration head 為 `0007_chat_sessions`；正式 DB 最近確認仍為 `0006_single_memory_agent`，部署新程式前必須先依備份與授權流程升版。

CLI 固定 Rushia，沿用同一 `main:app`／PostgreSQL `MemoryRuntime`，使用與正式 DB 不同的 `MEMORY_TEST_DATABASE_URL` 及專用 `test_<32 lowercase hex>` schema。每案 reset 隔離 owner，setup 結案後執行新 session／連線的長期 probe；不直接灌入記憶。短期組跳過長期接收與召回；長期 probe 停用短期載入、累積及寫入；綜合組記錄跨來源證據。五筆延伸案例沿用 `CHAT_AI_*` 生成，重播不重新生成。

每次覆寫固定 `backend/log/chat_test_runs/latest/`，不建立新時間資料夾；既有歷史保留。資料夾包含 `cases.json`、`turns.jsonl`、`case_states.jsonl`、`run.json`、`memory_report.md`、`expression_report.md` 與 `server.log`。`turns.jsonl` 保存完整逐輪 JEV／表情、提交操作、DB 來源、召回及裁切後 Chat messages；兩份 Markdown 只保留摘要與查詢入口。主表按三組並列「回答結果」與「最終應該答案／對話」，硬條件失敗時顯示具體錯誤，完整回答不截短。CLI 執行後以既有 Chat 模型的獨立 prompt 比對所有 probe，保存 `semantic_review` 並產生獨立語意表；模型評估仍可供人工核對。語意判定不能覆蓋來源硬條件；背景工作錯誤會保存並反映在執行狀態。結束清理測試 schema 與短期／prompt 暫存，不讀寫正式長期記憶。

獨立六情境 memory-agent 評估使用 `backend/log/memory_agent_runs/latest/`，保存 `run.json`、`memory_agents.json` 與 `memory_agents_report.md`，不會切換 Chat 測試的 `latest`。

先讀兩份摘要 Markdown；需要完整 evidence 時再用 `case_id`＋`turn` 查逐輪 JSON：

```bash
jq 'select(.case_id == "case_014" and .turn == 7)' backend/log/chat_test_runs/latest/turns.jsonl
```

需設定獨立的 `MEMORY_AI_*`、`EMBEDDING_AI_*`（1024 維）及 JEV key；缺少測試 DB 或測試 DB 指向正式 database 時停止。詳見 [記憶測試集設計與實作](docs/AI_VT_Memory_Testset_Design.md)。

---

## 記憶系統說明

長期記憶的唯一真值是 PostgreSQL／pgvector schema，由 Alembic 管理版本。JEV 產生 NONE／BUFFER／PROCESS 分類，PROCESS 由 Memory LLM 產生決策，再由 DB Manager 寫入 owner-scoped tables。`backend/memory/` 只保留短期 session、summary 與 emotion state；`user_profile.json`、`memory_records.json`、`memory.md` 只作明確執行的 legacy importer 輸入。

Chat 每輪使用最近 8 輪與 PostgreSQL 中的有界相關記憶。手動壓縮的對話摘要存入對應 session 的摘要檔，不混入長期記憶。

---

## 授權

本專案尚未聲明授權，作者保留所有權利。
