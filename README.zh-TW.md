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
│   ├── requirements.txt       # Python 相依套件
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

- Python 3.10+
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
cd backend
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python main.py
```

WebSocket 伺服器啟動於 `ws://localhost:${BACKEND_PORT}/ws/chat`。

### 前端啟動

```bash
cd vtuber-web-app
npm install
npm run dev
```

在瀏覽器開啟 `http://localhost:${FRONTEND_PORT}`。

### Headless Chat 測試（不開前端）

```bash
# 從 repository 根目錄執行完整 20＋5 cases
backend/.venv/bin/python backend/tools/chat_test_cli.py

# 精確重播已接受的案例快照
backend/.venv/bin/python backend/tools/chat_test_cli.py --scenario backend/log/chat_test_runs/latest/cases.json

# 既有 TXT 表情回歸素材
backend/.venv/bin/python backend/tools/chat_test_cli.py --scenario backend/tools/chat_test_scenarios.txt --max-turns 5
```

CLI 固定 Rushia，沿用同一 `main:app`／PostgreSQL `MemoryRuntime`，使用與正式 DB 不同的 `MEMORY_TEST_DATABASE_URL` 及專用 `test_<32 lowercase hex>` schema。每案 reset 隔離 owner，setup 結案後執行新 session／連線的長期 probe；不直接灌入記憶。五筆延伸案例沿用 `CHAT_AI_*` 生成，重播不重新生成。

每次建立 `backend/log/chat_test_runs/YYYYMMDD_HHMMSS/`，`latest/` 以連結指向最新完整 Chat 執行結果。資料夾包含 `cases.json`、`turns.jsonl`、`case_states.jsonl`、`run.json`、`memory_report.md`、`expression_report.md` 與 `server.log`。`turns.jsonl` 保存完整逐輪 JEV／表情、提出／提交操作、DB 來源、召回及裁切後 Chat messages；兩份 Markdown 只保留摘要與查詢入口。語意品質由人工查閱；背景工作錯誤會保存並反映在執行狀態。結束清理測試 schema 與短期／prompt 暫存，不讀寫正式長期記憶。

獨立六情境 memory-agent 評估使用 `backend/log/memory_agent_runs/<timestamp>/`，保存 `run.json`、`memory_agents.json` 與 `memory_agents_report.md`，不會切換 Chat 測試的 `latest`。

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
