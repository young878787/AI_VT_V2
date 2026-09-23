# 單次 JEV 兩軸表演決策設計

## 目標

每個對話輪次只呼叫一次 JEV。單次回應同時包含既有六欄 Emotion State、基礎情緒 Choice、互動態度 Choice，以及強度、能量、轉場與特殊表演觸發器。

最終表演保留兩個彼此獨立的語意軸：

- `base_emotion`：當輪主要情緒底色。
- `interaction_attitude`：角色以何種可見方式面對使用者。

後端不維護情緒與態度的交叉配對表。兩個 JEV Choice 驗證後直接傳給 expression compiler 的 `emotion` 與 `performance_mode`。

## 契約

### 基礎情緒

封閉集合為 `neutral`、`happy`、`angry`、`sad`、`gloomy`、`shy`、`surprised`、`conflicted`。`playful` 與 `teasing` 屬於互動表現，不再作為基礎情緒候選。

### 互動態度

第一階段沿用 compiler 已支援的 performance mode 集合，避免同時改動 Live2D：`smile`、`bright_talk`、`goofy_face`、`cheeky_wink`、`smug`、`deadpan`、`gloomy`、`volatile`、`meltdown`、`awkward`、`tense_hold`、`shock_recoil`。

JEV 問題將這些值描述成可見互動方式。未來增加 `confident`、`guarded` 或 `menacing` 時，新增單一態度及其 compiler 行為，不建立每種情緒的複合標籤。

### 聯合判斷

兩個 Choice 位於同一份 JEV questions。共同指令要求先判斷基礎情緒，再選擇能一致呈現它的互動態度；角色人格只能影響互動態度，不能獨立提高情緒。一般情況使用 `neutral + smile`，強烈模式需要當輪明確線索。

提示包含一致與衝突範例，例如 `happy + bright_talk`、`shy + awkward`、`angry + tense_hold`，並避免沒有文本支持的 `happy + meltdown`、`sad + bright_talk`。

## 資料流

1. 後端建立一次 JEV context，包含使用者輸入、近期對話、上一輪 Emotion State、上一輪 expression carry state、角色表達設定與互動人格。
2. 後端以一次 `call_jev()` 送出六欄 Noul、兩個 Choice 與其餘 Action 問題。
3. 六欄 Noul 原子驗證後更新 Emotion State；失敗時沿用上一輪或 neutral。
4. `base_emotion` 與 `interaction_attitude` 分別驗證白名單及 confidence。
5. 有效 Choice 直接寫入 `intent.emotion` 與 `intent.performance_mode`，不經組合 Resolver。
6. Chat 使用已驗證的 Emotion State；compiler 使用同一份 JEV 回應產生 expression plan。

## 回退

- 整次 JEV 失敗：Emotion State 使用既有回退；表演沿用上一輪，首輪使用 `neutral + smile`。
- `base_emotion` 無效或低信心：沿用上一輪表情 emotion，首輪使用 neutral。
- `interaction_attitude` 無效或低信心：使用 smile，不因上一輪強烈態度形成殘留。
- 任一軸失敗不丟棄另一個有效 Choice。
- compiler 失敗才整體回退到 `neutral + smile`。

## 可觀測性

expression plan debug 保存兩軸的原始 choice、confidence、probabilities、各自 fallback 原因、最終 emotion/mode、問題版本與問題指紋。

Markdown 測試報告只顯示基礎情緒、互動態度、兩者信心、最終結果及回退；完整機率留在 `turns.jsonl` 和 server log。

## 驗證

- 單元測試確認完整 questions 只送出一次。
- 六欄 Emotion State 與兩個 Choice 可各自失敗及回退。
- 映射器不含情緒與態度配對表，直接傳遞兩軸。
- WebSocket 測試確認 Chat 與 expression 使用同一份 JEV 回應。
- 以固定 20 輪情境檢查選擇分布、低信心回退及明顯衝突。

## 範圍

本次不新增 Live2D 表情、不重寫 compiler、不修改 Cubism SDK。互動態度先使用既有 performance modes；新增態度視覺語彙另行處理。
