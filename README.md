# AI VTuber

## 問題與目標

市面上多數 AI 虛擬角色只能「對話配固定動作」：回覆文字是生成的，表情卻是預設幾個制式動作輪播，看久了重複、無聊，也無法反映語氣與心情。傳統 VTuber 則需要真人即時操控表情與回應，開播成本高、無法 24 小時互動。

本專案的核心是 **AI 生成心情對應**：讓 LLM 每次都根據對話語境即時生成當下的心情與表情參數（眼睛、眉毛、嘴、臉紅、頭部角度、呼吸），而非觸發固定動作。同樣的話在不同情緒下會有不同表情，且帶有隨機性與多元變化，並跨對話記住使用者偏好與共同回憶。

目標使用者為想打造個人 AI 主播、互動式虛擬角色的開發者與創作者。預期影響是提供一套開源、可自託管（self-hosted）、可更換 LLM provider 的即時 AI VTuber 基座：前端負責渲染，後端負責對話、表情編譯與記憶。

## 核心功能

- AI 生成心情對應（核心差異）：每次回覆都由 LLM 依語境即時生成心情與表情參數，而非播放固定動作；同樣的話在開心、害羞、生氣時表情不同，每次皆有隨機性與多元變化。
- 即時表情驅動：LLM 透過結構化 tool call（`set_ai_behavior` / expression plan）同時輸出回覆文字與表情參數（眼睛、眉毛、嘴、臉紅、頭部角度、呼吸）。
- 獨特表情編譯：後端 `compile_expression_plan()` 將 AI 意圖轉為前端可播放的 expression plan，含平滑插值過渡。
- 持久化記憶：`backend/memory/` 下以 `user_profile.json` 存使用者特徵偏好，以 `memory.md` 追加記錄重要事件，跨 session 有效。
- 上下文自動壓縮：對話歷史接近 token 上限（約 230,000 tokens）時，自動摘要舊訊息並寫入記憶，維持 context window 可用。
- 可選 TTS 語音：支援 Google Cloud TTS（Chirp 3 HD），可開關（`TTS_ENABLED`）。
- 手動除錯面板：ControlPanel / ModelParamPanel 可手動調參、即時檢視 Live2D 參數。

## 系統架構

```text
使用者輸入
  → React 前端 (AIChatPanel → wsService)
  → WebSocket → Python FastAPI 後端 (/ws/chat)
  → 組裝 System Prompt（含 user_profile + memory）
  → LLM Provider (OpenRouter / NVIDIA / Google / Qwen)
  → Tool Calls：表情意圖 + 記憶更新
  → compile_expression_plan()
  → WebSocket 回傳：串流文字 + expression_plan
  → 前端 LAppModel 套用至 Live2D 模型（平滑插值）
```

目錄結構（重點）：

```text
AI_VT_V2/
├── backend/                   # Python FastAPI 後端
│   ├── main.py                # WebSocket server 進入點
│   ├── requirements.txt       # Python 相依套件
│   └── memory/                # 持久化記憶（gitignored，執行時自動建立）
│       ├── user_profile.json  # 使用者個性與喜好
│       └── memory.md          # 帶時間戳記的事件日誌
└── vtuber-web-app/            # React + TypeScript + Vite 前端
    └── src/
        ├── components/        # AIChatPanel / ControlPanel / Live2DCanvas 等
        ├── live2d/            # Cubism SDK 整合層，核心為 LAppModel.ts
        ├── services/wsService.ts  # 後端 WebSocket 客戶端
        └── store/appStore.ts  # Zustand 全域狀態
```

前端、後端、模型、資料與外部服務協作方式：

1. 使用者在聊天面板輸入訊息，前端經 WebSocket 送至後端。
2. 後端載入 `user_profile.json` 與 `memory.md`，動態組裝 System Prompt，再呼叫所選 LLM provider。
3. LLM 回傳結構化 tool call：表情意圖（`set_ai_behavior`）與記憶更新（`update_user_profile`、`save_memory_note`）。
4. 後端經 expression compiler 產生 `expression_plan`，與串流文字一併回傳前端。
5. 前端 `LAppModel` 以平滑插值將參數套用至 Live2D 模型；記憶檔為 JSON + Markdown 純文字檔，無額外資料庫。

## 使用技術

| 類型 | 技術／服務 | 用途 |
| --- | --- | --- |
| AI 模型 | OpenRouter / NVIDIA Build / Google AI Studio (Gemini) / 阿里雲 Qwen（DashScope，相容 OpenAI API） | 對話生成、表情意圖與記憶更新 tool call |
| 前端 | React 19、TypeScript、Vite（rolldown-vite）、Zustand | UI、Live2D 渲染、WebSocket 客戶端、全域狀態 |
| 後端 | Python、FastAPI、WebSocket（uvicorn）、tiktoken | 對話編排、expression compiler、記憶系統、token 估算 |
| Sponsor 技術 | 阿里雲 Qwen（DashScope）、Google Cloud Text-to-Speech（Chirp 3 HD） | LLM 對話備選模型、語音合成（可選） |

完整相依請見 `backend/requirements.txt` 與 `vtuber-web-app/package.json`。Live2D 渲染使用 Cubism SDK for Web 5（見下方第三方素材）。

## 安裝與執行

```bash
# 1. 前置需求：Python 3.10+、Node.js 18+、任一 LLM provider 的 API 金鑰
# 2. 將 Cubism SDK for Web 解壓縮至專案根目錄，命名為 CubismSdkForWeb-5-r.5-beta.3/
#    （gitignored，需手動放置；另有 MotionSync plugin 目錄，同為 gitignored）

# 3. 環境變數：複製 .env.example 為 .env 並填入金鑰
cp .env.example .env
# 至少填寫其一，例如：
# AI_PROVIDER=openrouter
# OPENROUTER_API_KEY=your_key_here
# BACKEND_PORT=9000
# FRONTEND_PORT=5287

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

## 作品展示

- 作品展示網址（選填）：（待補）
- 評選影片：（待補）

## 限制與未來工作

已知限制：

- Live2D 模型以 Hiyori（SDK 範例模型）調校為主，換其他模型時表情幅度可能需重新調 adapter。
- Cubism SDK 與模型 binary 為 gitignored，新環境需手動放置，無法一鍵重現。
- 對話歷史接近 token 上限時依賴自動摘要，超長記憶仍可能遺失細節。
- TTS 需要 Google Cloud ADC 登入，未設定則僅有文字無語音。
- 目前無 CI，`npm run build` 在部分 Windows 環境可能出現 `spawn EPERM`（與程式碼正確性無關，重試或換終端機即可）。

未來工作：

- 多模型 expression adapter（Haru 等）與表情差異放大。
- TTS 串流播放與口型（lip sync）對齊優化。
- 記憶檢索排序與 session 級對話持久化（`CHAT_PERSISTENCE_*` 目前預設關閉）。
- 補上 `LICENSE` 與 CI（lint / typecheck / backend unittest）。

## 第三方服務、資料與素材

| 項目 | 來源／連結 | 授權／備註 |
| --- | --- | --- |
| Cubism SDK for Web 5（`CubismSdkForWeb-5-r.5-beta.3/`，根目錄，gitignored） | https://www.live2d.com/en/sdk/ | Live2D 專有授權，需自行下載，勿提交至 repo |
| MotionSync Plugin（`CubismSdkMotionSyncPluginForWeb-5-r.2/`，gitignored） | https://www.live2d.com/en/sdk/ | 同上 |
| Hiyori 範例模型（`vtuber-web-app/public/Resources/Hiyori/`） | 隨 Cubism SDK 附帶之範例 | 僅供展示／開發測試，請遵循 Live2D 範例素材規範 |
| OpenRouter API | https://openrouter.ai | 需自備 `OPENROUTER_API_KEY`，金鑰勿提交 |
| NVIDIA Build API | https://build.nvidia.com | 需自備 `NVIDIA_API_KEY` |
| Google AI Studio（Gemini, OpenAI 相容端點） | https://ai.google.dev/gemini-api/docs/openai | 需自備 `GOOGLE_API_KEY` |
| 阿里雲 Qwen（DashScope 相容模式） | https://www.alibabacloud.com/help/en/model-studio/ | 需自備 `QWEN_API_KEY` |
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
