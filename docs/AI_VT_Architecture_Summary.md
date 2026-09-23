# AI_VT 系統架構整理

## 1. 架構目標

AI_VT 的核心設計方向是將「對話」、「動作」、「情緒狀態」與「記憶」拆分成不同責任模組。

避免讓單一大型語言模型同時負責：

- 對話生成
- 情緒維護
- 動作決策
- 動作執行
- 記憶管理
- 長期摘要

整體採用「前台即時處理 + 後台非同步記憶整理」的概念。

核心原則：

> Chat 決定「說什麼」  
> JEV 決定「怎麼反應」  
> Emotion State 保存「現在是什麼狀態」  
> Action Scheduler 決定「什麼時候真的做」  
> Memory Agent 決定「什麼值得記」

---

# 2. 整體架構

```text
                    ┌────────────────┐
User Input ────────►│ Input Processor │
                    └───────┬────────┘
                            │
             ┌──────────────┴──────────────┐
             │                             │
             ▼                             ▼
      ┌─────────────┐               ┌─────────────┐
      │ JEV Decision │               │ Chat Model  │
      │   Engine     │               │ 快速/急速模型     │
      └──────┬──────┘               └──────┬──────┘
             │                             │
             │                             ▼
             │                      User Response
             │
             ▼
      ┌──────────────┐
      │ Action Planner│
      └──────┬───────┘
             │
       ┌─────┼────────┐
       ▼     ▼        ▼
     即時   計畫     收尾
     動作   動作     動作
       └─────┬────────┘
             ▼
      ┌────────────────┐
      │ Action Scheduler│
      │ 優先權 / 中斷   │
      │ 冷卻 / 動作混合 │
      └───────┬────────┘
              ▼
          VT 動作層


          Shared Runtime State
      ┌──────────────────────────┐
      │ Current Emotion          │
      │ Conversation State       │
      │ Current Action           │
      │ Character State          │
      └──────────────────────────┘

        Chat / JEV / VT 共同讀取


──────────────── Background ────────────────

Conversation / Interaction Event (後端可使用雲端大模型 配置處理 不用很快)
              │
              ▼
      ┌───────────────┐
      │ Memory Agent  │
      └───────┬───────┘
              ▼
      ┌───────────────────┐
      │ Memory Importance │
      │ JEV / Judge 分類   │
      └───────┬───────────┘
              ▼
      ┌───────────────────┐
      │ LLM Consolidation │
      │ 摘要 / 合併 / 去重 │
      └───────┬───────────┘
              ▼
      ┌───────┼──────────────┐
      ▼       ▼              ▼
    短期     長期           特別記憶
    user.update 或是 memory.update 事件
              │
              ▼
          遺忘 / 衰減
```

---

# 3. Input Processor

使用者訊息進入系統後，先經過統一 Input Processor。

可能包含：

- 文字訊息
- 語音轉文字
- 使用者名稱 / 身分
- Session ID
- 時間
- 對話上下文
- 語音特徵
- 其他環境資訊

Input Processor 不負責推理，只負責將輸入整理為標準格式。

例如：

```json
{
  "session_id": "abc123",
  "user_id": "user_01",
  "text": "你今天看起來很開心欸",
  "timestamp": 123456789,
  "context": {}
}
```

整理後的事件同時送往：

1. JEV Decision Engine
2. Chat Engine
3. Memory Event Pipeline

---

# 4. Chat Engine

## 定位

Chat Engine 負責：

> 「角色要說什麼」

主要目標是低延遲產生自然回覆。

因此建議使用：

- 快速小模型
- 高速 API 模型
- 本地低延遲模型

Chat Model 不應負責維護完整角色狀態。

它只需要取得必要的 Context。
包含對話上下文，user對話關聯記憶(之後memory引用向量等)，還有情緒狀態
高輸入理解資訊 輸出低 處理回覆對話

---

## Chat 輸入

Chat Model 可以接收：

```text
User Message
+
Character Prompt
+
Current Emotion Summary
+
Relevant Memory
+
Conversation Context
```

例如：

```json
{
  "emotion": "slightly_shy",
  "mood": "positive",
  "relationship": "familiar",
  "relevant_memory": [
    "使用者喜歡露西亞",
    "昨天聊過 AI_VT"
  ]
}
```

避免直接輸入：

- 大量歷史情緒 Log
- 全部 Memory
- 整個長期 Session
- 大量動作資料

否則快速模型會重新變成高延遲模型。

---

# 5. JEV Decision Engine

## 定位

JEV 不主要負責生成對話。

JEV 的角色是：

> 根據使用者輸入、目前情緒、歷史狀態，判斷 VT 應該怎麼反應。

輸入：

```text
User Message
+
Current Emotion State
+
Conversation State
+
Recent Emotion History
+
Character State
```

輸出則偏向結構化決策。

例如：

```json
{
  "emotion_delta": {
    "happy": 0.1,
    "shy": 0.25
  },
  "reaction": "embarrassed_positive",
  "action": {
    "immediate": "blush",
    "sequence": "look_away",
    "recovery": "neutral"
  }
}
```

---

# 6. Emotion State 不由 JEV 自己保存

這是架構中非常重要的一點。

JEV 應該：

> 讀取情緒 → 判斷 → 更新情緒

而不是：

> 自己記住角色現在什麼情緒

情緒應由 Shared Runtime State 維護。

---

# 7. Shared Runtime State

Shared Runtime State 是整個 AI_VT 的即時狀態中心。

保存：

```text
Current Emotion
Current Action
Conversation State
Character State
Relationship State
Session State
```

例如：

```json
{
  "emotion": {
    "happy": 0.72,
    "shy": 0.31,
    "angry": 0.05
  },
  "valence": 0.65,
  "arousal": 0.32,
  "dominance": 0.45,
  "character_state": {
    "tsundere": 0.42
  },
  "current_action": "idle"
}
```

使用者每次輸入後：

```text
Input
  ↓
JEV
  ↓
Emotion Delta
  ↓
Runtime State Update
```

Chat Model 再讀取新的 Runtime State。

---

# 8. 情緒資料拆成兩層

不建議把所有情緒都放到長期 Memory DB。

應拆成：

## 8.1 Runtime Emotion

短期、即時、高頻更新。

例如：

```text
happy: 0.7
shy: 0.4
angry: 0.1
```

存放位置可以是：

- Process Memory
- Redis
- Session State
- Lightweight KV Store

用途：

- JEV 動作判斷
- Chat 情緒參考
- 動作系統

---

## 8.2 Long-term Emotion Pattern

真正值得保存的是長期趨勢。

例如：

```text
使用者聊到貓時角色通常很興奮

被使用者誇獎時：
shy ↑
tsundere ↑

深夜聊天：
arousal ↓
calm ↑
```

這類才放進 Memory 系統。

---

# 9. Action Planner

JEV 輸出的是「行為決策」。

Action Planner 將它轉換成實際動作計畫。

目前建議分成三種類型。

---

## 9.1 Immediate Action

即時反應。

時間尺度：

```text
約 100 ms ~ 500 ms
```

例如：

- 眨眼
- 眉毛
- 臉紅
- 視線
- 嘴型
- 耳朵
- 簡單手勢

---

## 9.2 Action Sequence

原本的「計畫動作」。

時間尺度：

```text
約 1 ~ 10 秒
```

例如：

```text
看向使用者
→ 揮手
→ 身體靠近
→ 微笑
```

適合：

- 組合動作
- 長動作
- 舞蹈
- 情緒表演
- 場景行為

---

## 9.3 Recovery

原本的「收尾動作」。

它更像：

> 動作結束後回到什麼狀態。

例如：

```text
wave_hand
↓
lower_hand
↓
neutral_pose
```

因此建議正式命名：

```text
Immediate Action
Action Sequence
Recovery
```

---

# 10. Action Scheduler

這是整個動作系統非常重要的一層。

JEV 只負責：

> 決定要做什麼

Scheduler 負責：

> 決定什麼時候做、能不能做、需不需要取消。

否則很容易出現：

```text
正在揮手
↓
突然收到訊息
↓
驚訝
↓
轉頭
↓
低頭
↓
另一個新動作
```

導致動作互相衝突。

---

## Scheduler 建議支援

```text
priority
interruptible
duration
cooldown
cancel
blend
queue
```

例如：

```json
{
  "action": "surprised",
  "priority": 80,
  "interruptible": true,
  "duration": 1200,
  "cooldown": 3000
}
```

---

# 11. VT Adapter Layer

Scheduler 不直接綁死 Live2D。

中間建議再保留 VT Adapter。

例如：

```text
Action Scheduler
      ↓
VT Action Interface
      ↓
 ┌────┼─────┐
 ↓    ↓     ↓
Live2D Inochi2D 3D
```

如此未來替換：

- Live2D
- Inochi2D
- Unity
- VRM
- 3D Avatar

都不需要更改 JEV。

---

# 12. Memory Agent

Memory Agent 在背景運作。

不應阻塞使用者對話。

流程：

```text
Conversation Event
        ↓
Memory Judge
        ↓
Worth Remembering?
        ↓
Memory Classification
        ↓
LLM Consolidation
        ↓
Memory Storage
```

---

# 13. JEV / Judge 在記憶中的角色

JEV 非常適合負責：

> 「這件事情值不值得記？」

而不是直接負責寫完整記憶。

例如輸出：

```json
{
  "store": true,
  "type": "preference",
  "importance": 0.82,
  "ttl": "long"
}
```

可以判斷：

- 是否保存
- Memory Type
- Importance
- TTL
- Special Flag

---

# 14. Memory 分類

建議至少分成：

## Short-term Memory

短期 Session Context。

例如：

```text
剛剛聊過什麼
目前任務
臨時上下文
```

---

## Long-term Memory

長期偏好或關係資料。

例如：

```text
使用者喜歡什麼
重要人物
固定設定
長期習慣
```

---

## Special Memory

特殊事件。

例如：

```text
第一次見面
生日
重要事件
承諾
特殊紀念
```

通常可以：

```text
importance = 1.0
forget = false
```

---

# 15. Large Model Consolidation

大模型在 Memory 系統裡適合負責：

```text
摘要
去重
合併
衝突整理
結構化
```

例如：

原始：

```text
使用者說他最近一直在研究 AI VT
使用者又說 AI VT 是主要專案
使用者今天又討論 AI VT 架構
```

整理後：

```text
使用者目前主要長期專案之一為 AI_VT，
重點研究 Chat、JEV、情緒、動作與記憶架構。
```

這樣可以避免 Memory 無限膨脹。

---

# 16. 遺忘機制

遺忘不建議完全交給 LLM。

建議使用 deterministic scoring。

例如：

```text
Memory Score =
Importance
× Relevance
× Recency Decay
× Access Frequency
```

例如：

```text
score < 0.15
→ Delete Candidate

0.15 ~ 0.40
→ Archive

score > 0.40
→ Keep
```

特殊記憶則可以設定：

```json
{
  "protected": true,
  "decay": false
}
```

---

# 17. 建議核心模組

最後整體可以整理成五個主要責任。

| 模組 | 責任 |
|---|---|
| Chat Engine | 說什麼 |
| JEV Decision Engine | 怎麼反應 |
| Emotion / Runtime State | 現在是什麼狀態 |
| Action Scheduler | 什麼時候執行動作 |
| Memory Agent | 什麼值得記 |

---

# 18. 模型可替換設計

此架構最大的優點是：

> 模型不是系統本身，只是某個模組的實作。

例如：

```text
Chat Engine
Qwen
↓
Gemma
↓
OpenAI API
```

不影響其他系統。

JEV：

```text
JEV Model
↓
Future Judge Model
↓
Rule + Model Hybrid
```

Memory：

```text
Local 8B
↓
27B
↓
Cloud API
```

VT：

```text
Live2D
↓
Inochi2D
↓
VRM
↓
3D Avatar
```

---

# 19. 建議最先實作的版本

第一階段不需要一次完成全部功能。

推薦 MVP：

```text
User Input
   │
   ├── Chat Model
   │       ↓
   │   User Response
   │
   └── JEV
           ↓
      Emotion State
           ↓
      Simple Action
           ↓
        Live2D
```

再逐步加入：

```text
Phase 2
Action Scheduler

Phase 3
Memory Judge

Phase 4
Memory Consolidation

Phase 5
Long-term Emotion Pattern

Phase 6
Advanced Action Sequence
```

---

# 20. 最終定位

AI_VT 不應設計成：

> 一個大型 LLM 控制所有事情。

更合理的方向是：

```text
LLM / JEV
      ↓
Decision Layer
      ↓
Runtime State
      ↓
Scheduler
      ↓
Avatar Runtime
```

搭配：

```text
Background Memory System
```

因此整體更接近：

> AI Agent Runtime + Character Runtime + Memory System

而不是單純的 Chatbot + Live2D。

這樣可以同時獲得：

- 低延遲
- 模型可替換
- 動作穩定
- 記憶可管理
- 情緒連續性
- 系統容易 Debug
- 後續容易擴充
