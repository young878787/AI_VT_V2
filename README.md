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
- 可選本地 TTS 語音：使用 Piper ONNX 推理，可開關（`TTS_ENABLED`）。
- 手動除錯面板：ControlPanel / ModelParamPanel 可手動調參、即時檢視 Live2D 參數。

## 系統架構

```text
使用者文字／語音 → /ws/chat → PostgreSQL MemoryRuntime 接收事件與檢索
                            ↓
                        JEV Emotion → Runtime Emotion
                            ├── Chat 完整文字 → 本地 Piper TTS
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
| Sponsor 技術 | 阿里雲 Qwen（DashScope） | LLM 對話備選模型 |
| 本地語音 | Piper TTS（ONNX） | 本地語音合成（可選） |

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
bun install
bun run dev
# 瀏覽器開啟 http://localhost:${FRONTEND_PORT}

# TTS（可選）：將 Piper 模型與 .onnx.json 放入 backend/models/，
# 並在 .env 設 TTS_ENABLED=true、PIPER_MODEL_PATH 等本地參數
```

## Headless Chat 測試（不開前端）

長期記憶固定使用 PostgreSQL／pgvector `MemoryRuntime`。一般後端使用 `MEMORY_DATABASE_URL` 與固定 `MEMORY_DATABASE_SCHEMA`；`backend/tools/chat_test_cli.py` 會自動啟動同一份後端程式的隔離 instance，使用與正式資料庫不同的 `MEMORY_TEST_DATABASE_URL`，每次建立獨立 schema 並套用 Alembic migration。執行前需設定獨立的 `EMBEDDING_AI_API_KEY`、`EMBEDDING_AI_BASE_URL`、`EMBEDDING_AI_MODEL`、`EMBEDDING_AI_DIMENSION=1024`。本地 vLLM 可另外設定 `EMBEDDING_AI_SERVING_MODEL` 及 query/document prefixes。缺少測試 DB、測試 DB 指向正式 database 或 schema 不合法時，CLI 會在啟動後端前失敗；測試完成後清理該 schema。CLI 逐輪等待記憶 job 完成（固定 330 秒，獨立於 Chat／Action 的 `--turn-timeout`），擷取回覆、JEV 決策、route、audit 與記憶變更，不讀寫正式長期記憶。

```bash
# 從 repository 根目錄執行：23 個固定 cases ＋ 5 個既有 Chat LLM 新生成案例
backend/.venv/bin/python backend/tools/chat_test_cli.py

# 原樣重播 28-case 快照，不再次生成
backend/.venv/bin/python backend/tools/chat_test_cli.py --scenario backend/log/chat_test_runs/latest/cases.json

# 保留既有 TXT 表情回歸；max-turns 僅用於 TXT
backend/.venv/bin/python backend/tools/chat_test_cli.py --scenario backend/tools/chat_test_scenarios.txt --max-turns 5
```

長期記憶由單一 Memory Agent 逐步搜尋、讀取、提出操作與結案，後端準備小批候選並原子提交；沒有獨立 intake agent。正式 DB 已在備份後升至 Alembic head `0006_single_memory_agent`，正式啟動與目前 schema 相符。

CLI 固定 Rushia。案例的 setup 與 probe 都走 `/ws/chat`；每案 reset 隔離 owner，長期 probe 使用新 session／連線，背景工作結案後再前進。短期組跳過長期接收與召回，長期 probe 停用短期載入、累積與寫入，綜合組核對跨來源證據。案例數與對話輪數分開記錄，執行後以既有 Chat 模型的獨立審查 prompt 比對所有 probe；保存模型、理由與缺漏，仍需人工核對可能的誤判。

每次覆寫固定 `backend/log/chat_test_runs/latest/`（已 gitignore），不新增時間資料夾；既有歷史保留：

- `cases.json` — 接受的完整案例，供精確重播。
- `turns.jsonl` — 唯一逐輪詳細資料；每行一筆，含完整表情、原始／resolved JEV、記憶交易、召回與裁切後實際 Chat messages，以及 probe 的獨立 `semantic_review`。
- `case_states.jsonl` — 每案一筆的結案 DB sources、evidence、relations、audit、jobs 與 memory items，不混入逐輪資料。
- `memory_report.md`、`expression_report.md` — 從 JSONL 衍生的精簡閱讀視圖，不內嵌完整 JSON；需要細查時以 `case_id`＋`turn` 查詢 `turns.jsonl`。
- `run.json`、`server.log` — 案例指紋、生成診斷、執行狀態與清理結果、隔離後端日誌。

獨立六情境 memory-agent 評估使用 `backend/log/memory_agent_runs/latest/`，包含 `run.json`、`memory_agents.json` 與 `memory_agents_report.md`，不會切換 Chat 測試的 `latest`。

摘要先讀兩份 Markdown；需要完整 evidence 時再查逐輪 JSON：

```bash
jq 'select(.case_id == "case_014" and .turn == 7)' backend/log/chat_test_runs/latest/turns.jsonl
```

JEV 需設定 `JEV_AI_API_KEY` 或 `OPENROUTER_API_KEY`。生成沿用 `CHAT_AI_*`，記憶 agent 使用 `MEMORY_AI_*`。測試 schema 與短期／prompt 暫存在結束時清理；失敗保存部分結果。成功送出的輸入不得盲目重送，只有尚未送出時可安全重試（`--retries`，預設 2）。背景 terminal failed 仍保留診斷並繼續獨立觀察，run 不會因此記成成功。

詳見 [記憶測試集設計與實作](docs/AI_VT_Memory_Testset_Design.md)。

## 作品展示

- 作品展示網址（選填）：（待補）
- 評選影片：（待補）

## 限制與未來工作

已知限制：

- Live2D 模型以 Hiyori（SDK 範例模型）調校為主，換其他模型時表情幅度可能需重新調 adapter。
- Cubism SDK 與模型 binary 為 gitignored，新環境需手動放置，無法一鍵重現。
- Chat 僅取最近 8 輪與有界相關記憶；較早但未摘要的細節可能不在當輪上下文。
- TTS 需要本地 Piper 模型檔；未啟用或模型不存在時僅有文字無語音。
- GitHub Actions 分為 `backend-tests`（Ruff + unittest）、`frontend-tests`（Bun lockfile、lint、契約／runtime 檢查與 build）及 `CodeQL`（Python、JavaScript／TypeScript 安全掃描），均在 push／PR 執行；CodeQL 另有每週排程。Rushia 素材未追蹤，完整資源驗收須在本機執行 `bun run check:rushia-assets`，CI build 不代表模型可載入。
- `bun run build` 在部分 Windows 環境可能出現 `spawn EPERM`（與程式碼正確性無關，重試或換終端機即可）。

未來工作：

- 多模型 expression adapter（Haru 等）與表情差異放大。
- TTS 串流播放與口型（lip sync）對齊優化。
- 更精細的記憶檢索排序與長期情緒趨勢；session 級對話持久化（`CHAT_PERSISTENCE_*` 目前預設關閉）。
- 補上 `LICENSE`，並設定 GitHub branch protection 的 required checks。

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
| Piper TTS 模型 | `backend/models/`（本地檔案） | 由 `piper-tts[zh]` 載入 ONNX 模型，不依賴雲端語音服務 |
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
