# AI VTuber 專案開發指示

## 專案概述

基於 Web 的 AI 虛擬主播助理，使用 Live2D Cubism SDK 5-r.5-beta.3 和 MotionSync Plugin 5-r.2。

**技術棧**：Vite + React 19 + TypeScript + Zustand + WebGL

---

## 目錄結構規範

```
vtuber-web-app/
├── public/
│   ├── Core/              # live2dcubismcore.js（不可修改）
│   ├── MotionSyncCore/    # MotionSync 核心庫
│   ├── Resources/         # 模型資源（Hiyori, Haru 等）
│   └── Shaders/WebGL/     # SDK 5.3 著色器（必需）
├── src/
│   ├── live2d/            # Live2D 核心邏輯
│   │   ├── framework/     # SDK Framework 原始碼
│   │   ├── LAppModel.ts   # 模型類（核心）
│   │   ├── LAppDefine.ts  # 常數與模型配置
│   │   └── ...
│   ├── audio/             # 音訊處理（麥克風、LipSync）
│   ├── components/        # React UI 元件
│   └── store/             # Zustand 狀態管理
```

---

## Live2D SDK 5.3 關鍵寫法

### 1. Framework 引入路徑（必須使用別名）

```typescript
// ✅ 正確：使用 @framework 別名
import { CubismFramework } from '@framework/live2dcubismframework';
import { CubismUserModel } from '@framework/model/cubismusermodel';
import { CubismMotion } from '@framework/motion/cubismmotion';

// ❌ 錯誤：相對路徑
import { CubismFramework } from '../framework/live2dcubismframework';
```

### 2. 模型類必須繼承 CubismUserModel

```typescript
export class LAppModel extends CubismUserModel {
  // 繼承提供：_model, _moc, _motionManager, _expressionManager 等
}
```

### 3. 動作載入必須呼叫 setEffectIds（關鍵！）

```typescript
// 載入動作後，必須設置眨眼和嘴型同步 ID
const motion = CubismMotion.create(arrayBuffer, arrayBuffer.byteLength);
motion.setEffectIds(this._eyeBlinkIds, this._lipSyncIds);  // 必須呼叫！
```

**原因**：CubismMotion 內部 `_eyeBlinkParameterIds` 和 `_lipSyncParameterIds` 初始為 null，不設置會導致 `Cannot read properties of null` 錯誤。

### 4. 渲染器初始化流程（嚴格順序）

```typescript
// 必須按此順序執行
this.createRenderer(width, height);      // 1. 創建渲染器
renderer.startUp(gl);                     // 2. 設置 WebGL 狀態
renderer.loadShaders('/Shaders/WebGL/');  // 3. 載入著色器（SDK 5.3 必需）
await this.setupTextures();               // 4. 綁定紋理
```

### 5. 參數 ID 獲取方式

```typescript
const idManager = CubismFramework.getIdManager();
this._idParamAngleX = idManager.getId(CubismDefaultParameterId.ParamAngleX);
this._idParamMouthOpenY = idManager.getId(CubismDefaultParameterId.ParamMouthOpenY);
```

### 6. 效果參數 ID 陣列初始化

```typescript
// 在 setupEffects() 中初始化
const eyeBlinkIdCount = this._modelSetting.getEyeBlinkParameterCount();
this._eyeBlinkIds = [];
for (let i = 0; i < eyeBlinkIdCount; i++) {
  this._eyeBlinkIds.push(this._modelSetting.getEyeBlinkParameterId(i));
}

const lipSyncIdCount = this._modelSetting.getLipSyncParameterCount();
this._lipSyncIds = [];
for (let i = 0; i < lipSyncIdCount; i++) {
  this._lipSyncIds.push(this._modelSetting.getLipSyncParameterId(i));
}
```

---

## 開發規範

### 必須遵守

1. **單例模式**：Manager 類使用靜態 `getInstance()` 和 `releaseInstance()`
2. **TypeScript 嚴格模式**：不使用 `any`，明確定義類型
3. **異步處理**：模型載入使用 `async/await`，不使用回調地獄
4. **錯誤處理**：使用 `try/catch`，錯誤透過 `LAppPal.printError()` 輸出

### 禁止行為

- ❌ 不要修改 `framework/` 目錄下的 SDK 原始碼
- ❌ 不要使用回退機制（fallback），遇到問題直接報錯
- ❌ 不要猜測 SDK API，不確定時詢問或查閱官方範例

### 模型配置格式

```typescript
interface ModelConfig {
  name: string;           // 唯一識別名稱
  directory: string;      // 模型目錄
  fileName: string;       // .model3.json 文件名
  displayName: string;    // UI 顯示名稱
}
```

---

## 狀態管理（Zustand）

```typescript
// 使用方式
const { microphoneEnabled, toggleMicrophone } = useAppStore();

// 狀態更新
set({ modelLoading: true });
```

---

## 常見錯誤排查

| 錯誤訊息 | 原因 | 解決方案 |
|---------|------|---------|
| `Cannot read properties of null (reading 'length')` | 動作未設置 effectIds | 呼叫 `motion.setEffectIds()` |
| `CubismMoc 创建失败` | moc3 文件損壞或未載入 Core | 確認 `live2dcubismcore.js` 已載入 |
| `getRenderer() 返回 null` | 未呼叫 `createRenderer()` | 按順序初始化渲染器 |
| 著色器載入失敗 | 缺少 Shaders 目錄 | 確認 `/public/Shaders/WebGL/` 存在 |

---

## 開發流程

1. **不確定需求** → 直接詢問，不要假設
2. **修改 Live2D 相關程式碼** → 參考 `LAppModel.ts` 現有寫法
3. **新增模型** → 在 `LAppDefine.ts` 的 `AvailableModels` 添加配置
4. **測試** → `npm run dev` 後檢查瀏覽器控制台

---

## 參考資源

- 官方 SDK 範例：`CubismSdkForWeb-5-r.5-beta.3/Samples/TypeScript/Demo/`
- MotionSync 範例：`CubismSdkMotionSyncPluginForWeb-5-r.2/Samples/`
- 專案計劃書：`docs/專案實作計劃書.md`
