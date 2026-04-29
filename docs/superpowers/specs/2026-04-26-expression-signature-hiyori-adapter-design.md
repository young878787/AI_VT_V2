# Expression Signature + Hiyori Adapter 設計稿

> 日期：2026-04-26
> 狀態：Approved
> 目的：針對目前 Live2D 表情差異過小的問題，建立可辨識的 visual signature 規則，並新增 Hiyori 專用 adapter，讓相同高階 intent 能穩定轉成更明顯的眉眼嘴變化。

## 1. 背景

目前專案已經有：

1. `emotion + performance_mode` 雙軸語意
2. preset + modifier 的 compiler
3. 前端 base pose + micro event + sequence 的播放機制

但實際視覺結果仍然不夠明顯，常見症狀如下：

1. 生氣時沒有穩定皺眉
2. 鬼臉與開心差異不足
3. 哭臉只像輕微調整，缺乏可辨識的悲傷 signature
4. 特殊動作如 wink、awkward、shock 雖然存在，體感仍偏短或偏淡
5. `blushLevel` 的使用語義不夠穩定，導致 angry/sad/happy/goofy 對臉頰狀態沒有清楚規則

經目前設計與實作比對後，問題主因不是「AI 不知道要演什麼」，而是「compiler 沒有把 intent 轉成足夠鮮明的 Live2D 參數 signature」。

因此本階段不優先新增更多底層 AI 輸出欄位，而是先補強：

1. visual signature 規則層
2. model-specific adapter
3. 必要的 micro event 幅度與時長調整

## 2. 核心決策

本設計採用以下原則：

1. AI 維持輸出高階語意，不直接輸出底層 Live2D 參數
2. compiler 新增 visual signature 層，明確規定每種表情應命中的眉毛、眼睛、嘴角、臉頰特徵
3. compiler 在 visual signature 之後，再套用 Hiyori 專用 adapter，放大 Hiyori 真正有感的參數區間
4. 前端先沿用目前 base pose / event / sequence 架構，只在必要時調整時序與 fade 行為

這代表系統主幹改為：

`intent -> topic guard -> performance_mode -> visual signature -> base pose -> Hiyori adapter -> micro events / sequence -> expression_plan`

## 3. 為何不先把 AI 輸出拆更細

本階段不讓 AI 直接輸出 `browLY`、`eyeLOpen`、`mouthForm` 等低階欄位，理由如下：

1. 目前問題不是語意不足，而是語意到參數的映射太保守
2. 若 AI 直接輸出底層參數，容易再次收斂到安全中間值
3. 同一組底層參數在不同模型上的視覺效果不同，應由 adapter 控制
4. 真正可維護的邊界應是「AI 決定表演意圖，compiler 決定模型表現」

第一版最多只考慮新增少量高階 tag，例如：

1. `keep_blush`
2. `drop_blush`
3. `one_brow_up`
4. `soft_squint`
5. `hard_frown`
6. `wink_left`
7. `wink_right`

這些 tag 的責任是補充演法，不是直接指定底層值。

## 4. Visual Signature 層

### 4.1 新責任

在 `backend/services/expression_compiler.py` 內新增 visual signature 決策層，位置固定為：

1. `resolve_effective_performance_mode()` 之後
2. `select_base_pose()` 之前
3. base preset 載入之後再由 signature 驅動 modifier 與 adapter

此層的責任是把高階 intent 轉成「本輪表演一定要有的臉部特徵」，並提供 preset 選擇與 modifier 放大的共同依據。

建議輸出結構：

```python
{
    "blush_policy": "keep|drop|neutralize|boost",
    "eye_shape": "open|soft_squint|hard_squint|wide",
    "brow_pattern": "calm|frown|one_up_one_down|inner_raised|asymmetric_tense",
    "mouth_pattern": "smile|smirk|flat|downturned|open_shock",
    "asymmetry_strength": 0.0,
    "event_bias": ["wink_left", "gloom_drop"],
}
```

實作上不一定要真的長成這個物件，但概念上要有一層明確規則。

### 4.2 表情 signature 規則

#### `happy + smile`

期望特徵：

1. `blushLevel` 保留或小幅提升
2. `eyeLSmile` / `eyeRSmile` 明顯高於 neutral
3. `mouthForm` 明顯正值
4. `browLY` 輕度上抬
5. 不應出現高不對稱

#### `playful + goofy_face`

期望特徵：

1. 保留臉紅
2. 左右眼明顯不對稱
3. 左右眉毛一高一低
4. `browAngle` / `browForm` 至少一側更誇張
5. `mouthForm` 比普通 smile 更大
6. 必要時偏向 `wink_left`、`goofy_eye_cross_bias`、`awkward_freeze`

#### `teasing + cheeky_wink` / `smug`

期望特徵：

1. 有笑意但不等於 happy
2. 單側眼或眉更突出
3. 臉紅可以保留少量
4. 嘴角上揚，但不能像 `goofy_face` 那樣大幅扭動

#### `sad + tense_hold`

期望特徵：

1. `blushLevel` 應清到接近 0 或負向
2. `eyeLOpen` / `eyeROpen` 比 happy 更低，呈現稍閉眼
3. `mouthForm` 明顯負值
4. `browAngle` 往悲傷方向加深
5. `browLX/browRX` 往內收
6. 不應保留 playful 類笑眼

#### `gloomy + deadpan/gloomy`

期望特徵：

1. 無臉紅
2. 低動能、低嘴角
3. 眼睛低開合但不必像哭
4. 眉毛下壓且內收
5. 事件偏 `gloom_drop`

#### `angry + angry/meltdown`

期望特徵：

1. 臉紅不應被當成預設特徵，應偏 0 或依 warmth 清除
2. 眼睛收窄
3. `browForm` 顯著負值
4. `browAngle` 顯著朝皺眉方向偏移
5. `browLX/browRX` 明顯內收
6. `meltdown` 比一般 angry 更不對稱、更極端

#### `surprised + shock_recoil`

期望特徵：

1. 眼睛張大
2. 眉毛上抬
3. 嘴角張開或偏驚訝形
4. 事件偏 `shock_pop`

#### `awkward` / `wink` / 特殊演出

期望特徵：

1. 不只靠 base pose
2. 一定有對應 micro event 或短 sequence
3. duration 不可短到使用者感知不到

## 5. Hiyori Adapter

### 5.1 目的

Hiyori 的參數雖然齊全，但目前 compiler 輸出的值對實際視覺效果仍偏保守。  
因此需要一層 `Hiyori adapter`，把 canonical signature 映射到 Hiyori 真正有感的數值區間。

### 5.2 新增位置

建議新增：

1. `apply_visual_signature(...)`
2. `apply_model_adapter(..., model_name="Hiyori")`

或以等價方式實作，只要責任邊界清楚即可。

### 5.3 Hiyori 第一版放大重點

#### 眉毛

優先放大：

1. `browLY/browRY`
2. `browLAngle/browRAngle`
3. `browLForm/browRForm`
4. `browLX/browRX`

原因：使用者目前最直接感受到的缺點就是 angry 不夠皺眉、goofy 不夠一高一低。

#### 眼睛

優先放大：

1. `eyeLOpen/eyeROpen` 的收窄差距
2. `eyeLSmile/eyeRSmile`
3. goofy / teasing / sad 之間的左右差異

#### 嘴巴

優先放大：

1. `mouthForm` 正負方向的區別
2. happy 與 goofy 的差距
3. sad 與 gloomy 的負向層次

#### 臉頰

明確建立規則：

1. happy：保留臉紅
2. goofy：保留或略增臉紅
3. teasing：少量臉紅可接受
4. sad：清除臉紅
5. gloomy：清除臉紅
6. angry：預設不保留臉紅，除非未來另有特殊 tag

### 5.4 具體實作形狀

可用以下其中一種方式：

1. 每個 `performance_mode` 對應一組 amplification rule
2. 每個 `emotion + performance_mode` 對應一組 rule
3. 每個 facial feature 各有一張放大量表，再依 signature 組合

本設計推薦第 3 種，因為可維護性較高，且較容易跨模型擴充。

## 6. Micro Event 與 Sequence 調整

本階段不只調 base pose，也同步調整事件的可感知度。

### 6.1 問題

目前部分事件雖然存在，但有以下問題：

1. duration 太短
2. patch 與 base pose 差距太小
3. fade 回 base 的速度太快

### 6.2 調整方向

1. `wink_left` / `wink_right`
   - 保持短，但臉紅與單側眼差異要更明顯
2. `goofy_eye_cross_bias`
   - 與 base smile 的差距要拉開
3. `gloom_drop`
   - 要比 sad 更像瞬間掉下去
4. `tense_squeeze`
   - 眉毛內收與眼睛壓縮更強
5. `meltdown_warp`
   - 與一般 angry 顯著分開
6. `awkward_freeze`
   - duration 可略拉長，避免感知不足

## 7. 前端策略

### 7.1 本階段不大改前端架構

`vtuber-web-app/src/live2d/LAppModel.ts` 目前有：

1. base pose 套用
2. micro event overlay
3. sequence enqueue
4. `eyeSync` 鏡射
5. `lerpFactor` 平滑

第一版不重寫這條鏈路。

### 7.2 只做必要校正

若後端 signature + adapter 做完後仍覺得不夠明顯，再進行以下微調：

1. 拉高 expression 參數的 lerp 速度
2. 調整 event fade 曲線
3. 檢查 `eyeSync` 是否吃掉不對稱事件

原則是先證明問題主要在 compiler 輸出，而不是先改前端播放機制。

## 8. 測試要求

至少新增或調整以下測試：

1. angry 比 happy 更皺眉，且 blush 較低
2. goofy_face 比 happy_smile 具有更高左右不對稱
3. sad 比 happy 更閉眼、更少 blush、更低 mouthForm
4. happy 會保留 blush
5. angry 預設不保留 blush
6. goofy_face 會保留 blush，且眉毛一高一低
7. awkward / wink 類模式一定產生對應 micro event
8. meltdown 與普通 angry 的差距足以從 base pose 或事件看出

## 9. 非目標

本階段不做以下事情：

1. 不讓 AI 直接輸出底層 Live2D 參數
2. 不重寫前端整個動畫引擎
3. 不引入自由文字風格提示詞作為主要控制來源
4. 不同時為所有模型建立 adapter，先以 Hiyori 為主

## 10. 實作順序

建議依序進行：

1. 在 compiler 中抽出 visual signature 規則層
2. 實作 Hiyori adapter
3. 調整 presets / modifiers / event patches
4. 補 compiler 測試
5. 如有必要，再微調前端 lerp / fade

## 11. 最終決策

本階段優先處理的是：

1. 讓每種高階表情擁有穩定且可辨識的 visual signature
2. 讓 Hiyori 真正吃到更有感的參數幅度
3. 先改善 compiler 與 adapter，暫不要求 AI 輸出更細底層欄位

這樣可以先解決目前最核心的問題：

1. angry 沒有皺眉
2. goofy 臉不夠鬼
3. happy / sad / gloomy / awkward 的臉頰規則不明
4. 特殊動作存在但辨識度不足
