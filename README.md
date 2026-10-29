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
│   ├── requirements.txt       # 舊版 pip／相容安裝清單
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

Python 主環境的依賴以根目錄 `pyproject.toml` 宣告、由 `uv.lock` 鎖定；`backend/requirements.txt` 保留給既有 pip／CI 流程。前端依賴見 `vtuber-web-app/package.json`。Live2D 渲染使用 Cubism SDK for Web 5（見下方第三方素材）。

## 安裝與執行

```bash
# 1. 前置需求：Python 3.12、uv、Node.js 18+、JEV 與 CHAT／MEMORY 路線所需的 API 金鑰
# 2. 將 Cubism SDK for Web 解壓縮至專案根目錄，命名為 CubismSdkForWeb-5-r.5-beta.3/
#    （gitignored，需手動放置；另有 MotionSync plugin 目錄，同為 gitignored）

# 3. 環境變數：複製 .env.example 為 .env 並填入金鑰
cp .env.example .env
# 三條路線各自填入所用端點的金鑰、URL、模型，例如：
# OPENROUTER_API_KEY=your_openrouter_key  # JEV_AI_API_KEY 留空時沿用
# CHAT_AI_API_KEY=your_chat_key
# CHAT_AI_BASE_URL=https://api.openai.com/v1
# CHAT_AI_MODEL=gpt-6-luna
# MEMORY_AI_API_KEY=your_memory_key
# MEMORY_AI_BASE_URL=https://api.openai.com/v1
# MEMORY_AI_MODEL=gpt-6-luna
# BACKEND_PORT=15287
# FRONTEND_PORT=18080
# JEV 固定使用 SystemOne 請求格式；可用 JEV_AI_BASE_URL、JEV_AI_MODEL 覆寫預設。

# 4. 建立／同步根目錄主環境（Linux/macOS）
uv sync
source .venv/bin/activate
cd backend
python main.py
# WebSocket 伺服器：ws://localhost:${BACKEND_PORT}/ws/chat

# Windows PowerShell：在專案根目錄執行
# uv sync
# .\.venv\Scripts\Activate.ps1
# cd backend
# python main.py

# 5. 啟動前端（另開一個終端機，於專案根目錄）
cd vtuber-web-app
bun install
bun run dev
# 瀏覽器開啟 http://localhost:${FRONTEND_PORT}

# TTS（可選）：將 Piper 模型與 .onnx.json 放入 backend/models/，
# 並在 .env 設 TTS_ENABLED=true、PIPER_MODEL_PATH 等本地參數
```

## Headless Chat 測試（不開前端）

長期記憶使用 PostgreSQL／pgvector。唯一入口 `backend/tools/chat_test_cli.py` 會啟動同一份 `main:app` 的隔離 instance，使用與正式 DB 不同的 `MEMORY_TEST_DATABASE_URL` 及臨時 `test_<32 lowercase hex>` schema，套用目前 Alembic head，完成後清理。需要現有 `CHAT_AI_*`、`JEV_AI_*`、`MEMORY_AI_*`、`EMBEDDING_AI_*` 設定；不操作正式資料庫。不帶 `--suite` 只顯示使用方式。

```bash
uv run backend/tools/chat_test_cli.py --suite chat
uv run backend/tools/chat_test_cli.py --suite memory
uv run backend/tools/chat_test_cli.py --suite tools
uv run backend/tools/chat_test_cli.py --suite integration

# 輸入重播，不重新生成續問
uv run backend/tools/chat_test_cli.py --suite integration --scenario backend/log/ai_tests/integration/latest/cases.json

# 定位題目，包含必要前序：mem_003、mem_005、mem_006
uv run backend/tools/chat_test_cli.py --suite memory --case mem_006
```

`chat` 7 題、`memory` 14 題（13 核心＋1 容量）、`tools` 7 題、`integration` 15 題（13 固定＋2 生成）。一題只有一次實際 user input；前置 history／summary／corpus 不經 setup 聊天，操作後召回保留真實提交結果。記憶題的 query embedding、接收、路由、agent 及交易沿用正式流程，初始 document 向量固定快取並驗證文字及 embedding contract。客觀前置條件失敗時，相依題記為 failed、request_sent=false 並不送出，不重灌預期答案；回答語意與新增／補強品質不控制後續執行。`--case` 保留全組分母，不宣稱整組通過。舊 28-case、TXT、`--max-turns`、`--model` 及重複評估 runner 已移除。

每組固定覆寫 `backend/log/ai_tests/<suite>/latest/`，保留既有歷史產物。先讀 `run.json` 與單一 `report.md`；完整證據位於 `turns.jsonl`、`case_states.jsonl`，重播輸入與 oracle 位於 `cases.json`，人工檢視存於 `review.json`，必要錯誤診斷位於 `server.log`。報告包含完整對話、JEV 原始／resolved／fallback、工具 receipt、圖片來源及召回／投影／實際 prompt 證據，不在 JSON 內嵌圖片 base64。

專項工具使用合成 fixture；整合正常工具使用 live 唯讀來源，僅 `it_012` 注入明示 vision 拒收替身。必要 live 工具未取得資料、未按契約交付圖片或最終缺少必要客觀證據時為 failed。實機私人圖片不寫入持久報告；缺少可授權複核圖片時，人工視覺判定保持 pending，與工具／交付客觀結果分開。測試不證明前端動作、語音、嘴型或 OBS 已通過。

執行完成、客觀硬條件、容量與人工判定分開，移除 blocked 狀態。必要候選缺漏、禁止候選注入、必要工具未執行或失敗及交易／隔離證據失敗才控制相依題；預期故障依案例契約驗證。測試不呼叫語意 Judge、不產生評分或理由，完整對話與新增／補強等交易證據留待後期人工評讀。容量未達影響 overall；整合全部 15 題人工 accepted 前保持 pending。已送出的寫入不重送。人工檢視填入 `review.json` 的 reason、turn_id／evidence 後，可離線重建報告：

```bash
uv run python -c "import sys; from pathlib import Path; sys.path.insert(0,'backend'); from tools.ai_test_report import rebuild_report; rebuild_report(Path('backend/log/ai_tests/integration/latest'))"
```

重建只讀保存的資料，不呼叫模型。人工判定綁定 cases／turns／case_states 指紋，重新執行即失效。規格及驗收證據見 [AI_VT 分組測試計畫與驗收規格](docs/AI_VT_分組測試計畫與驗收規格.md)；實作完成與真實模型能力達標分開記錄。

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
- 更精細的記憶檢索排序與長期情緒趨勢；session 級對話、摘要與情緒狀態固定保存於 PostgreSQL。
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
| React / Vite (rolldown-vite) / Zustand / FastAPI / uvicorn 等開源套件 | 見 `vtuber-web-app/package.json`、`pyproject.toml`、`uv.lock` | 各自遵循 MIT / Apache-2.0 等開源授權 |

本 repo 不含任何 API 金鑰、Token 或個人資料；`backend/memory/` 已 gitignore。

## 團隊成員

| 姓名 | 分工 |
| --- | --- |
| young87878 | （待補） |
| Rushia | （待補） |

> 註：上表人名取自本 repo Git 歷史 contributor，實際分工待維護者補充。

## License

本專案根目錄尚未加入 `LICENSE` 檔案，目前為作者保留所有權利（All rights reserved）。若要開源，建議補上 `LICENSE`（如 MIT）並在此標示授權名稱。
