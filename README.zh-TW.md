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
cd backend
python tools/chat_test_cli.py --scenario tools/chat_test_scenarios.txt   # 腳本模式（20 輪範例對話表）
python tools/chat_test_cli.py                                            # 互動模式
python tools/chat_test_cli.py --scenario tools/chat_test_scenarios.txt --max-turns 5
```

長期記憶固定使用 PostgreSQL／pgvector `MemoryRuntime`。一般後端使用 `MEMORY_DATABASE_URL` 與固定正式 schema；CLI 會自動啟動同一份後端程式的隔離測試 instance，使用與正式 DB 不同的 `MEMORY_TEST_DATABASE_URL`，每次建立專用測試 schema、套用 Alembic migration，逐輪等待記憶 job 完成，並記錄 route、audit、記憶變更與對話結果；報告寫入後清理 schema。執行前需設定獨立的 `EMBEDDING_AI_API_KEY`、`EMBEDDING_AI_BASE_URL`、`EMBEDDING_AI_MODEL`、`EMBEDDING_AI_DIMENSION=1024`；本地 vLLM 可另外設定 `EMBEDDING_AI_SERVING_MODEL` 及 query/document prefixes。缺少測試 DB、測試 DB 指向正式 database 或 schema 不合法時，CLI 不會啟動後端。`backend/log/chat_test_runs/<run-id>/` 保留獨立的短期對話 `memory/`、`turns.jsonl`、逐輪更新的 `report.md`、`run.json` 與 `server.log`（已 gitignore）。JEV Emotion 與 Action 需設定 `JEV_AI_API_KEY` 或 `OPENROUTER_API_KEY`；`EXPRESSION_DECIDER` 已不再使用。

---

## 記憶系統說明

長期記憶的唯一真值是 PostgreSQL／pgvector schema，由 Alembic 管理版本。JEV 產生 NONE／BUFFER／PROCESS 分類，PROCESS 由 Memory LLM 產生決策，再由 DB Manager 寫入 owner-scoped tables。`backend/memory/` 只保留短期 session、summary 與 emotion state；`user_profile.json`、`memory_records.json`、`memory.md` 只作明確執行的 legacy importer 輸入。

Chat 每輪使用最近 8 輪與 PostgreSQL 中的有界相關記憶。手動壓縮的對話摘要存入對應 session 的摘要檔，不混入長期記憶。

---

## 授權

本專案尚未聲明授權，作者保留所有權利。
