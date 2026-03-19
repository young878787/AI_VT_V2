# AI 助理開發指南與專案架構 (AI Assistant Development Guide & Architecture)

## 專案概述 (Project Overview)
本專案為基於 Web 的 AI 虛擬主播助理，整合 Live2D 模型渲染、即時麥克風嘴型同步 (MotionSync) 與游標視線追蹤，並預留 AI 對話串接介面。
核心技術棧：Vite + React 19 + TypeScript + Zustand + WebGL。
虛擬主播引擎：Live2D Cubism SDK 5-r.5-beta.3 與 MotionSync Plugin 5-r.2。

## 目錄與架構 (Directory & Architecture)
採用乾淨架構原則 (Clean Architecture)，模組職責分離：
- public/：存放核心庫 (Live2D Core, MotionSync Core)、SDK 著色器 (Shaders) 與模型資源。
- src/live2d/：Live2D 核心邏輯。包含 Framework 原始碼、模型核心類別 (LAppModel) 與應用委託 (LAppDelegate)。
- src/motionsync/：音訊處理與 MotionSync 嘴型同步管理器。
- src/components/：React 介面元件 (Live2DCanvas, ControlPanel 等)。
- src/store/：Zustand 狀態管理 (如麥克風開關、模型載入狀態)。

## 開發最高指導原則 (Core Development Principles)

### 1. 程式碼風格與穩定性優先
- 簡潔明瞭：邏輯必須職責單一，避免過度設計，符合乾淨架構原則。
- 一致性：保持與現有專案架構（包含命名慣例、單例模式管理等）高度一致。
- 完善註解：複雜運算、核心 SDK 呼叫或特定業務邏輯必須加上清晰的註解。
- 嚴格型別：全面使用 TypeScript，禁止使用 any。

### 2. 精準修改與持續推進 (無回退機制)
- 最小化變更：鎖定問題根源進行「精準修正代碼」，絕對不輕易進行大範圍重構。
- 無回退機制：不要寫 fallback (降級或退回) 程式碼。遇到錯誤直接拋出並解決根本原因，確保專案每次都在「進步」與向前推進。

### 3. 語言與溝通規範
- 繁體中文為主：所有的計劃書、Task 說明、程式註解及溝通日誌，必須使用「繁體中文」撰寫 (英文專有名詞及技術術語除外)。
- 無 Emoji 輸出：日誌、回答與程式碼中盡量不要使用任何 Emoji 符號，保持專業與乾淨。
- 主動提問與建議：如果對使用者的要求不理解、發現潛在問題，或想到更好的處理方式，請主動提出問題或建議，不要盲目猜測與執行。

### 4. Live2D 開發特殊注意事項
- Framework 引入必須使用 `@framework` 別名，嚴禁相對路徑。
- 載入動作時必須呼叫 `motion.setEffectIds`，設置眨眼與嘴型同步 ID。
- 渲染器初始化有嚴格順序：創建渲染器 -> startUp -> loadShaders -> setupTextures。
