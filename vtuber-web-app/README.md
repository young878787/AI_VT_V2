# Rushia Live2D 前端

React + TypeScript + Zustand 前端，使用 Cubism Web SDK 顯示 Rushia，並播放後端編譯的 `expression_plan`。主畫面以半身角色與對話為主，表情調校及進階設定放在抽屜內。

## 啟動與資源

在本目錄執行：

```bash
bun install
bun run dev
```

前端讀取專案根目錄 `.env`；`FRONTEND_PORT` 預設為 `5173`，`BACKEND_PORT` 預設為 `9999`。目前聊天與表情預覽連線使用 `localhost`，後端啟動及其設定請參閱[專案 README](../README.md)。修改連線設定後需重新啟動 Vite；正式 build 的前端設定於建置時載入。

單獨啟動前端可觀看模型；聊天及表情工作室的「產生／換演法」需要後端。模型與 SDK 靜態資源須備齊：

| 路徑（相對本目錄） | 用途 |
| --- | --- |
| `public/Resources/Rushia/RushiaHD.model3.json` | 模型入口，引用 Moc、貼圖、物理、原生動作與表情 |
| `public/Core/live2dcubismcore.js` | Cubism Core，由 `index.html` 載入 |
| `public/MotionSyncCore/live2dcubismmotionsynccore.js` | `index.html` 引用的 MotionSync Core；目前 TTS 口型由 `TTSPlayer` 音量分析控制 |
| `public/Shaders/WebGL/` | WebGL shader 資源 |
| `src/live2d/framework/` | Cubism Framework；不直接修改 SDK 原始碼 |

模型固定為 **Rushia**，前端沒有選模型、匯入或刪除模型功能。[LAppDefine.ts](src/live2d/LAppDefine.ts) 的 `FixedModel` 與 [LAppLive2DManager.ts](src/live2d/LAppLive2DManager.ts) 負責唯一模型的載入；載入失敗會顯示錯誤，不切換到其他角色。後端聊天輸入及表情 debug API 也固定使用 Rushia。

## 畫面與操作

- **主舞台**：桌機為角色與右側對話，窄螢幕改為上下排列。取景由 [modelFraming.ts](src/live2d/modelFraming.ts) 依畫布比例計算，以頭頂留白為縮放錨點；手動縮放為預設構圖的 `0.75～1.25` 倍。
- **重置構圖**：回復預設位置與縮放。模型拖移預設關閉，可在設定中開啟；開啟後可拖移及使用滾輪縮放。
- **設定**：包含畫面與互動、原生參數、情緒狀態。抽屜開關不改變舞台畫布尺寸。
- **表情工作室**：預覽日常表情、情緒及動作，提供輕柔／自然／鮮明三種強度。

表情工作室的操作差異：

| 操作 | 行為 |
| --- | --- |
| 選擇表情 | 呼叫後端編譯，再由前端播放 |
| 重播這次表情 | 重用已保存的 plan，不再次呼叫 API |
| 換個演法 | 增加 seed，帶上上一份 `carryState`，重新編譯以避開上一個變體 |
| 回到平靜 | 編譯並播放 `calm` 家族 |

「重播」保證重用相同計畫；角色起始姿態、眨眼、視線與物理狀態仍可能不同，因此不保證逐幀畫面完全一致。摘要中的眼睛、眉毛等數值是 **base pose**，短暫閉眼或張嘴要查看後續事件及實際畫面。

## 表情資料流與分工

```text
聊天：JEV 情緒／互動態度 → intent 正規化 ─┐
                                        ├→ compile_expression_plan()
工作室：debug fixture 或直接 intent ────┘       → Rushia profile
                                                → expression_plan
                                                      ↓
                聊天 WebSocket 或工作室 HTTP response
                                                      ↓
           型別驗證 → ActionScheduler → appStore → LAppModel → Cubism

語音：voice 訊息 → TTSPlayer → isSpeaking 與口型值 → LAppModel
```

| 責任 | 主要檔案 |
| --- | --- |
| 聊天 JEV 結果映射、送出 plan、保存跨輪 `carryState` | [chat_ws.py](../backend/api/routes/chat_ws.py) |
| 表情編譯公共入口，Rushia 分流 | [expression_compiler.py](../backend/services/expression_compiler.py) |
| Rushia 基準姿態、變體、短事件、收尾與待機 | [rushia_expression_profile.py](../backend/domain/rushia_expression_profile.py) |
| 不呼叫 AI 的表情預覽 API | [expression_debug_router.py](../backend/api/routes/expression_debug_router.py) |
| TypeScript 契約與 runtime validator | [expressionPlan.ts](src/types/expressionPlan.ts) |
| 聊天事件接收與過期回合過濾 | [wsService.ts](src/services/wsService.ts) |
| 優先權、中斷、pending、完成通知 | [actionScheduler.ts](src/services/actionScheduler.ts) |
| 把 plan 分送到模型控制方法 | [appStore.ts](src/store/appStore.ts) |
| 逐幀合成與最終原生參數寫入 | [LAppModel.ts](src/live2d/LAppModel.ts) |
| 音訊播放、音量口型、結束與取消清理 | [TTSPlayer.ts](src/audio/TTSPlayer.ts) |

後端決定「演什麼」，前端負責「如何連續播放」。前端不直接根據聊天文字選一組固定表情，也不讓 AI 直接操作 WebGL。

### Plan 的主要欄位

| 欄位 | 用途 |
| --- | --- |
| `basePose` | 基準臉部／身體姿態、持續時間與身體動作風格 |
| `sequence` | 依序播放的局部參數事件，支援淡入、淡出與重疊 |
| `microEvents` | 額外疊加的短事件；目前 Rushia profile 使用 `sequence`，此陣列為空 |
| `motionPlan` / `eyeMotionPlan` | 程序化身體／頭部動作與細微眼動 |
| `blinkPlan` | 自動眨眼的恢復、間隔與暫停命令 |
| `idlePlan` | 延遲進入的收尾姿態與低幅循環 |
| `carryState` | 後端下輪選擇的連續性資料，包含上一個家族及變體 |
| `debug` / `modelHints` / `timingHints` | 調校、診斷與時序資訊 |

## Rushia 表情設計

目前共有 **13 個家族、32 個變體**，定義集中於 `rushia_expression_profile.py` 的 `FAMILY_POSES` 與 `FAMILY_VARIANTS`。

| 家族 | 變體數 | 表現重點 |
| --- | ---: | --- |
| `calm` | 3 | 平靜、輕微偏視與確認反應 |
| `listening` | 3 | 溫和抬眉、專注與回正視線 |
| `thinking` | 3 | 左右／下方偏視、眉毛與眼睛不對稱 |
| `soft_smile` | 3 | 微笑、輕微臉紅與身體變化 |
| `closed_smile` | 3 | 短暫閉眼笑，接著恢復睜眼 |
| `playful` | 3 | 偷看、左眼或右眼 wink |
| `teasing`、`angry`、`sad`、`gloomy`、`shy`、`surprised`、`conflicted` | 各 2 | 各情緒的視線、眉嘴及局部反應 |

選擇家族時先套用主題保護及負面情緒規則，再處理指定家族與互動態度。例如一般 neutral 回覆使用 `calm`；`awkward`／`tense_hold` 可映射為聆聽，開心可使用柔和微笑或閉眼笑。工作室的 `thinking` 預覽與前端等待回覆時的輕微思考姿態是不同入口；新增家族不代表 JEV 已新增同名分類。

變體使用局部亂數產生器。相同 intent、seed 與 previous state 可重現相同編譯結果；帶入上一輪 `carryState` 時避開立即重複的變體。這是有範圍的姿態選擇，不是任意抖動原生參數。

### 短反應、收尾與語音

- 序列由短反應、安靜間隔組成；聆聽／思考另有回正注意力事件。淡入淡出重疊後，排程以實際最晚事件結束時間計算，不固定截斷於 10 秒。
- 閉眼笑及 wink 的閉眼事件為 `780 ms`，淡入 `100 ms`、淡出 `150 ms`，並短暫暫停自動眨眼，避免兩者相互干擾。
- `idlePlan` 在動作／估計說話時間後增加 `500～850 ms` 收尾，再進入低幅待機。負面情緒保留較淡的原情緒，不強制切成笑臉。
- 工作室播放優先於一般聊天；pending 只保留最新候選。新回合、取消與斷線會清理舊動作及語音，避免舊計畫重新啟動。
- 語音播放時，口型由音訊分析優先控制；非語音時可使用 `mouthOpenBias` 做驚訝張嘴。自然播放結束後以約 `180 ms` 的時間淡出閉嘴，取消則立即清零。

**目前的時序限制：**聊天中的表情計畫與文字生成並行，編譯時尚未拿到完整回覆。只有 intent 帶有 `spoken_text`／`dialogue_text` 時，profile 才會估計句長並加入較長回覆的節奏。Scheduler 在計畫到期且仍播放語音時會延後完成通知，但不會重新安排整份表情序列；`stream_end` 也只代表文字完成。現階段不是逐字／逐句對齊語音的演出系統，`arc` 目前保留於 debug，尚無各 arc 專屬的 Rushia 序列分支。

### 原生資源與參數限制

`RushiaHD.model3.json` 引用的 `.motion3.json`／`.exp3.json` 會被載入，但聊天演出主要使用上面的程序化 plan。`motionPlan` 不是原生 motion 檔名；新增這些表情不需要另外產生 `.exp3.json`，也不代表聊天會自動選播模型附帶的原生動作。

- Rushia 沒有 `ParamEyeLSmile`、`ParamEyeRSmile`、`ParamTere`。笑眼透過短暫閉合眼睛表現，臉紅使用 `ParamCheek`；契約中的 `eyeLSmile`／`eyeRSmile` 目前保留且 Rushia 輸出為 `0`。
- 眼睛開合與 `mouthOpenBias` 為 `0～1`，嘴形為 `-1～1`。設計新參數時須核對模型實際原生範圍。
- 左右眉角不能直接填相同符號：已校準的生氣為左正／右負，難過為左負／右正。Rushia pose 使用 `eyeSync=false`，保留各眼與各眉的獨立值。
- `headIntensity` 控制程序化頭部擺動強度，不是可任意放入 micro-event 的頭部角度。現有名稱含 `nod` 的部分變體使用 `bodyAngleY`，應以畫面判斷動作效果。

`LAppModel.update()` 依序處理原生 motion、表情目標與平滑、自動眨眼、原生 expression、滑鼠／程序化視線、口型、身體與臉部參數、呼吸／物理／pose，最後套用原生參數手動覆蓋。因此原生動作可能被後續控制覆寫；原生參數面板則最後生效，單一覆蓋值在 5 秒無操作後釋放。

## 新增及調整表情

1. 在 `FAMILY_POSES` 定義基準、`FAMILY_VARIANTS` 定義有意義的局部變化。閉眼／張嘴放在有結束時間的事件中，收尾恢復睜眼與閉嘴。
2. 若新增家族，檢查 `_family()` 的情緒／態度映射及 topic guard。供工作室預覽時，同步更新後端 [expression_debug_fixtures.py](../backend/domain/expression_debug_fixtures.py)、前端 [expressionDebugService.ts](src/services/expressionDebugService.ts) 的 kind 型別及 [ExpressionPlanDebugPanel.tsx](src/components/ExpressionPlanDebugPanel.tsx) 按鈕。
3. 優先沿用現有 plan 欄位。真的需要新控制參數時，同步更新契約驗證、預設值、clamp、事件疊加及 `LAppModel` 最終寫入，不只新增 TypeScript 型別。
4. 使用固定 seed 預覽、重播，再換變體比較。先檢查單一原生參數的方向與範圍，再看完整表情；必要時暫停眨眼、視線與物理以隔離問題，完成後恢復它們驗證合成。
5. 查看反應開始、峰值、恢復及待機；連續切換不同表情，測試語音播放與取消，確認沒有卡住的閉眼、張嘴、眉毛或過期動作。

預覽 API：`POST /api/debug/expression-plan`。例如送出：

```json
{
  "kind": "closed_smile",
  "intensity": "normal",
  "seed": 42
}
```

回應包含 `plan` 與 `summary`。要換變體，可把上一份 `plan.carryState` 放入下一個 request 的 `previousState`。此 API 使用 fixture／直接 intent，不呼叫 AI；仍需後端可啟動。若 `EXPRESSION_DEBUG_API_ENABLED=false`，API 回傳 404，工作室無法重新編譯預覽。

## 顯示鏡像 `/display`

主頁運行唯一 Live2D 實例，透過 `createImageBitmap` 與 `BroadcastChannel` 傳送畫面；`/display` 只接收，不重新計算模型。顯示端同步 bitmap 的固有尺寸，再以 `object-fit: contain` 放入設定的輸出尺寸，避免把半身角色拉寬。

測試時保持主頁開啟，在**同一瀏覽器環境、相同 origin** 開啟 `/display`。目前沒有跨瀏覽器程序的影像傳輸；獨立 OBS Browser Source 不會因網址相同就自動收到一般 Chrome 分頁的廣播。已驗證的是同一瀏覽器 context 的雙分頁鏡像，外部 OBS 擷取流程需另外確認。

## 驗證

在本目錄執行前端檢查：

```bash
bun run check:expression-plan
bun run check:emotion-state
bun run check:rushia-runtime
bun run check:rushia-assets
bun run build
```

`build` 依序執行 codegen、TypeScript 與 Vite 建置。`check:rushia-runtime` 涵蓋固定模型設定、取景、眨眼／口型合成、事件重疊、取消、語音結束及低幀率閉嘴；`check:rushia-assets` 另行確認模型 manifest 與所有引用檔案完整，缺檔時直接失敗。Rushia 素材未納入 Git，因此 `frontend-tests` CI 執行純程式檢查，資源驗收保留本機；build 通過不代表模型可載入。這些檢查都不能取代實際 WebGL 視覺驗證。

後端表情回歸可在**專案根目錄**執行：

```bash
backend/.venv/bin/python -m unittest \
  backend.tests.test_rushia_expression_profile \
  backend.tests.test_expression_compiler \
  backend.tests.test_model_registry
```

視覺驗收至少包含桌機、平板、手機構圖，抽屜開關不裁頭，以及閉眼笑／wink 恢復、思考偏視、負面情緒眉角、驚訝張嘴、快速切換和語音結束。每次調整都應回看畫面，不以參數數值或 build 通過代替表情品質判斷。

本地若保留 `../logs/rushia-visual-review/`，其中有最近一次的畫面、表情／轉換對照及 `validation.json`。此目錄不納入版本控制，也不由上面的測試命令自動重新產生；既有證據使用隔離後端與本機音訊，不代表真實 AI／雲端 TTS 完整對話已驗證。
