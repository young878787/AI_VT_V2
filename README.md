# AI VTuber

## 問題與目標

市面上多數 AI 虛擬角色只能「對話配固定動作」：回覆文字是生成的，表情卻是預設幾個制式動作輪播，看久了重複、無聊，也無法反映語氣與心情。傳統 VTuber 則需要真人即時操控表情與回應，開播成本高、無法 24 小時互動。

本專案的核心是 **AI 生成心情對應**：讓 LLM 每次都根據對話語境即時生成當下的心情與表情參數（眼睛、眉毛、嘴、臉紅、頭部角度、呼吸），而非觸發固定動作。同樣的話在不同情緒下會有不同表情，且帶有隨機性與多元變化，並跨對話記住使用者偏好與共同回憶。

目標使用者為想打造個人 AI 主播、互動式虛擬角色的開發者與創作者。預期影響是提供一套開源、可自託管（self-hosted）、可更換 LLM provider 的即時 AI VTuber 基座：前端負責渲染，後端負責對話、表情編譯與記憶。

## 核心功能

- AI 生成心情對應（核心差異）：每次回覆都由 LLM 依語境即時生成心情與表情參數，而非播放固定動作；同樣的話在開心、害羞、生氣時表情不同，每次皆有隨機性與多元變化。
- 即時表情驅動：JEV 決定結構化表演意圖，Chat 模型獨立輸出對白；`expression_plan` 控制眼睛、眉毛、嘴、臉紅與頭部參數（眼睛、眉毛、嘴、臉紅、頭部角度、呼吸）。
- 獨特表情編譯：後端 `compile_expression_plan()` 將 AI 意圖轉為前端可播放的 expression plan，含平滑插值過渡。
- 持久化記憶：PostgreSQL／pgvector `MemoryRuntime` 保存跨 session 的使用者特徵、偏好與事件；舊 JSON／Markdown 只供一次性匯入。
- 背景記憶整理：JEV 負責分類，Memory LLM 產生受驗證的記憶決策，DB Manager 交易寫入 PostgreSQL。
- 上下文自動壓縮：Chat 只取最近對話與有界相關記憶；手動壓縮的摘要存於 session 專用檔，維持 context window 可用。
- 可選 TTS 語音：支援 Google Cloud TTS（Chirp 3 HD），可開關（`TTS_ENABLED`）。
- 手動除錯面板：ControlPanel / ModelParamPanel 可手動調參、即時檢視 Live2D 參數。

## 系統架構

```text
使用者文字／語音 → /ws/chat → PostgreSQL MemoryRuntime 接收事件與檢索
                            ↓
                        JEV Emotion → Runtime Emotion
                            ├── Chat 逐段文字 → TTS
                            └── JEV Action → expression compiler → expression_plan
                                                        ↓
                                  前端 Action Scheduler → Live2D adapter → LAppModel
Memory worker → Memory LLM → DB Manager → PostgreSQL
```

目錄結構（重點）：

```text
AI_VT_V2/
├── backend/                   # Python FastAPI 後端
│   ├── main.py                # WebSocket server 進入點
│   ├── requirements.txt       # Python 相依套件
│   └── memory/                # 短期 session／summary／emotion state（gitignored）
└── vtuber-web-app/            # React + TypeScript + Vite 前端
    └── src/
        ├── components/        # AIChatPanel / ControlPanel / Live2DCanvas 等
        ├── live2d/            # Cubism SDK 整合層，核心為 LAppModel.ts
        ├── services/wsService.ts  # 後端 WebSocket 客戶端
        └── store/appStore.ts  # Zustand 全域狀態
```

前端送出 `turn_id`；後端固定本輪對話與 PostgreSQL 記憶快照。JEV 更新六欄情緒並分類長期記憶後，Chat 逐段回覆，Action 獨立產生 `expression_plan`。前端 Scheduler 統一仲裁對話與手動操作；Memory worker 非同步完成記憶決策與 DB 寫入。`stream_end` 只表示文字完成，Action 與 Memory 可稍後完成。

## 使用技術

| 類型 | 技術／服務 | 用途 |
| --- | --- | --- |
| AI 模型 | OpenRouter / NVIDIA Build / Google AI Studio (Gemini) / 阿里雲 Qwen（DashScope，相容 OpenAI API） | Chat 對白、Memory 判斷；JEV 另行決定情緒與動作 |
| 前端 | React 19、TypeScript、Vite（rolldown-vite）、Zustand | UI、Live2D 渲染、WebSocket 客戶端、全域狀態 |
| 後端 | Python、FastAPI、WebSocket（uvicorn）、tiktoken | 對話編排、expression compiler、記憶系統、token 估算 |
| Sponsor 技術 | 阿里雲 Qwen（DashScope）、Google Cloud Text-to-Speech（Chirp 3 HD） | LLM 對話備選模型、語音合成（可選） |

完整相依請見 `backend/requirements.txt` 與 `vtuber-web-app/package.json`。Live2D 渲染使用 Cubism SDK for Web 5（見下方第三方素材）。

## 安裝與執行

```bash
# 1. 前置需求：Python 3.10+、Node.js 18+、JEV 與 CHAT／MEMORY 路線所需的 API 金鑰
# 2. 將 Cubism SDK for Web 解壓縮至專案根目錄，命名為 CubismSdkForWeb-5-r.5-beta.3/
#    （gitignored，需手動放置；另有 MotionSync plugin 目錄，同為 gitignored）

# 3. 環境變數：複製 .env.example 為 .env 並填入金鑰
cp .env.example .env
# 三條路線各自填入所用端點的金鑰、URL、模型，例如：
# OPENROUTER_API_KEY=your_openrouter_key  # JEV_AI_API_KEY 留空時沿用
# CHAT_AI_API_KEY=your_chat_key
# CHAT_AI_BASE_URL=https://api.openai.com/v1
# CHAT_AI_MODEL=gpt-4o-mini
# MEMORY_AI_API_KEY=your_memory_key
# MEMORY_AI_BASE_URL=https://api.openai.com/v1
# MEMORY_AI_MODEL=gpt-4o-mini
# BACKEND_PORT=9000
# FRONTEND_PORT=5287
# JEV 固定使用 SystemOne 請求格式；可用 JEV_AI_BASE_URL、JEV_AI_MODEL 覆寫預設。

# 4. 啟動後端（Windows PowerShell）
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
# WebSocket 伺服器：ws://localhost:${BACKEND_PORT}/ws/chat

# 5. 啟動前端（另開一個終端機，於專案根目錄）
cd vtuber-web-app
npm install
npm run dev
# 瀏覽器開啟 http://localhost:${FRONTEND_PORT}

# TTS（可選）：需先執行 gcloud auth application-default login，
# 並在 .env 設 TTS_ENABLED=true、TTS_LANGUAGE、TTS_VOICE_NAME
```

## Headless Chat 測試（不開前端）

長期記憶固定使用 PostgreSQL／pgvector `MemoryRuntime`。一般後端使用 `MEMORY_DATABASE_URL` 與固定 `MEMORY_DATABASE_SCHEMA`；`backend/tools/chat_test_cli.py` 會自動啟動同一份後端程式的隔離 instance，使用與正式資料庫不同的 `MEMORY_TEST_DATABASE_URL`，每次建立獨立 schema 並套用 Alembic migration。執行前需設定獨立的 `EMBEDDING_AI_API_KEY`、`EMBEDDING_AI_BASE_URL`、`EMBEDDING_AI_MODEL`、`EMBEDDING_AI_DIMENSION=1024`。本地 vLLM 可另外設定 `EMBEDDING_AI_SERVING_MODEL` 及 query/document prefixes。缺少測試 DB、測試 DB 指向正式 database 或 schema 不合法時，CLI 會在啟動後端前失敗；測試完成後清理該 schema。CLI 逐輪等待記憶 job 完成，擷取回覆、JEV 決策、route、audit 與記憶變更，不讀寫正式長期記憶。

```bash
cd backend

# 腳本模式：使用對話表逐輪發送（backend/tools/chat_test_scenarios.txt 為 20 輪範例）
python tools/chat_test_cli.py --scenario tools/chat_test_scenarios.txt

# 互動模式：手動輸入對話
python tools/chat_test_cli.py

# 限制輪數／逾時
python tools/chat_test_cli.py --scenario tools/chat_test_scenarios.txt --max-turns 5
python tools/chat_test_cli.py --scenario tools/chat_test_scenarios.txt --turn-timeout 120
```

每次執行在 `backend/log/chat_test_runs/<run-id>/` 建立全新的短期記憶與報告；測試 DB schema 在報告寫入後清理：

- `memory/` — 該次測試專用短期對話記憶
- `turns.jsonl` — 該次測試的逐輪原始資料
- `report.md` — 每輪更新的 Markdown 報告；中斷或失敗時保留部分結果與原因
- `run.json`、`server.log` — 情境／模型設定與後端錯誤日誌

JEV Emotion 與 Action 使用 OpenRouter System One，啟動前需設定 `JEV_AI_API_KEY` 或 `OPENROUTER_API_KEY`。Chat 只輸出露西亞的純文字回覆；`EXPRESSION_DECIDER` 已不再使用。

以上 log 檔案皆已 gitignore。單輪錯誤可沿用測試 session 重試（`--retries`，預設 2 次）；重試耗盡、已有回覆後失敗或逾時時會停止，不會把後續題目記成有效輪次。外部 `--url` 模式已移除，以免誤連正式服務。

## 作品展示

- 作品展示網址（選填）：（待補）
- 評選影片：（待補）

## 限制與未來工作

已知限制：

- Live2D 模型以 Hiyori（SDK 範例模型）調校為主，換其他模型時表情幅度可能需重新調 adapter。
- Cubism SDK 與模型 binary 為 gitignored，新環境需手動放置，無法一鍵重現。
- Chat 僅取最近 8 輪與有界相關記憶；較早但未摘要的細節可能不在當輪上下文。
- TTS 需要 Google Cloud ADC 登入，未設定則僅有文字無語音。
- 目前無 CI，`npm run build` 在部分 Windows 環境可能出現 `spawn EPERM`（與程式碼正確性無關，重試或換終端機即可）。

未來工作：

- 多模型 expression adapter（Haru 等）與表情差異放大。
- TTS 串流播放與口型（lip sync）對齊優化。
- 更精細的記憶檢索排序與長期情緒趨勢；session 級對話持久化（`CHAT_PERSISTENCE_*` 目前預設關閉）。
- 補上 `LICENSE` 與 CI（lint / typecheck / backend unittest）。

## 第三方服務、資料與素材

| 項目 | 來源／連結 | 授權／備註 |
| --- | --- | --- |
| Cubism SDK for Web 5（`CubismSdkForWeb-5-r.5-beta.3/`，根目錄，gitignored） | https://www.live2d.com/en/sdk/ | Live2D 專有授權，需自行下載，勿提交至 repo |
| MotionSync Plugin（`CubismSdkMotionSyncPluginForWeb-5-r.2/`，gitignored） | https://www.live2d.com/en/sdk/ | 同上 |
| Hiyori 範例模型（`vtuber-web-app/public/Resources/Hiyori/`） | 隨 Cubism SDK 附帶之範例 | 僅供展示／開發測試，請遵循 Live2D 範例素材規範 |
| OpenRouter API | https://openrouter.ai | JEV 可使用 `OPENROUTER_API_KEY`；Chat／Memory 請填各路線的 `*_AI_API_KEY` |
| NVIDIA Build API | https://build.nvidia.com | 使用時將金鑰與端點填入對應 CHAT／MEMORY 路線 |
| Google AI Studio（Gemini, OpenAI 相容端點） | https://ai.google.dev/gemini-api/docs/openai | 使用時將金鑰與端點填入對應 CHAT／MEMORY 路線 |
| 阿里雲 Qwen（DashScope 相容模式） | https://www.alibabacloud.com/help/en/model-studio/ | 使用時將金鑰與端點填入對應 CHAT／MEMORY 路線 |
| Google Cloud Text-to-Speech（Chirp 3 HD） | https://cloud.google.com/text-to-speech | 需 GCP ADC 登入，`TTS_ENABLED=true` 才啟用 |
| React / Vite (rolldown-vite) / Zustand / FastAPI / uvicorn 等開源套件 | 見 `vtuber-web-app/package.json`、`backend/requirements.txt` | 各自遵循 MIT / Apache-2.0 等開源授權 |

本 repo 不含任何 API 金鑰、Token 或個人資料；`backend/memory/` 已 gitignore。

## 團隊成員

| 姓名 | 分工 |
| --- | --- |
| young87878 | （待補） |
| Rushia | （待補） |

> 註：上表人名取自本 repo Git 歷史 contributor，實際分工待維護者補充。

## License

本專案根目錄尚未加入 `LICENSE` 檔案，目前為作者保留所有權利（All rights reserved）。若要開源，建議補上 `LICENSE`（如 MIT）並在此標示授權名稱。
