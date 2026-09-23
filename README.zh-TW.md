# AI VTuber

一套以大型語言模型驅動的虛擬主播系統，讓 AI 即時操控 Live2D 角色的表情與動作，並透過持久化記憶系統在多次對話間記住使用者的喜好與共同回憶。

> 本專案持續維護與擴充中。

---

## 核心能力

- AI 根據對話內容即時驅動 Live2D 模型的表情參數（眼睛、眉毛、嘴角、臉紅、頭部動作）。
- JEV 決定情緒與表演意圖，後端編譯 expression plan，前端 Scheduler 控制播放。
- 持久化記憶系統能跨對話記住使用者的個性特徵、喜好及重要事件。
- Chat 使用有界近期對話與相關記憶；對話摘要保存在 session 專用檔。

---

## 架構

```
AI_VT_V2/
├── backend/                   # Python FastAPI 後端
│   ├── main.py                # WebSocket 伺服器、LLM 協調、記憶系統
│   ├── requirements.txt       # Python 相依套件
│   └── memory/                # 持久化記憶（已 gitignore，執行時自動建立）
│       ├── user_profile.json  # 使用者個性與喜好資料
│       ├── memory_records.json # 長期記憶紀錄
│       └── memory_jobs/      # 背景記憶待辦
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
| 記憶 | JSON + Markdown 純文字檔 |

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
- 任一可用 provider 的 API 金鑰（[OpenRouter](https://openrouter.ai)、NVIDIA 或 Google AI Studio）

### 環境變數設定

將 `.env.example` 複製為 `.env` 並填入金鑰：

```
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=your_key_here
# 或使用 Google
# AI_PROVIDER=google
# GOOGLE_API_KEY=your_key_here
# 可選：CHAT_AI_PROVIDER / CHAT_MODEL_NAME、MEMORY_AI_PROVIDER / MEMORY_MODEL_NAME
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

CLI 會自動啟動隔離測試後端，逐輪擷取回覆、JEV 六欄位情緒、表情與記憶變更。每次執行在 `backend/log/chat_test_runs/<run-id>/` 建立獨立的 `memory/`、`turns.jsonl`、逐輪更新的 `report.md`、`run.json` 與 `server.log`（已 gitignore），不會讀寫正式記憶。錯誤重試耗盡或逾時即停止，並保留部分報告。JEV Emotion 與 Action 需設定 `OPENROUTER_API_KEY`；`EXPRESSION_DECIDER` 已不再使用。

---

## 記憶系統說明

AI 在 `backend/memory/`（已排除版本控制）維護持久化資料：

- `user_profile.json` — 記錄使用者的核心特徵、溝通風格、興趣與討厭的事物。
- `memory_records.json` — 結構化長期記憶；舊 `memory.md` 匯入前會備份，之後保留相容檢視。
- `memory_jobs/` — 可追蹤、可重跑的背景記憶待辦。
- `long_term_summary.json` — 由已採納紀錄生成、附來源 ID 的長期摘要。

Chat 每輪使用最近 8 輪與有界相關記憶。手動壓縮的對話摘要存入對應 session 的摘要檔，不混入長期記憶。

---

## 授權

本專案尚未聲明授權，作者保留所有權利。
