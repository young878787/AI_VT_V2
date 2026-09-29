# Rushia_first 表情相容性記錄（方案A最小驗證）

- 日期：2026-09-29
- 狀態：方案A最小驗證（能上架顯示、不保證細膩表情；缺失通道以 SDK 靜默 no-op 帶過）
- 範圍：只記錄現況與後續方向，不做程式碼修改

## 1. 資料來源（行號以本日 master 為準）

| # | 檔案 | 用途 |
|---|------|------|
| 1 | `vtuber_model/Rushia_first/RushiaHD.cdi3.json` | Rushia 參數全表（Parameters / Groups / Parts） |
| 2 | `vtuber-web-app/src/live2d/LAppModel.ts` 約 1598–1630（`applyCurrentExpressionParameters`）、約 255–287（參數 ID 初始化） | 前端表情寫入點 |
| 3 | `vtuber-web-app/src/types/expressionPlan.ts`（`ExpressionBasePose['params']`、`ExpressionMicroEventPatch`、`EXPRESSION_MICRO_EVENT_PATCH_KEYS`） | 表情合約（type guard） |
| 4 | `backend/services/expression_compiler.py` 約 250–313（`apply_model_adapter`） | 後端模型適配器 |
| 5 | `backend/domain/tools/Hiyori.json`（`openai_tools.live2d`、`ui_config.live2d`、`prompt_config.live2d`） | Tool 定義 + prompt 口吻（Hiyori-specific） |
| 6 | `vtuber_model/Rushia_first/RushiaHD.model3.json`（Motions / Groups；公開鏡像另見 `vtuber-web-app/public/Resources/Rushia_first/RushiaHD.model3.json`） | 動作群組與貼圖 |
| 7 | `backend/domain/expression_sequence_library.py`（`MICRO_EVENT_LIBRARY`、`SEQUENCE_LIBRARY`）、`backend/domain/expression_presets.py`（`BASE_POSE_PRESETS`） | preset / micro-event 實際用到的 patch key |

## 2. Rushia 擁有參數表（來自 `RushiaHD.cdi3.json` Parameters）

共 31 個參數。表情管線真正用得到的只有其中一部分，其餘是頭髮／緞帶／袖口物理。

| 分群 | 參數 Id | 中文名（cdi3 原文） | 管線用途 |
|------|---------|---------------------|----------|
| 臉 | `ParamAngleX/Y/Z` | 角度 X/Y/Z | 頭部 yaw/pitch/roll（`headIntensity` 驅動） |
| 眼 | `ParamEyeLOpen` / `ParamEyeROpen` | 左眼开合／右眼开合 | `eyeLOpen/eyeROpen`、眨眼、`wink_left/right`、`surprised_eye_pop` 等 |
| 眼 | `ParamEyeBallX/Y` | 眼球 X/Y | `eyeBallX/eyeBallY`（`goofy_eye_cross_bias`、`tense_squeeze` 等仍有效） |
| 眼 | `ParamEyeBallForm` | 果冻眼 | Rushia 獨有，管線目前無人寫入（見 §4） |
| 眉 | `ParamBrowLY` / `ParamBrowRY` | 左眉 Y／右眉 Y | `browLY/browRY`（上下壓／上揚，Rushia 唯一可用的眉通道） |
| 嘴 | `ParamMouthForm` | 嘴型 | `mouthForm`（笑／哭嘴角） |
| 嘴 | `ParamMouthOpenY` | 嘴巴开合 | LipSync（Groups `LipSync` 唯一成員） |
| 身體 | `ParamBodyAngleX/Y/Z` | 身体角度 X/Y/Z | `bodyAngleX/Y/Z`（身體 sway/bob/twist 全有效） |
| 身體 | `ParamBreath` | 呼吸 | `breathLevel`（`happy_smile_pulse` 等的 `breathLevel` patch 有效） |
| 物理／裝飾 | `ParamHairBack`、`ParamHairFront`、`ParamHairLongR/L`、`ParamHairSideR/L`、`ParamHairLongRTip/LTip`、`ParamHairSideRTip/LTip`、`ParamRibbonR`、`ParamHairRoll`、`ParamHairPitch`、`ParamSeamBake`、`ParamHairBack` | 后发摆动／前发摆动／左右長髮／側髮／髮尾／右緞帶擺動／髮絲歪頭慣性／俯仰慣性／袖口接縫補償 | 物理擺動，由 moc/physics 驅動；`physicsImpulse` 仍可透過既有物理鏈間接帶動 |

Groups：`EyeBlink → [ParamEyeLOpen, ParamEyeROpen]`、`LipSync → [ParamMouthOpenY]`。

## 3. Rushia 缺失參數表（7+1 項）

合約（`expressionPlan.ts`）要求但 Rushia cdi3 完全沒有的參數。前後端都不會報錯，全部以靜默 no-op 消失（見 §5.1）。

| # | 缺失參數 | 對應合約 key | 說明 |
|---|----------|--------------|------|
| 1 | `ParamBrowLAngle` | `browLAngle` | 左眉角度（倒八字＋／八字眉－） |
| 2 | `ParamBrowRAngle` | `browRAngle` | 右眉角度（`eyeSync=true` 時由左眉鏡像） |
| 3 | `ParamBrowLForm` | `browLForm` | 左眉彎曲（皺眉－／上凸＋） |
| 4 | `ParamBrowRForm` | `browRForm` | 右眉彎曲 |
| 5 | `ParamBrowLX` | `browLX` | 左眉水平（外展－／內攏＋） |
| 6 | `ParamBrowRX` | `browRX` | 右眉水平（`eyeSync=true` 時鏡像） |
| 7 | `ParamEyeLSmile` / `ParamEyeRSmile` | `eyeLSmile` / `eyeRSmile` | 左右笑眼（註：任務原文 `RValidateSmile` 應為 `EyeRSmile` 之筆誤） |
| +1 | `ParamCheek` / `ParamTere` | `blushLevel` | 臉紅／蒼白。前端同時寫兩個 Id（`LAppModel.ts:1617–1618`）；Rushia 兩個都沒有 |
| 獨有（反向） | `ParamEyeBallForm`（果冻眼） | — | Rushia 獨有，合約無此 key，目前無任何 preset / event / adapter 寫入，等於閒置 |

## 4. 每個缺失參數的情緒／micro-event 影響（kind 名與程式碼對得上）

> 閱讀方式：左欄是「後端送了什麼」，右欄是「Rushia 畫面少了什麼」。`eyeLOpen/eyeROpen`、`browLY/browRY`、`mouthForm`、`bodyAngle*` 的部分仍有效，不在下表。

### 4.1 `eyeLSmile` / `eyeRSmile`（笑眼）—— 影響最大

開心、害羞、調皮、鬼臉的核心「瞇眼笑」全部或部分失效。Rushia 的開心只剩 `eyeLOpen` 縮小＋`mouthForm` 上揚，沒有眼皮柔和下壓的笑意。

用到笑眼的 event（`expression_sequence_library.py`）：

- `happy_smile_pulse`（`eyeLSmile 0.54 / eyeRSmile 0.48`）—— 開心主脈衝，笑眼全丟，只剩嘴角＋臉紅＋呼吸
- `happy_brow_lift`（`eyeLSmile 0.42`）、`happy_eye_smile_right`（`eyeRSmile 0.58`）、`happy_body_bounce_pop`（雙笑眼 0.56）、`happy_body_sway_bounce_left/right`（單側笑眼 0.62）
- `smirk_left`（`eyeLSmile 0.66`）、`smirk_right`（`eyeRSmile 0.66`）、`smirk_then_flat` 序列 —— 壞笑／不對稱笑只剩嘴角歪
- `brow_micro_curve_smile`（雙笑眼 0.70）、`brow_micro_bounce_down`（0.44）、`brow_micro_shape_wave`（0.50/0.34）、`brow_micro_soft_relax`（0.62）、`brow_micro_understand_lift`（0.24）、`bright_sway_left/right`（`SEQUENCE_LIBRARY`，單側笑眼 0.68）
- preset（`expression_presets.py`）：`happy_smile_soft`（0.45）、`happy_bright_talk`（0.52）、`playful_smirk`（0.42/0.14）、`playful_goofy_face`（0.62/0.10）、`teasing_cheeky_wink`（0.78/0.18）、`teasing_smug`、`conflicted_volatile`、`shy_tucked`（0.18/0.10）、`awkward_stuck`、`calm_soft`
- idle（`expression_idle_library.py`）：`happy_idle_warm_lift`（0.56/0.52）、`shy_idle_side_peek`（0.08/0.20）、`neutral_idle_calm_breath`（0.18/0.16）、`conflicted_idle_uneasy_shift` 等的笑眼分量

### 4.2 `browLAngle` / `browRAngle`（眉角度）—— 影響次大

生氣倒八字、悲傷八字眉、驚訝眉弧、鬼臉不對稱眉全部失效。Rushia 的眉毛只剩 `BrowLY/RY` 上下移動，**做不出角度**。

- 生氣：`angry_brow_press`（`browLAngle 0.48 / browRAngle -0.44`）、`angry_eye_narrow`（0.42/-0.38）、`angry_stare_flash`（0.50/-0.50）、`meltdown_warp`（0.35/0.10）、preset `angry_meltdown`（0.42/0.08）、idle `angry_idle_glare_lock`（0.78）
- 悲傷：`sad_brow_waver`（-0.34/0.32）、`brow_micro_inner_worry`（-0.30/0.30）、preset `sad_tense_hold`（-0.14/0.14）、`gloomy_deadpan`、idle `crying_idle_tremble_breath`（-0.34）、`gloomy_idle_slow_sink`
- 開心／調皮／鬼臉不對稱：`happy_brow_lift`、`playful_brow_spark`（0.20/-0.12）、`shy_peek_left`（-0.12/0.08）、`conflicted_brow_tilt`（0.26/-0.20）、`uneven_brow_pop`、`goofy_eye_cross_bias`（0.18）、`volatile_twitch`、`tense_squeeze`（-0.12/0.12）、`brow_micro_dual_lift`、`brow_micro_surprise_arc` 等整個 `brow_micro_*` 家族
- 後端 `apply_model_adapter` 的 `brow_scale` 放大（`browLAngle *= brow_scale`）對 Rushia 直接跳過（early return，見 §5.2）

### 4.3 `browLForm` / `browRForm`（眉彎曲）—— 生氣皺眉消失

- `angry_brow_press`（`browLForm -0.34 / browRForm -0.28`）的皺眉分量全丟；`meltdown_warp`（-0.20）、preset `angry_meltdown`（-0.22/-0.06）、`sad_tense_hold`（-0.10）、idle `angry_idle_glare_lock`（-0.42）
- `brow_micro_curve_smile`（+0.42  Marta 上凸笑眉）、`brow_micro_soft_relax`（+0.50）、`brow_micro_surprise_arc`（+0.24）、`brow_micro_inner_worry`（-0.22）、`brow_micro_shape_wave`、`brow_micro_soft_question` 等的上凸／下彎眉形全丟
- adapter 的 `angry_meltdown` / `sad_tense` 分支（`browLForm -= 0.08 + intensity*0.08`）對 Rushia 不執行

### 4.4 `browLX` / `browRX`（眉水平）—— 皺眉內攏消失

- `angry_brow_press`（`browLX 0.22 / browRX -0.20`）、`sad_brow_waver`（0.14/-0.12）、`tense_squeeze`（0.10/-0.10）、`playful_brow_spark`（-0.08/0.06）、`shy_peek_left`（0.08/-0.06）、`brow_micro_inner_worry`（0.12/-0.12）、`brow_micro_shape_wave`、`brow_micro_soft_question` 的眉毛內外移動全丟
- preset `playful_goofy_face`（-0.12/0.08）、`angry_meltdown`、`sad_tense_hold`、`teasing_cheeky_wink` 的水平分量同樣失效

### 4.5 `blushLevel` → `ParamCheek` / `ParamTere`（臉紅／蒼白）—— 害羞與低氣壓失效

- 害羞：`shy_blush_pulse`（`blushLevel 0.62`）、`shy_peek_left` 搭配、preset `shy_tucked`（0.18）、idle `shy_idle_side_peek`（0.42）、`awkward_freeze`（0.10）、`wink_left` / `wink_right`（0.12 含羞）、`happy_smile_pulse`（0.08）
- 生氣／悲傷的「蒼白」：`angry_brow_press`（-0.45）、`angry_eye_narrow`（-0.50）、`angry_stare_flash`（-0.42）、`sad_brow_waver`（-0.42）、`sad_eye_sink`（-0.55）、`gloomy_flat_hold`（-0.48）、`gloom_drop`（-0.15）、`tense_squeeze`（-0.08）、idle `angry_idle_glare_lock`（-0.70）、`crying_idle_tremble_breath`（-0.65）
- prompt 寫的「`blush_level -0.5~-1.0（Hiyori 專有）`」在 Rushia 上連 no-op 都稱不上——值照常送達前端，只是兩個 Id 都不存在
- adapter 的 `blush_policy`（`drop`/`keep`/`boost`/`neutralize`）對 Rushia 不執行，原值只經 `clamp` 後送出

### 4.6 鬼臉不對稱（`wink_left`、`goofy_*`、`conflicted_*`）總覽

- `wink_left`（`eyeLOpen 0.02`）本體有效（`EyeLOpen` 存在），但搭配的 `blushLevel 0.12` 含羞消失 → 眨眼還在，害羞感打折
- `teasing_cheeky_wink` preset、`conflicted_brow_tilt`、`volatile_twitch`、`playful_brow_spark`、`brow_micro_shape_wave`、`brow_micro_soft_question` 的不對稱眉角度／眉水平／單邊笑眼差全丟 → 鬼臉只剩 `eyeLOpen/eyeROpen` 開合差和 `eyeBallX/Y` 偏移撐場
- prompt 要求的「左右眼開合差 0.12 以上、單邊笑眼差 0.35 以上、眉毛高低或角度差 0.2 以上」三條件中，笑眼差與眉角度差在 Rushia 上永遠無視覺效果

### 4.7 `ParamEyeBallForm`（果冻眼）—— Rushia 獨有、目前閒置

合約、`EXPRESSION_MICRO_EVENT_PATCH_KEYS`、所有 preset / event / adapter 都沒有這個 key。現況是「有參數、沒人用」，方案B 可考慮映射（如把高興奮度的笑眼分量轉為果冻眼），但方案A 不動。

## 5. 方案A現況

### 5.1 前端：no-op 行為（`LAppModel.ts`）

- `applyCurrentExpressionParameters`（約 1598–1630 行）無條件寫入 13 個通道：`Tere`、`Cheek`、`MouthForm`、`BrowLY/RY`、`BrowLAngle/RAngle`、`BrowLForm/RForm`、`EyeLSmile/RSmile`、`BrowLX/RX`。其中 10 個在 Rushia 上不存在。
- Cubism SDK 語義：`idManager.getId()` 永遠回傳 handle，`setParameterValueById()` 遇到模型沒有的 Id 就是靜默 no-op，不報錯、不 crash。這就是方案A 的安全網：同一份 `expression_plan` 可同時餵 Hiyori 與 Rushia。
- 註解缺口：`LAppModel.ts:126–127`（Tere/Cheek）、`:279`（笑眼／BrowLX-RX）目前只提到 Haru / Hiyori / huohuo，尚未標註 Rushia。後續（方案B）建議補一行 `// Rushia_first 缺失：BrowAngle/Form/LX/RX、EyeSmile、Cheek/Tere → no-op`，方案A 不改。
- 仍有效的 Rushia 通道：`EyeLOpen/ROpen`、`EyeBallX/Y`、`BrowLY/RY`、`MouthForm`、`MouthOpenY`（LipSync）、`AngleX/Y/Z`、`BodyAngleX/Y/Z`、`Breath`。

### 5.2 後端：adapter 直接 return（`expression_compiler.py:250–258`）

```python
if model_name.lower() != "hiyori":
    return _clamp_expression_params(params)
```

- Rushia（以及任何非 Hiyori 模型）直接 `clamp` 後回傳，跳過：eye/mouth/brow 的 `energy`/`intensity` 縮放、`goofy_asym` / `angry_meltdown` / `sad_tense` 簽名微調、`blush_policy` 四路徑。數值仍在合約範圍內，前端 type guard 不會擋。
- 效果：Rushia 收到的表情幅度比 Hiyori 保守（少了 adapter 放大），且 `blushLevel` 原值直送（無 Hiyori 的 0.2x 壓縮），但反正前端 no-op，視覺上等於沒送。

### 5.3 Prompt 仍是 Hiyori 口吻（`Hiyori.json`）

- `prompt_config.live2d.general_emotion_hints` 多處 Hiyori-specific 描述：「`eye_*_open` 大（1.05~1.2 Hiyori 上限瞪大眼睛）」、「`mouth_form` 大負值（-0.5~-1.0，Hiyori 可到 -2）」、「`blush_level` -0.5~-1.0（Hiyori 專有）」、「開心大笑 `eye_*_smile` 0.75~1.0」—— LLM 會認真照做，但 Rushia 側笑眼／蒼白／超大眼全無反應。
- `openai_tools.live2d` 的 `eye_*_open` 上限 1.2、`mouth_form` 下限 -2.0、`blush_level` 同時寫 `ParamCheek` 與 `ParamTere` 都是 Hiyori 視角。`schema_loader.py` 的 `DEFAULT_MODEL = "Hiyori"`，目前也沒有按模型切換 schema。
- `persona_hints`（傲嬌／開朗／生氣／魅惑）全用 `brow_angle`、`eye_*_smile`、`blush_level` 描述，Rushia 三者缺二（眉角度、笑眼、臉紅），只剩 `mouth_form`、`brow_y`、`head_intensity` 可見。
- 方案A 接受此落差（先求不壞），方案B 再做 prompt 分流或後處理改寫。

### 5.4 Motion group 差異

| | Hiyori（`public/Resources/Hiyori/Hiyori.model3.json`） | Rushia_first（`RushiaHD.model3.json`） |
|---|---|---|
| Motions | `Idle` ×9（m01/m02/m03/m05–m10，含 FadeIn/Out 0.5）、`TapBody` ×1（m04） | `Idle` ×1、`Blink` ×1、`Nod` ×1、`Shake` ×1（均無 Fade 值） |
| HitAreas | `Head`、`Body(HitArea)` | 無 `HitAreas` key |
| Groups | `LipSync`、`EyeBlink` | `EyeBlink`、`LipSync`（相同，順序不同） |
| 額外引用 | `Pose`、`UserData`、`DisplayInfo` | `Physics`、`DisplayInfo`（無 Pose/UserData） |
| 貼圖 | `Hiyori.2048/` ×2 | `RushiaHD.4096/` ×4（見 §5.6） |

- `MotionController.getPreferredGroups()` 回傳 `['Idle', 'TapBody', 'TapHead']`（`MotionController.ts:193`）；Rushia 沒有 `TapBody`/`TapHead`，點擊身體觸發對應 motion 時等於無動作（或走 fallback，不得依賴）。
- `MotionGroup` 常數（`LAppDefine.ts:106–110`）同樣只有 `Idle`/`TapBody`/`TapHead`，與 Rushia 的 `Nod`/`Shake`/`Blink` 對不上——方案A 不接，有需要才在方案B 映射（例如把點頭／搖頭接到肯定／否定回應）。

### 5.5 HitAreas 缺失

- Rushia `model3.json` 無 `HitAreas` 陣列 → `CubismModelSettingJson.getHitAreasCount()` 回 0 → `LAppModel` 的傳統 HitArea 命中測試直接無區域。
- 好消息：`HitAreaOverlay.tsx` 已改為不依賴 `model3.json`（註解明寫「有些模型設定不良」），用網格投影計算法線顯示 overlay，所以除錯 overlay 在 Rushia 上仍可用。
- 點擊互動（tap → motion）在 Rushia 上目前無 HitArea 可點，方案A 視為已知限制。

### 5.6 4096 貼圖效能提醒

- Rushia：`RushiaHD.4096/texture_00–03.png` 共 4 張 4096 級貼圖；Hiyori：`Hiyori.2048/` ×2。粗估 VRAM 與解碼成本為 Hiyori 的 8 倍（面積 4x × 張數 2x）。
- 低階 GPU／內顯、行動裝置 WebGL 可能出現載入慢、首次渲染卡頓、甚至 context 丟失。方案A 僅提醒：驗證時請用目標機器實測載入時間與 FPS；若過慢，方案C 再考慮壓縮為 2048 或延遲載入。

## 6. 後續方案B／C 建議方向（只條列，不實作）

方案B（表情重映射，讓 Rushia 表情回來）：

- BrowAngle 缺失 → 用 `BrowLY/RY` 高低差模擬角度感（如左眉上＋右眉下表示倒八字），或小幅 `AngleZ` 歪頭補償。
- EyeSmile 缺失 → 用 `EyeLOpen/ROpen` 縮小＋`EyeBallForm`（果冻眼）聯動模擬笑眼；或把笑眼值折算進 `MouthForm`＋`BrowLY` 上揚。
- BrowForm/LX 缺失 → 生氣時改用 `BrowLY` 下壓＋`MouthForm` 負值＋`physicsImpulse` 抖動補強度。
- blush（Cheek/Tere）缺失 → 無替代貼圖層，建議 prompt 分流：Rushia 專用 hints 拿掉臉紅／蒼白描述，改以眼開合＋嘴角＋身體前傾表達害羞／低氣壓。
- prompt 分流：在 `schema_loader`／`get_live2d_tools(model_name)` 層按模型選 hints，或後處理把笑眼／眉角度 key 改寫為 Rushia 可見通道。
- 前端註解補齊 `LAppModel.ts:126–127, 279` 的 Rushia no-op 說明；`MotionGroup` 考慮 `Nod`/`Shake` 映射。

方案C（模型級優化，動 moc／貼圖才需要）：

- 壓縮 `RushiaHD.4096` → 2048（或提供雙解析度），驗證 VRAM／載入時間。
- 評估是否在模型編輯器補 `ParamEyeLSmile/RSmile`、`BrowAngle` 等通道（需重新匯出 moc3，成本最高，僅在B 仍不夠時考慮）。
- 補 `HitAreas`（Head/Body）與 `TapBody` motion，讓點擊互動與 Hiyori 對齊。
- 補 `Pose`／`UserData` 引用（若需要部件淡入淡出）。

## 7. 驗證清單

按順序執行，全部通過才算方案A 驗證完成：

1. **registry**：`backend/model_registry.json` 含 `Rushia_first`（`directory: Rushia_first`、`fileName: RushiaHD.model3.json`、`displayName: Rushia（方案A驗證中）`）。
2. **Resources 路徑**：`vtuber-web-app/public/Resources/Rushia_first/` 下有 `RushiaHD.model3.json`、`RushiaHD.moc3`、`RushiaHD.physics3.json`、`RushiaHD.cdi3.json`、`RushiaHD.4096/`（4 張貼圖）、`RushiaHD.idle/blink/nod/shake.motion3.json`。注意前端實際讀的是 `public/Resources/`（`LAppLive2DManager.ts:86` 組 `/Resources/${directory}/`），`vtuber_model/` 是倉庫源、非執行路徑。
3. **`/api/models`**：後端啟動後 `GET /api/models` 回傳含 `Rushia_first`（經 `models_router.py` → `load_model_registry()`）。
4. **前端下拉切換**：`ControlPanel.tsx:289` 的 `currentModelConfig` 能選到 Rushia 並載入；`HitAreaOverlay` 仍顯示（不依賴 model3.json）；主畫布表情指令不報錯（缺失通道靜默 no-op）。
5. **`check-expression-plan`**：`cd vtuber-web-app && node scripts/check-expression-plan.mjs` 通過（合約本身不分模型，Rushia 照收同一份 plan）。
6. **`tsc`**：`cd vtuber-web-app && cmd /c npx tsc -b --pretty false` 通過（本文件不碰程式碼，理論上無影響；若失敗先跑 `npm run generate` 再重試）。
7. （加選）後端單元測試：`cd backend && python -m unittest backend.tests.test_expression_compiler`；手動對 Rushia 送一次開心＋一次生氣，確認只有眼開合／眉上下／嘴角／身體在動，其餘無反應即符合預期。
