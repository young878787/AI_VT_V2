# Expression Intent Compiler 重構設計稿

> 日期：2026-04-25
> 狀態：Proposed
> 目標：將現有 Expression Agent 從「直接輸出低階 Live2D 工具呼叫」重構為「輸出高階表演意圖」，並由 backend 的 Expression Compiler 編譯成可播放的動作套組與 runtime payload，以提升穩定性、可測試性與表情豐富度。

## 1. 背景與問題定義

目前系統在名義上已經將 Agent A / Agent B-1 / Agent B-2 拆開，但 Expression 這條路徑仍保留舊型態：

1. Expression Agent 直接輸出 `set_ai_behavior` 與 `blink_control`
2. backend 解析 tool arguments 後，組成單一 `behavior` payload
3. frontend 將模型臉部 lerp 到一個 target state，再額外疊加 blink

這個設計在功能上可用，但已經出現以下結構性瓶頸：

1. 模型必須直接輸出低階參數，容易保守、缺欄位、輸出壞 JSON
2. parser 雖然可以補救部分格式錯誤，但本質仍是修壞掉的低階輸出
3. backend 目前主要保存單一最終表情狀態，無法良好承接短促表情節奏
4. frontend runtime 目前偏單一 target state，表演層次有限
5. 未來 tools 會持續擴充，若每一種表演能力都直接暴露給模型，維護與調教成本會快速上升

因此，本次重構的核心不是再增加更多 Agent，而是重新劃分責任層級：

1. 模型負責輸出表演意圖
2. backend 負責編譯、補齊、組合、容錯
3. frontend runtime 負責播放與微動態呈現

## 2. 現況觀察

### 2.1 backend 現況

目前 `backend/api/routes/chat_ws.py` 的主要流程為：

1. 呼叫 Dialogue Agent 取得角色對白與 `emotion_state`
2. 組裝 Expression Agent prompt 與 Memory Agent prompt
3. 並行呼叫 Expression Agent / Memory Agent
4. 透過 `extract_agent_tool_calls()` 解析工具呼叫
5. 在 `_execute_chat_orchestrator_tool_calls()` 中直接處理：
   - `set_ai_behavior`
   - `blink_control`
   - memory tools
6. 組成 `behavior` payload 後發給 websocket 與 display

目前 Expression 路徑的特性是：

1. 工具解析與執行仍以低階參數為中心
2. 缺少一層正式的 expression semantic model
3. 若 `set_ai_behavior` 缺失，系統只能退回上輪或預設 payload

### 2.2 parser 現況

`backend/services/tool_arg_parser.py` 目前能處理：

1. leading-zero decimals
2. Python literals (`True` / `False` / `None`)
3. trailing commas
4. 部分欄位 salvage

但這層的本質仍是在救「模型直接輸出的低階工具參數」，並未真正降低輸出複雜度。

### 2.3 frontend 現況

`vtuber-web-app/src/live2d/LAppModel.ts` 目前主要採用：

1. 單一 AI expression target state
2. 以 lerp 漸進靠近 target
3. 眨眼作為額外疊層
4. 最後把參數寫回 Live2D model

目前 runtime 已能處理基礎表情與 blink 疊加，但對於：

1. micro expression
2. staged expression arc
3. event queue
4. deterministic subtle variation

都還沒有正式結構。

## 3. 重構目標

本次方案的目標如下：

1. 讓 Expression Agent 不再直接輸出低階 Live2D 工具參數
2. 導入 `Expression Intent` 作為新的中介語義層
3. 新增 `Expression Compiler`，將 intent 編譯成動作套組與 runtime payload
4. 讓 backend 的容錯重心從「修壞 JSON」轉向「修不完整 intent」
5. 為未來的 micro-expression、sequence、更多動作工具保留可擴充邊界
6. 降低 prompt 工程對低階 schema 的耦合度

### 3.1 擴充性目標

此次重構除了改善目前穩定性與表情層次，也必須刻意留下後續擴充的介面，避免下一次再回到「直接把新工具暴露給模型」的做法。

具體擴充性目標如下：

1. 新增表演能力時，優先透過 compiler 與 preset library 擴充，而不是先修改模型 prompt 讓模型直接學工具細節
2. 新增前端 runtime 能力時，應能以新 payload 欄位或新 event type 向下相容擴充
3. 新增模型或角色時，應能沿用同一套 intent schema，只替換 preset / mapping 配置
4. 新增更高階的 planning 或 moderation 層時，不應破壞既有 intent -> compiler -> runtime 主幹
5. 未來不論由人類或 AI 接手，都能快速判斷「某個新需求應插在哪一層」

### 3.2 非目標

為了避免未來擴充時再次失焦，以下內容不屬於本次重構的直接目標：

1. 不在第一版就建立完整 animation scripting language
2. 不在第一版就把所有可能的 Live2D 工具都抽象化
3. 不在第一版就支援多 Agent 協作式表演規劃
4. 不要求所有未來擴充點先實作完成，但必須先定義清楚插入位置

## 4. 架構決策

### 4.1 新的責任分層

重構後，Expression 相關鏈路調整為：

1. `Dialogue Agent`
   - 產生角色回覆
   - 產生 `emotion_state`

2. `Expression Agent`
   - 根據 user message、AI reply、`emotion_state`、上一輪表情摘要
   - 輸出 `Expression Intent`

3. `Expression Compiler`
   - 驗證 intent
   - 補齊預設值
   - 選擇 base pose preset
   - 組合 micro-expression / sequence / blink strategy
   - 產生 runtime payload

4. `Runtime Playback`
   - 播放 base pose
   - 疊加 micro events
   - 套用 blink plan
   - 處理 deterministic variation

### 4.2 明確不採用的方向

本次不採用「再把 Expression Agent 拆成更多 skills / 子 Agent」作為第一優先。

原因：

1. 目前主要問題不是 Agent 不夠細，而是模型直接承擔低階輸出責任
2. 繼續拆 Agent 只會讓 orchestrator 更複雜
3. 若不改輸出型態，拆更多 Agent 仍會遇到同樣的格式脆弱性與表情保守問題

### 4.3 Extension Points 設計原則

為了讓未來擴充時不需要重新大改主幹，所有新能力盡量優先落在以下 extension points：

1. `Expression Intent schema`
   - 新增高階語義欄位
   - 例如：`gaze_mode`、`reaction_speed`、`dramatic_weight`

2. `Preset libraries`
   - 新增 base pose / micro event / sequence / blink strategy 模板
   - 不改 orchestration 主流程

3. `Compiler modifiers`
   - 新增 mapping rule、style modifier、fallback rule
   - 不改 Agent 呼叫流程

4. `Runtime event handlers`
   - 新增新的 event type 或播放規則
   - 不改 intent parser 與 agent prompt 主體

5. `Model-specific adapters`
   - 針對不同 Live2D 模型做參數映射差異
   - 不改 intent schema 本身

原則上，若未來新增需求能落在上述任一 extension point，就不應回頭讓模型直接處理低階工具細節。

## 5. Expression Intent 設計

### 5.1 設計原則

Intent schema 需要滿足以下條件：

1. 比低階 tool args 短
2. 仍足以控制表情風格與節奏
3. 容易驗證與補值
4. 可映射到 preset、event 與 blink strategy
5. 不要求模型理解具體 Live2D 參數細節

### 5.2 第一版建議欄位

第一版建議採用以下欄位：

```json
{
  "primary_emotion": "playful",
  "secondary_emotion": "teasing",
  "intensity": 0.72,
  "energy": 0.68,
  "dominance": 0.55,
  "playfulness": 0.84,
  "warmth": 0.41,
  "asymmetry_bias": "strong",
  "blink_style": "teasing_pause",
  "tempo": "snappy",
  "arc": "pop_then_settle",
  "hold_ms": 1800,
  "must_include": ["smirk_left"],
  "avoid": ["fully_neutral"],
  "speaking_rate": 1.08
}
```

### 5.3 欄位分級

#### 核心必要欄位

1. `primary_emotion`
2. `intensity`
3. `energy`
4. `arc`
5. `hold_ms`

#### 建議欄位

1. `secondary_emotion`
2. `dominance`
3. `playfulness`
4. `warmth`
5. `asymmetry_bias`
6. `blink_style`
7. `tempo`
8. `speaking_rate`

#### 可選控制欄位

1. `must_include`
2. `avoid`

### 5.4 欄位用途

這些欄位的作用不是直接控制眉毛與嘴角數值，而是提供足夠的編譯控制訊號：

1. `primary_emotion`：決定 base pose 類別
2. `intensity`：決定幅度範圍
3. `energy`：決定動態感、短促事件與 blink 節奏
4. `dominance` / `playfulness` / `warmth`：決定風格修飾分支
5. `asymmetry_bias`：決定是否優先做左右不對稱
6. `arc`：決定是否產生 staged sequence
7. `must_include` / `avoid`：提供少量人工可控的語義錨點

## 6. 動作套組設計

### 6.1 Base Pose Preset Library

建立一批基礎表情模板，例如：

1. `neutral_soft`
2. `gentle_smile`
3. `playful_smirk`
4. `teasing_side_smile`
5. `shy_tucked`
6. `annoyed_flat`
7. `angry_sharp`
8. `surprised_open`
9. `embarrassed_tense`
10. `conflicted_uneven`

每個 preset 定義：

1. 基礎參數集
2. 預設 duration
3. 可接受的強度伸縮範圍
4. 是否允許不對稱擴張

### 6.2 Micro Expression Library

建立短促 micro event 模板，例如：

1. `smirk_left`
2. `smirk_right`
3. `wink_left`
4. `wink_right`
5. `brow_raise_left`
6. `brow_raise_right`
7. `surprised_pop`
8. `embarrassed_squeeze`
9. `tiny_pout`
10. `quick_narrow_eyes`

每個事件定義：

1. patch 內容
2. duration
3. ease type
4. 是否自動回 base

### 6.3 Sequence Template Library

用於較有戲的短段落，建議僅在特定 arc 啟用：

1. `pop_then_settle`
2. `pause_then_smirk`
3. `widen_then_tease`
4. `shrink_then_recover`
5. `glare_then_flatten`

每個 sequence 限制為 2-4 steps，避免 runtime 複雜度失控。

### 6.4 Blink Strategy Library

blink 不再由模型直接輸出底層工具參數，而是由 intent 的語義欄位決定，例如：

1. `normal`
2. `focused_pause`
3. `shy_fast`
4. `teasing_pause`
5. `surprised_hold`
6. `sleepy_slow`

compiler 再把這些策略轉譯成：

1. `pause`
2. `resume`
3. `force_blink`
4. `set_interval`

## 7. Expression Compiler 設計

### 7.1 新增模組建議

backend 建議新增以下模組：

1. `backend/services/expression_intent_parser.py`
2. `backend/services/expression_compiler.py`
3. `backend/domain/expression_presets.py`
4. `backend/domain/expression_sequence_library.py`
5. `backend/domain/expression_blink_strategies.py`

### 7.2 編譯流程

Compiler 流程建議為：

1. 讀入 raw intent
2. schema 驗證與 normalize
3. 補齊缺失欄位
4. 選擇 base pose preset
5. 依 style dimensions 套用 modifiers
6. 依 `arc` 決定是否加入 sequence 或 micro events
7. 依 `blink_style` 生成 blink commands
8. 產出 runtime payload

### 7.3 Compiler 輸出格式

建議輸出為：

```json
{
  "type": "expression_plan",
  "basePose": {
    "preset": "playful_smirk",
    "params": {
      "mouthForm": 0.28,
      "eyeLOpen": 0.84,
      "eyeROpen": 0.72,
      "eyeSync": false,
      "browLY": 0.16,
      "browRY": 0.05,
      "browLAngle": 0.24,
      "browRAngle": -0.08,
      "eyeLSmile": 0.52,
      "eyeRSmile": 0.18
    },
    "durationSec": 1.8
  },
  "microEvents": [
    {
      "kind": "smirk_left",
      "durationMs": 520,
      "patch": {
        "mouthForm": 0.42,
        "eyeLSmile": 0.66
      },
      "returnToBase": true
    }
  ],
  "blinkPlan": {
    "style": "teasing_pause",
    "commands": [
      { "action": "pause", "durationSec": 1.2 },
      { "action": "force_blink" }
    ]
  },
  "speakingRate": 1.08,
  "debug": {
    "intentPrimaryEmotion": "playful",
    "intentArc": "pop_then_settle",
    "selectedBasePreset": "playful_smirk"
  }
}
```

### 7.4 重要原則

1. compiler 必須可在 intent 不完整時輸出可接受結果
2. compiler 不應依賴模型回傳完整欄位
3. compiler 必須保留 deterministic fallback
4. compiler 可在 debug 模式下附帶 mapping 資訊，方便調參與追查

### 7.5 建議預留的 compiler 介面

以下介面不一定要在第一版全部做滿，但建議在檔案設計與命名上先預留，避免未來重新拆檔：

1. `normalize_expression_intent(raw_intent, emotion_state, previous_state)`
   - 目的：欄位正規化、補預設值、範圍修正

2. `select_base_pose(intent, model_name)`
   - 目的：從 intent 選出 base pose preset

3. `build_micro_events(intent, base_pose, model_name)`
   - 目的：依 arc 與 style 維度生成短事件

4. `build_expression_sequence(intent, base_pose, model_name)`
   - 目的：在需要時生成 2-4 step sequence

5. `build_blink_plan(intent, model_name)`
   - 目的：把語義 blink style 轉成 runtime / tool commands

6. `compile_expression_plan(intent, model_name, previous_state)`
   - 目的：組合完整 expression plan

7. `render_legacy_behavior_payload(expression_plan)`
   - 目的：phase 1 將新計畫編回舊 `behavior` 格式

8. `render_runtime_payload(expression_plan)`
   - 目的：phase 2 起轉為新 websocket payload

若未來要新增新型事件，例如 gaze、head accent、mouth accent，優先擴充 `build_*` 與 `render_*` 層，而不是直接改 Agent prompt。

## 8. Prompt 改造方向

### 8.1 Expression Agent prompt 重點調整

舊 prompt 著重於：

1. 如何呼叫 `set_ai_behavior`
2. 如何調低階參數
3. 如何使用 `blink_control`

新 prompt 應改為著重於：

1. 為本句台詞判斷主情緒與副情緒
2. 判斷表情強度、活力與節奏
3. 判斷是否偏不對稱
4. 判斷是否需要短促 micro expression
5. 只輸出結構化 intent，不輸出任何低階工具呼叫

### 8.2 Prompt 設計原則

1. 不要求模型理解具體參數欄位
2. 不要求模型直接輸出 Live2D tool schema
3. 不要求模型處理 blink 的底層數值
4. 若模型不確定，應輸出合理的保守 intent，而不是空欄位或亂填工具

## 9. Payload 與相容策略

### 9.1 新增 payload 類型

websocket payload 建議新增：

1. `expression_plan`
2. `expression_debug`（選擇性）

### 9.2 舊格式相容策略

重構不應一次硬切，建議採過渡期雙軌：

第一階段：

1. backend 先導入 intent + compiler
2. compiler 先編譯回舊的 `behavior` + `blink_control`
3. frontend 暫時不需要大改

第二階段：

1. backend 開始額外發送 `expression_plan`
2. frontend 優先吃 `expression_plan`
3. 若缺失則 fallback 至舊 `behavior`

第三階段：

1. frontend 穩定支援新格式後
2. backend 逐步淡出舊 payload

### 9.3 建議預留的 payload 擴充欄位

為了避免未來每新增一種表演能力就重做 payload，建議 `expression_plan` 保留可向下相容的結構空間，例如：

1. `basePose`
2. `microEvents`
3. `sequence`
4. `blinkPlan`
5. `timingHints`
6. `modelHints`
7. `debug`

其中：

1. `timingHints` 可保留給未來 TTS 對齊、嘴型節奏、停頓演出
2. `modelHints` 可保留給不同模型的特殊能力或參數映射
3. `debug` 可保留 mapping trace、preset selection 原因、fallback 記錄

只要新欄位是 optional，frontend 與 backend 就能逐步擴充而不需要同步硬切。

## 10. Frontend Runtime 重構方向

### 10.1 第一階段

在不推翻既有 `LAppModel.ts` 主結構的前提下，新增一層 event playback：

1. `basePoseTarget`
2. `activeMicroEvents`
3. `blinkPlan`

這一階段可讓 runtime 從單一 target state，升級為 base + patch 的雙層模型。

### 10.2 第二階段

將 micro events 正式 queue 化：

1. enqueue event
2. event decay
3. event blend
4. short sequence playback

### 10.3 第三階段

新增 deterministic variation，例如：

1. playful 狀態的微小左右差
2. focused 狀態的 gaze hold
3. high energy 狀態的輕微回彈與 settle-back

這些細節不應全部交給模型逐項指定，而應由 runtime 自己在受控範圍內生成。

### 10.4 建議預留的 runtime interface

為了讓之後新增新表演能力時不需要重寫 `LAppModel.ts` 主循環，建議預留下列概念介面：

1. `applyBasePose(basePose)`
2. `enqueueMicroEvent(event)`
3. `enqueueSequence(sequence)`
4. `applyBlinkPlan(blinkPlan)`
5. `applyDeterministicVariation(context)`
6. `clearExpiredEvents(now)`

這些介面可以先用內部方法存在，不一定要立即抽成完整 class，但命名與責任邊界應先固定，方便未來 AI 或人類直接接著補強。

## 10.5 未來擴充範例

以下列出幾個常見未來需求，並標示建議插入層，作為後續快速判斷的參考。

### 範例一：新增「凝視 / 視線停留」能力

需求：角色在某些語氣下出現短暫凝視或停住不眨眼。

建議插入層：

1. intent schema 新增 `gaze_mode`
2. compiler 新增 `build_gaze_plan()` 或併入 `timingHints`
3. runtime 新增 gaze hold 處理

不建議做法：

1. 直接要求模型輸出一堆新的低階眼球參數

### 範例二：新增「頭部強調動作」能力

需求：吐槽、強調、驚訝時加入小幅 head accent。

建議插入層：

1. base pose 或 micro event library 新增 head accent 模板
2. compiler 根據 `energy` / `arc` 選擇是否附加
3. runtime 新增 head accent event handler

### 範例三：新增「模型專屬表演能力」

需求：某模型多了特有參數，例如耳朵、尾巴、特殊 blush。

建議插入層：

1. `modelHints` 或 model adapter
2. model-specific preset mapping
3. runtime 僅在該模型存在對應參數時啟用

不建議做法：

1. 把這些模型特有欄位直接加入通用 intent 必填欄位

### 範例四：新增「更高階的情緒規劃器」

需求：未來想在 Expression Agent 前再加一個 planner，先決定這句是 steady、burst、delay reveal 等表演策略。

建議插入層：

1. planner 輸出仍應收斂成 intent 可理解的欄位
2. compiler 主流程維持不變
3. 不應讓 planner 直接輸出低階 runtime 指令

### 範例五：新增「更多動作工具」

需求：未來新增眉毛震動、短暫嘴角抽動、特殊呼吸節奏等工具。

建議插入層：

1. 先評估是否能包成 micro event / sequence / runtime variation
2. 若可以，就只擴充 compiler 與 runtime handler
3. 只有在確定是新的底層能力類型時，才新增新的 payload 區塊

## 10.6 未來接手判斷指南

為了讓下次 AI 或人類更快進入狀況，新增需求時可先問四個問題：

1. 這是高階語義問題，還是低階參數問題？
2. 這個需求應該放在 intent、compiler、runtime，還是 model adapter？
3. 能不能用既有 preset / event / sequence 擴充，而不是新增 prompt 規則？
4. 這項能力是否必須暴露給模型，還是可以由程式 deterministic 產生？

若答案偏向可由 preset、compiler、runtime 解決，就不應優先去修改模型輸出格式。

## 11. 遷移階段設計

### Phase 1：先建立 intent + compiler，但先編回舊 payload

目標：

1. 驗證「模型輸出高階語義」是否能取代直接輸出 tools
2. 不立即大改 frontend
3. 降低切換風險

工作內容：

1. 建立 intent schema
2. 改寫 Expression Agent prompt
3. 新增 intent parser 與 compiler
4. 將 compiler 輸出映射回現有 `behavior` + `blink_control`
5. 補齊 backend 測試

### Phase 2：frontend 支援 `expression_plan`

目標：

1. 讓 runtime 可以吃 base pose + micro events
2. 讓 backend 不再受限於單一 final state

工作內容：

1. websocket service 支援新 payload
2. store 增加 event queue state
3. `LAppModel.ts` 加入 micro event overlay
4. 保留對舊 `behavior` 的 fallback

### Phase 3：擴充 sequence 與 runtime variation

目標：

1. 真正提升表情豐富度
2. 讓動作套件具有可持續擴充能力

工作內容：

1. 新增更多 preset / micro event / sequence templates
2. 建立 variation rules
3. 調整 compiler 的選擇策略
4. 增補整合測試與實機觀察調參流程

## 12. 測試策略

### 12.1 Backend 單元測試

新增測試群：

1. intent parser 測試
2. intent normalize 測試
3. preset selection 測試
4. blink strategy mapping 測試
5. sequence generation 測試
6. fallback compilation 測試

### 12.2 Backend 整合測試

1. `chat_ws.py` 能接收 intent 並成功編譯
2. Memory Agent 不受本次改動影響
3. fallback 路徑在 intent 缺欄位時仍能輸出有效 payload

### 12.3 Frontend 測試

1. `expression_plan` 可正確套用 base pose
2. micro events 可播放且會自然結束
3. blink plan 不會卡住在 pause 狀態
4. base pose 與 event 疊加不互相覆蓋失控

## 13. 風險與限制

### 13.1 Intent schema 過大風險

若欄位太多，模型仍然可能亂填。因此第一版必須節制欄位數量，避免重新變成另一種低階 schema。

### 13.2 Compiler 過度剛性風險

若 preset 與事件模板太少，可能導致表情雖穩但偏單一。因此 compiler 必須保留：

1. preset variation
2. intensity scaling
3. asymmetry modifiers
4. must_include / avoid hooks

### 13.3 Frontend 一次改太多風險

若前端一次導入完整 animation engine，風險過高。因此本次設計要求 frontend 先從 event overlay 做起，不直接全面改寫 runtime。

### 13.4 過渡期雙格式成本

在 phase 1 到 phase 2 期間，系統短期內會同時維護：

1. 舊 `behavior` payload
2. 新 `expression_plan`

需要在實作計畫中明確規範淘汰時機。

## 14. 最終決策

本次採用的方向為：

1. 保留單一 Expression Agent
2. Expression Agent 改輸出高階 `Expression Intent`
3. backend 新增 `Expression Compiler`
4. runtime 逐步從單一 target state 升級為 `base pose + micro events + blink strategy`

這個方向的核心價值不是減少模型參與，而是把模型從「低階動作工具填表器」提升為「高階表演導演」，並將穩定性、容錯、組合能力與擴充能力移回程式系統本身。

## 15. 後續建議

本設計稿確認後，下一步應進入 implementation planning，拆出：

1. Phase 1/2/3 的具體實作步驟
2. 檔案級修改清單
3. 測試新增清單
4. 過渡期相容策略與切換條件
5. extension points 的最小落地形式與命名規範
