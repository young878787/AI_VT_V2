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
CHAT_AI_MODEL=gpt-6-luna
MEMORY_AI_API_KEY=your_memory_key
MEMORY_AI_BASE_URL=https://api.openai.com/v1
MEMORY_AI_MODEL=gpt-6-luna
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

---

## 記憶系統說明

長期記憶的唯一真值是 PostgreSQL／pgvector schema，由 Alembic 管理版本。JEV 產生 NONE／BUFFER／PROCESS 分類，PROCESS 由 Memory LLM 產生決策，再由 DB Manager 寫入 owner-scoped tables。`backend/memory/` 只保留短期 session、summary 與 emotion state；`user_profile.json`、`memory_records.json`、`memory.md` 只作明確執行的 legacy importer 輸入。

Chat 每輪使用最近 8 輪與 PostgreSQL 中的有界相關記憶。手動壓縮的對話摘要存入對應 session 的摘要檔，不混入長期記憶。

---

## 授權

本專案尚未聲明授權，作者保留所有權利。
