# AI VTuber

一套以大型語言模型驅動的虛擬主播系統，讓 AI 即時操控 Live2D 角色的表情與動作，並透過持久化記憶系統在多次對話間記住使用者的喜好與共同回憶。

> 本專案持續維護與擴充中。

---

## 核心能力

- AI 根據對話內容即時驅動 Live2D 模型的表情參數（眼睛、眉毛、嘴角、臉紅、頭部動作）。
- 每次回覆均搭配 AI 自行設計的獨特表情，透過工具呼叫（Tool Calls）實現。
- 持久化記憶系統能跨對話記住使用者的個性特徵、喜好及重要事件。
- 當對話歷史趨近模型的 token 上限時，系統自動壓縮舊訊息並產生摘要寫入記憶。

---

## 架構

```
AI_VT_V2/
├── backend/                   # Python FastAPI 後端
│   ├── main.py                # WebSocket 伺服器、LLM 協調、記憶系統
│   ├── requirements.txt       # Python 相依套件
│   └── memory/                # 持久化記憶（已 gitignore，執行時自動建立）
│       ├── user_profile.json  # 使用者個性與喜好資料
│       └── memory.md          # 帶時間戳記的事件日誌
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
| LLM | OpenRouter API（模型可設定） |
| 記憶 | JSON + Markdown 純文字檔 |

---

## 運作流程

1. 使用者在聊天面板輸入訊息。
2. 前端透過 WebSocket 傳送至 Python 後端。
3. 後端動態組裝 System Prompt（包含使用者畫像與共同回憶），透過 OpenRouter 呼叫 LLM。
4. LLM 使用結構化工具呼叫決定表情參數（`set_ai_behavior`），並視情況更新記憶（`update_user_profile`、`save_memory_note`）。
5. 後端將表情資料與串流文字同步回傳給前端。
6. 前端以平滑插值的方式將表情參數套用至 Live2D 模型。

---

## 安裝與啟動

### 前置需求

- Python 3.10+
- Node.js 18+
- Cubism SDK for Web（放置於專案根目錄，命名為 `CubismSdkForWeb-5-r.5-beta.3/`）
- [OpenRouter](https://openrouter.ai) API 金鑰

### 環境變數設定

將 `.env.example` 複製為 `.env` 並填入金鑰：

```
OPENROUTER_API_KEY=your_key_here
```

### 後端啟動

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python main.py
```

WebSocket 伺服器啟動於 `ws://localhost:8000/ws/chat`。

### 前端啟動

```bash
cd vtuber-web-app
npm install
npm run dev
```

在瀏覽器開啟 `http://localhost:5173`。

---

## 記憶系統說明

AI 在 `backend/memory/`（已排除版本控制）維護兩個持久化檔案：

- `user_profile.json` — 記錄使用者的核心特徵、溝通風格、興趣與討厭的事物。
- `memory.md` — 以追加方式記錄重要對話事件，附帶時間戳記。

當對話歷史接近模型的 token 上限（約 230,000 tokens）時，系統會自動將較舊的訊息壓縮為摘要，並寫入 `memory.md`，以維持上下文視窗的可用空間。

---

## 授權

本專案尚未聲明授權，作者保留所有權利。
