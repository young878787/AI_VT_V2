# Agent 分層與職責重整設計稿

> 日期：2026-04-24
> 狀態：Proposed
> 目標：整理現有 Agent 架構的職責邊界，降低 Agent 間互相汙染，並以方案 A 為主軸推動後續重構與實作規劃。

## 1. 背景與目的

目前專案名義上採用雙 Agent 架構，但實際執行路徑已經演化為：

1. Agent A：角色對話與 JPAF 狀態產生
2. Agent B-1：Live2D / 表情工具決策
3. Agent B-2：Memory / 記憶工具決策
4. `chat_ws.py`：總控協調、工具解析、工具執行、狀態更新、事件推送

這代表目前真正的問題不只是「Agent 有沒有拆開」，而是：

1. 命名已經無法準確反映責任邊界
2. 決策層雖然拆分，但執行層仍混在同一條流程中
3. `chat_ws.py` 承擔過多責任，成為跨層耦合中心
4. Expression Agent 目前直接讀取 `jpaf_state`，造成不必要的跨層汙染

本設計稿的目標，是先做「分層優先」的方案 A 重整，而不是繼續細分更多 Agent。

## 2. 現況診斷摘要

### 2.1 目前實際分工

#### Agent A

目前負責：

1. 產生 VTuber 對白
2. 產生隱藏的 `jpaf_state`
3. 產生 `emotion_state`
4. 不直接呼叫工具

此層邊界相對清楚，是目前最乾淨的一層。

#### Agent B-1

目前負責：

1. 根據使用者訊息、角色回覆、上一輪表情摘要、`jpaf_state`、`emotion_state` 決定表情工具呼叫
2. 主要輸出 `set_ai_behavior`
3. 視情況額外輸出 `blink_control`

此層的問題在於輸入來源過多，尤其直接讀取 `jpaf_state`，使 Expression 決策綁定到 Dialogue Agent 的內部心理模型。

#### Agent B-2

目前負責：

1. 根據使用者訊息判斷是否需要更新 user profile
2. 根據上下文決定是否需要寫入 memory note

此層責任大致清楚，但在執行階段仍被混入同一條工具處理流程。

#### `chat_ws.py`

目前同時負責：

1. session 切換與歷史訊息管理
2. Agent A prompt 注入與呼叫
3. JPAF session 更新
4. 建立 Expression / Memory Agent prompt
5. 並行呼叫兩個子 Agent
6. 解析 tool calls
7. 執行 Live2D 與 Memory 工具
8. 建立 websocket payload
9. 發送 websocket 與 display 廣播
10. 啟動 TTS
11. prompt log 與 context compression

這表示目前 route handler 已不只是 API 入口，而是實際上的 orchestrator、executor、presenter 與 state manager。

### 2.2 主要問題彙總

#### 問題一：命名已不符合實際職責

`Agent B` 這個名稱已經失真。現在系統裡實際存在的是兩個不同責任的子 Agent，而不是單一 B Agent。

影響：

1. 文件與討論時容易模糊
2. 後續再分層時命名會越來越混亂
3. code review 時不容易快速對照責任範圍

#### 問題二：決策層拆了，但執行層沒拆

目前 Expression Agent 與 Memory Agent 會先各自輸出 tool calls，但最後被合併到單一 `all_calls` 中執行。

影響：

1. 失去來源資訊
2. 每個 Agent 不能有獨立驗證與 fallback
3. 工具處理流程耦合過深

#### 問題三：`chat_ws.py` 過胖

目前 `chat_ws.py` 同時持有：

1. 對話狀態
2. 表情狀態
3. 記憶操作
4. websocket 傳輸責任
5. TTS lifecycle

影響：

1. 維護困難
2. 很難針對單一責任做測試
3. 任一修改都容易連動其他功能

#### 問題四：Expression Agent 直接讀取 `jpaf_state`

`jpaf_state` 是 Dialogue Agent 的內部心理與人格推理結果，不應直接成為 Expression Agent 的主要輸入之一。

影響：

1. Expression Agent 對上游實作細節耦合過深
2. 對話人格模型變動時，表情決策也會被迫一起調整
3. 造成 Prompt 汙染與責任邊界模糊

#### 問題五：狀態沒有正式分層

目前至少存在三種不同性質的狀態：

1. Conversation state
2. Expression state
3. Memory actions / persistence state

但目前這些狀態主要集中在 `chat_ws.py` 內部協調，缺少清楚的資料邊界。

## 3. 命名重整方案

為了讓後續分層與檢視更直觀，命名調整如下：

1. `Agent A` -> `Dialogue Agent`
2. `Agent B-1` -> `Expression Agent`
3. `Agent B-2` -> `Memory Agent`
4. `chat_ws.py` 內的主協調流程 -> `Chat Orchestrator`

### 3.1 命名調整原則

1. 名稱必須直接反映責任
2. 避免使用無法說明功能的抽象代號
3. 未來新增 Agent 或模組時，能自然延續同一套命名規則

### 3.2 命名重整的好處

1. 文件與程式碼可直接對齊
2. review 時能更快辨識該模組的責任範圍
3. 後續若新增 Moderation、Animation、Planner 等模組，能用同一套命名方式延伸

## 4. 方案比較與決策

### 4.1 方案 A：分層優先，維持現有 Agent 數量

做法：

1. 保留三個 Agent，不先增加更多 Agent
2. 先拆分 orchestration、parsing、execution、presentation
3. 將工具處理流程從混池改為分池
4. 同步完成命名重整
5. 移除 Expression Agent 對 `jpaf_state` 的直接依賴

優點：

1. 改動風險較低
2. 直接命中目前最大的耦合來源
3. 可為後續擴充打好乾淨的邊界

缺點：

1. 仍需重構部分既有流程
2. 短期內會有命名與實作並存的過渡期

### 4.2 方案 B：先只做語意重命名

做法：

1. 先把命名全部改清楚
2. 暫時不動 orchestrator 結構

缺點：

1. 只能改善溝通，不會真正消除混層
2. 執行邊界仍然混在一起

### 4.3 方案 C：繼續細分更多 Agent

做法：

1. 再拆出 Blink、Emotion Mapping、Moderation 等更多 Agent

缺點：

1. 在目前架構下會讓 orchestrator 更複雜
2. 會把分層問題轉化成更多 Agent 管理問題

### 4.4 決策

本次採用 **方案 A**。

原因：

1. 目前主要瓶頸是混層與互相汙染，不是 Agent 數量不足
2. 先把分層整理乾淨，後續若需要再細分 Agent 才不會失控

## 5. 目標架構

### 5.1 目標分層

重整後，系統應清楚分成以下幾層：

1. Prompt Construction Layer
2. Agent Invocation Layer
3. Orchestration Layer
4. Parsing / Validation Layer
5. Execution Layer
6. Presentation Layer

### 5.2 目標模組責任

#### Dialogue Agent

責任：

1. 產生角色對白
2. 產生 `jpaf_state`
3. 產生 `emotion_state`

不負責：

1. 工具呼叫
2. 表情參數決策
3. 記憶工具執行

#### Expression Agent

責任：

1. 根據使用者訊息、角色回覆、上一輪表情摘要、`emotion_state` 產生表情工具決策
2. 決定 `set_ai_behavior`
3. 視情況決定 `blink_control`

不負責：

1. 讀取或理解 `jpaf_state` 內部結構
2. 記憶相關工具
3. websocket payload 組裝

#### Memory Agent

責任：

1. 判斷是否需要更新 user profile
2. 判斷是否需要寫入 memory note

不負責：

1. 表情工具
2. 對話輸出
3. 前端事件推送

#### Chat Orchestrator

責任：

1. 串接對話回合流程
2. 管理每輪資料流
3. 決定哪些資料要傳給哪個 Agent
4. 協調各 Agent 的執行順序與結果彙整

不負責：

1. 直接解析所有工具細節
2. 直接執行所有工具分支
3. 承擔全部 presentation 細節

## 6. `jpaf_state` 去汙染原則

### 6.1 目前問題

Expression Agent 直接讀取 `jpaf_state`，等於把 Dialogue Agent 的內部心理推理資訊直接暴露給下游表情層。

### 6.2 重整原則

本次方案要求：

1. `build_live2d_prompt()` 移除 `jpaf_state` 參數
2. Expression Agent 不再直接依賴 `active_function` 或 `suggested_persona`
3. Expression 決策主要依賴：
   - 使用者直接要求
   - `agent_a_reply`
   - `emotion_state`
   - `previous_expression_state`

### 6.3 為什麼保留 `emotion_state`

`emotion_state` 屬於較薄的表現層摘要，適合作為 Expression Agent 的輸入；它描述的是這句台詞的情緒輸出，而不是 JPAF 的內部人格推理結構。

### 6.4 若未來仍需要人格風格影響

不得直接把完整 `jpaf_state` 傳給 Expression Agent。若真的需要，必須由 orchestrator 做明確降階轉譯，例如僅傳遞有限的語氣風格標記，而不是內部結構細節。

## 7. 方案 A 的具體修改計畫

### 7.1 第一階段：命名與邊界先對齊

1. 在文件、註解、設計溝通中改用 `Dialogue Agent`、`Expression Agent`、`Memory Agent`
2. 在不大改外部行為的前提下，先讓函式與流程命名逐步反映新責任
3. 明確註記舊名稱與新名稱對照，避免過渡期混淆

### 7.2 第二階段：移除 Expression Agent 對 `jpaf_state` 的直接依賴

1. 修改 `build_live2d_prompt()` 介面
2. 修改 `chat_ws.py` 或後續 orchestrator 呼叫處，不再傳入 `jpaf_state`
3. 調整 prompt wording，改以 `emotion_state` 與台詞內容作為主要依據
4. 驗證表情輸出是否仍穩定

### 7.3 第三階段：拆出分池解析與執行

1. Expression tool calls 獨立解析
2. Memory tool calls 獨立解析
3. 不再合併成單一 `all_calls`
4. 每一池有自己的驗證與 fallback

### 7.4 第四階段：縮小 `chat_ws.py` 責任

1. 抽出 orchestrator service
2. 抽出 tool parser service
3. 抽出 tool executor service
4. 抽出 presenter / payload builder

### 7.5 第五階段：建立檢查點

每次重構都必須確認：

1. 對話仍正常輸出
2. 表情工具仍會被正確呼叫
3. 記憶工具不受影響
4. websocket payload 順序不被破壞
5. TTS 與廣播流程仍可運作

## 8. 驗收標準

完成方案 A 的最低驗收條件如下：

1. 文件與核心程式碼中已採用新命名
2. Expression Agent 已不再直接讀取 `jpaf_state`
3. Expression 與 Memory 工具解析不再混池
4. `chat_ws.py` 的責任比目前明顯縮小
5. 對話、表情、記憶、TTS 基本流程仍可正常運作

## 9. 實作順序建議

建議實作順序如下：

1. 先改 Expression Agent 介面，移除 `jpaf_state`
2. 再調整 prompt 內容與呼叫點
3. 接著把 Expression / Memory tool call 解析改為分池
4. 最後再逐步拆出 orchestrator / executor / presenter

原因：

1. 先處理最明顯的跨層汙染點
2. 再處理工具執行邊界
3. 最後才做較大範圍的流程抽離，能降低重構風險

## 10. 本次結論

本次不採用「繼續拆更多 Agent」，而是採用 **方案 A：分層優先**。

核心結論：

1. 以責任導向命名取代 A / B-1 / B-2 的模糊代號
2. 讓 Expression Agent 從 `jpaf_state` 中解耦
3. 讓工具解析與執行從混池改為分池
4. 逐步將 `chat_ws.py` 從過胖總控重整為較乾淨的 Chat Orchestrator

這樣做的目的，不是一次把架構拆到最細，而是先把邊界建立清楚，讓後續重構、維護與檢視都更可控。
