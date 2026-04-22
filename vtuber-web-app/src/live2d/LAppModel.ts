/**
 * Live2D 模型類（簡化版）
 * 負責單個模型的載入、更新和渲染
 */

import { CubismModelSettingJson } from '@framework/cubismmodelsettingjson';
import { CubismUserModel } from '@framework/model/cubismusermodel';
import { CubismMatrix44 } from '@framework/math/cubismmatrix44';
import { CubismModelMatrix } from '@framework/math/cubismmodelmatrix';
import { CubismDefaultParameterId } from '@framework/cubismdefaultparameterid';
import type { CubismIdHandle } from '@framework/id/cubismid';
import { CubismFramework } from '@framework/live2dcubismframework';
import { CubismEyeBlink } from '@framework/effect/cubismeyeblink';
import { CubismBreath } from '@framework/effect/cubismbreath';
import type { BreathParameterData } from '@framework/effect/cubismbreath';
import type { CubismMotionQueueEntryHandle } from '@framework/motion/cubismmotionqueuemanager';
import { InvalidMotionQueueEntryHandleValue } from '@framework/motion/cubismmotionqueuemanager';
import { ACubismMotion } from '@framework/motion/acubismmotion';
import { CubismMotion } from '@framework/motion/cubismmotion';
import { LAppPal } from './LAppPal';
import type { ModelConfig } from './LAppDefine';
import { ResourcePath, Priority } from './LAppDefine';
import { LAppTextureManager, TextureInfo } from './LAppTextureManager';
import { LAppDelegate } from './LAppDelegate';

/**
 * LAppModel 類
 * 簡化版的模型管理，適合我們的項目結構
 */
export class LAppModel extends CubismUserModel {
  private _modelHomeDir: string = '';
  private _modelSetting: CubismModelSettingJson | null = null;
  private _modelConfig: ModelConfig | null = null;
  private _textureManager: LAppTextureManager;
  protected _modelMatrix: CubismModelMatrix;
  private _canvasWidth: number = 0;
  private _canvasHeight: number = 0;

  // 頭部轉向參數 ID
  private _idParamAngleX: CubismIdHandle;
  private _idParamAngleY: CubismIdHandle;
  private _idParamAngleZ: CubismIdHandle;
  private _idParamBodyAngleX: CubismIdHandle;

  // 眼球參數 ID
  private _idParamEyeBallX: CubismIdHandle;
  private _idParamEyeBallY: CubismIdHandle;

  // LipSync 參數 ID
  private _idParamMouthOpenY: CubismIdHandle;
  private _lipSyncValue: number = 0;

  // 表情參數 ID (AI 控制)
  private _idParamTere: CubismIdHandle;   // Haru 等舊模型用
  private _idParamCheek: CubismIdHandle;  // Hiyori / huohuo 等新模型用（自動偵測）
  private _idParamEyeLOpen: CubismIdHandle;
  private _idParamEyeROpen: CubismIdHandle;

  // 臉部表情擴充參數 ID (AI 控制 - 嘴形、眉毛)
  private _idParamMouthForm: CubismIdHandle;
  private _idParamBrowLY: CubismIdHandle;
  private _idParamBrowRY: CubismIdHandle;
  private _idParamBrowLAngle: CubismIdHandle;
  private _idParamBrowRAngle: CubismIdHandle;
  private _idParamBrowLForm: CubismIdHandle;
  private _idParamBrowRForm: CubismIdHandle;

  // 笑眼與眉毛水平位移參數 ID (AI 控制)
  private _idParamEyeLSmile: CubismIdHandle;  // 左眼笑眼（三個模型均有）
  private _idParamEyeRSmile: CubismIdHandle;  // 右眼笑眼（三個模型均有）
  private _idParamBrowLX: CubismIdHandle;     // 左眉水平（Hiyori/Haru 有，huohuo no-op）
  private _idParamBrowRX: CubismIdHandle;     // 右眉水平（Hiyori/Haru 有，huohuo no-op）

  // 眨眼和嘴型同步 ID 陣列（用於動作效果）
  private _eyeBlinkIds: CubismIdHandle[] = [];
  private _lipSyncIds: CubismIdHandle[] = [];

  // 控制開關
  private _autoEffectsEnabled: boolean = false;  // 自動效果（眨眼、呼吸）
  private _eyeTrackingEnabled: boolean = true;  // 視線追蹤
  private _physicsEnabled: boolean = true;      // 物理效果

  // Draw debug 旗標（私有屬性，切換模型時會自動重置）
  private _drawDebugLogged: boolean = false;

  // 模型變換屬性
  private _modelPositionX: number = 0;           // 模型 X 位置
  private _modelPositionY: number = 0;           // 模型 Y 位置
  private _modelScale: number = 1.0;             // 模型縮放

  // 動作和表情
  private _motions: Map<string, ACubismMotion[]> = new Map();
  private _expressions: Map<string, ACubismMotion> = new Map();
  private _motionsLoaded: boolean = false;

  // AI 控制行為參數 (目標值)
  private _aiHeadIntensity: number = 0;
  private _aiBlushLevel: number = 0;
  private _aiEyeLOpen: number = 1.0;
  private _aiEyeROpen: number = 1.0;
  private _aiEyeBaseLOpen: number = 1.0;
  private _aiEyeBaseROpen: number = 1.0;
  private _aiMouthForm: number = 0.0;
  private _aiBrowLY: number = 0.0;
  private _aiBrowRY: number = 0.0;
  private _aiBrowLAngle: number = 0.0;
  private _aiBrowRAngle: number = 0.0;
  private _aiBrowLForm: number = 0.0;
  private _aiBrowRForm: number = 0.0;
  private _aiEyeLSmile: number = 0.0;
  private _aiEyeRSmile: number = 0.0;
  private _aiBrowLX: number = 0.0;
  private _aiBrowRX: number = 0.0;
  private _aiEyeSync: boolean = true;  // 是否同步（含眉毛）
  private _aiBehaviorTimer: number = 0;

  // AI 控制平滑插值參數 (當前值)
  private _currentBlushLevel: number = 0;
  private _currentEyeLOpen: number = 1.0;
  private _currentEyeROpen: number = 1.0;
  private _currentMouthForm: number = 0.0;
  private _currentBrowLY: number = 0.0;
  private _currentBrowRY: number = 0.0;
  private _currentBrowLAngle: number = 0.0;
  private _currentBrowRAngle: number = 0.0;
  private _currentBrowLForm: number = 0.0;
  private _currentBrowRForm: number = 0.0;
  private _currentEyeLSmile: number = 0.0;
  private _currentEyeRSmile: number = 0.0;
  private _currentBrowLX: number = 0.0;
  private _currentBrowRX: number = 0.0;

  // 原生參數手動覆蓋 (NativeParamPanel 使用者手動控制)
  // key = paramId (string), value = { value, lastSetAt (performance.now ms) }
  private _nativeParamOverrides: Map<string, { value: number; lastSetAt: number }> = new Map();

  /**
   * 建構函式
   */
  constructor() {
    super();

    this._modelMatrix = new CubismModelMatrix();
    this._textureManager = new LAppTextureManager();

    // 初始化參數 ID
    const idManager = CubismFramework.getIdManager();
    this._idParamAngleX = idManager.getId(CubismDefaultParameterId.ParamAngleX);
    this._idParamAngleY = idManager.getId(CubismDefaultParameterId.ParamAngleY);
    this._idParamAngleZ = idManager.getId(CubismDefaultParameterId.ParamAngleZ);
    this._idParamBodyAngleX = idManager.getId(CubismDefaultParameterId.ParamBodyAngleX);

    // 眼球參數 ID
    this._idParamEyeBallX = idManager.getId(CubismDefaultParameterId.ParamEyeBallX);
    this._idParamEyeBallY = idManager.getId(CubismDefaultParameterId.ParamEyeBallY);

    // 表情與眼睛參數 ID
    this._idParamTere = idManager.getId('ParamTere');    // Haru 等舊模型
    this._idParamCheek = idManager.getId('ParamCheek');  // Hiyori / huohuo 新模型
    this._idParamEyeLOpen = idManager.getId(CubismDefaultParameterId.ParamEyeLOpen);
    this._idParamEyeROpen = idManager.getId(CubismDefaultParameterId.ParamEyeROpen);

    // 臉部表情擴充參數 ID（嘴形 + 眉毛）
    this._idParamMouthForm = idManager.getId('ParamMouthForm');
    this._idParamBrowLY = idManager.getId('ParamBrowLY');
    this._idParamBrowRY = idManager.getId('ParamBrowRY');
    this._idParamBrowLAngle = idManager.getId('ParamBrowLAngle');
    this._idParamBrowRAngle = idManager.getId('ParamBrowRAngle');
    this._idParamBrowLForm = idManager.getId('ParamBrowLForm');
    this._idParamBrowRForm = idManager.getId('ParamBrowRForm');

    // 笑眼與眉毛水平位移（三個模型均有笑眼；BrowLX/RX Hiyori/Haru 有，huohuo no-op）
    this._idParamEyeLSmile = idManager.getId('ParamEyeLSmile');
    this._idParamEyeRSmile = idManager.getId('ParamEyeRSmile');
    this._idParamBrowLX = idManager.getId('ParamBrowLX');
    this._idParamBrowRX = idManager.getId('ParamBrowRX');

    // LipSync 參數 ID（嘴巴張開程度）
    this._idParamMouthOpenY = idManager.getId(CubismDefaultParameterId.ParamMouthOpenY);
  }

  /**
   * 載入模型資源
   */
  public async loadAssets(dir: string, fileName: string, modelConfig?: ModelConfig): Promise<void> {
    this._modelHomeDir = dir;
    this._modelConfig = modelConfig || null;

    try {
      // 1. 載入 model3.json
      const modelJsonPath = `${this._modelHomeDir}${fileName}`;
      LAppPal.log(`正在載入 model3.json: ${modelJsonPath}`);
      const response = await fetch(modelJsonPath);

      if (!response.ok) {
        throw new Error(`無法載入 model3.json: ${response.status} ${response.statusText}`);
      }

      const arrayBuffer = await response.arrayBuffer();
      LAppPal.log(`model3.json 已載入，大小: ${arrayBuffer.byteLength} bytes`);

      this._modelSetting = new CubismModelSettingJson(arrayBuffer, arrayBuffer.byteLength);

      // 2. 載入 moc3
      LAppPal.log('開始載入 moc3...');
      await this.setupModel();
      LAppPal.log('✓ moc3 載入完成');

      // 3. 設定其他效果（紋理將在 setupRenderer 後載入）
      LAppPal.log('設定效果...');
      this.setupEffects();

      // 4. 載入動作
      LAppPal.log('載入動作...');
      await this.loadMotions();

      LAppPal.log('✓ 模型資源載入完成');
    } catch (error) {
      LAppPal.printError(`模型載入失敗: ${error}`);
      throw error;
    }
  }

  /**
   * 設定模型
   */
  private async setupModel(): Promise<void> {
    if (!this._modelSetting) return;

    // 載入 moc3
    const mocFileName = this._modelSetting.getModelFileName();
    const mocPath = `${this._modelHomeDir}${mocFileName}`;
    LAppPal.log(`正在加载 moc3: ${mocPath}`);

    const mocResponse = await fetch(mocPath);
    if (!mocResponse.ok) {
      throw new Error(`无法加载 moc3 文件: ${mocPath} (${mocResponse.status} ${mocResponse.statusText})`);
    }

    const mocArrayBuffer = await mocResponse.arrayBuffer();
    if (mocArrayBuffer.byteLength === 0) {
      throw new Error(`moc3 文件为空: ${mocPath}`);
    }

    LAppPal.log(`moc3 文件已加载，大小: ${mocArrayBuffer.byteLength} bytes`);

    // 调用父类的 loadModel 方法（不檢查一致性，避免 SDK 5.3 版本兼容性問題）
    LAppPal.log('调用 CubismUserModel.loadModel()...');
    try {
      // 注意：loadModel 不會拋出異常，需要檢查 _model 和 _moc
      this.loadModel(mocArrayBuffer, false);
    } catch (e) {
      LAppPal.printError(`loadModel 抛出异常: ${e}`);
      throw new Error(`CubismUserModel.loadModel() 失败: ${e}`);
    }

    // 验证模型是否创建成功
    LAppPal.log(`_model 状态: ${this._model ? '已创建' : 'null'}`);
    LAppPal.log(`_moc 状态: ${this._moc ? '已创建' : 'null'}`);

    if (!this._moc) {
      throw new Error('CubismMoc 创建失败，moc3 文件可能损坏、格式不正确，或 Live2DCubismCore 未正确加载');
    }

    if (!this._model) {
      throw new Error('CubismModel 创建失败，createModel() 返回 null');
    }

    LAppPal.log('✓ 模型和 Moc 创建成功');

    // 載入物理
    const physicsFileName = this._modelSetting.getPhysicsFileName();
    if (physicsFileName) {
      const physicsPath = `${this._modelHomeDir}${physicsFileName}`;
      LAppPal.log(`正在加载物理文件: ${physicsPath}`);
      const physicsResponse = await fetch(physicsPath);
      if (physicsResponse.ok) {
        const physicsArrayBuffer = await physicsResponse.arrayBuffer();
        this.loadPhysics(physicsArrayBuffer, physicsArrayBuffer.byteLength);
        LAppPal.log('物理文件加载成功');
      } else {
        LAppPal.printWarning(`物理文件加载失败: ${physicsResponse.status}`);
      }
    }

    // 載入姿勢
    const poseFileName = this._modelSetting.getPoseFileName();
    if (poseFileName) {
      const posePath = `${this._modelHomeDir}${poseFileName}`;
      LAppPal.log(`正在加载姿态文件: ${posePath}`);
      const poseResponse = await fetch(posePath);
      if (poseResponse.ok) {
        const poseArrayBuffer = await poseResponse.arrayBuffer();
        this.loadPose(poseArrayBuffer, poseArrayBuffer.byteLength);
        LAppPal.log('姿态文件加载成功');
      } else {
        LAppPal.printWarning(`姿态文件加载失败: ${poseResponse.status}`);
      }
    }

    // 載入用戶數據
    const userDataFile = this._modelSetting.getUserDataFile();
    if (userDataFile) {
      const userDataPath = `${this._modelHomeDir}${userDataFile}`;
      const userDataResponse = await fetch(userDataPath);
      const userDataArrayBuffer = await userDataResponse.arrayBuffer();
      this.loadUserData(userDataArrayBuffer, userDataArrayBuffer.byteLength);
    }

    // 設定模型矩陣大小（使用 updateModelMatrix 統一管理）
    this.updateModelMatrix();
  }

  /**
   * 設定紋理
   */
  public async setupTextures(): Promise<void> {
    if (!this._modelSetting) return;

    // iPhone/Safari 上為了更好的透明度品質，使用 premultiplied alpha
    const usePremultiply = true;

    const textureCount = this._modelSetting.getTextureCount();
    console.log(`[Live2D] 正在載入 ${textureCount} 個紋理...`);

    for (let modelTextureNumber = 0; modelTextureNumber < textureCount; modelTextureNumber++) {
      const textureFileName = this._modelSetting.getTextureFileName(modelTextureNumber);
      const texturePath = `${this._modelHomeDir}${textureFileName}`;
      console.log(`[Live2D] 載入紋理 ${modelTextureNumber}: ${texturePath}`);

      await new Promise<void>((resolve) => {
        this._textureManager.createTextureFromPngFile(
          texturePath,
          usePremultiply,
          (textureInfo: TextureInfo) => {
            if (textureInfo.id) {
              console.log(`[Live2D] ✓ 紋理 ${modelTextureNumber} 綁定成功, 尺寸: ${textureInfo.width}x${textureInfo.height}`);
              this.getRenderer().bindTexture(
                modelTextureNumber,
                textureInfo.id
              );
            } else {
              console.warn(`[Live2D] ✗ 紋理 ${modelTextureNumber} 綁定失敗`);
            }
            resolve();
          }
        );
      });

      // 每個紋理載入後都設置一次 premultiplied alpha
      this.getRenderer().setIsPremultipliedAlpha(usePremultiply);
    }
  }

  /**
   * 設定效果（眨眼、呼吸等）
   */
  private setupEffects(): void {
    if (!this._modelSetting) return;

    // 眨眼
    if (this._modelSetting.getEyeBlinkParameterCount() > 0) {
      this._eyeBlink = CubismEyeBlink.create(this._modelSetting);
    }

    // 設置眨眼參數 ID 陣列
    const eyeBlinkIdCount = this._modelSetting.getEyeBlinkParameterCount();
    this._eyeBlinkIds = [];
    for (let i = 0; i < eyeBlinkIdCount; i++) {
      this._eyeBlinkIds.push(this._modelSetting.getEyeBlinkParameterId(i));
    }
    LAppPal.log(`眨眼參數數量: ${eyeBlinkIdCount}`);

    // 設置嘴型同步參數 ID 陣列
    const lipSyncIdCount = this._modelSetting.getLipSyncParameterCount();
    this._lipSyncIds = [];
    for (let i = 0; i < lipSyncIdCount; i++) {
      this._lipSyncIds.push(this._modelSetting.getLipSyncParameterId(i));
    }
    LAppPal.log(`嘴型同步參數數量: ${lipSyncIdCount}`);

    // 呼吸
    this._breath = CubismBreath.create();
    const breathParameters: BreathParameterData[] = [
      {
        parameterId: this._idParamAngleX,
        offset: 0.0,
        peak: 15.0,
        cycle: 6.5345,
        weight: 0.5
      },
      {
        parameterId: this._idParamAngleY,
        offset: 0.0,
        peak: 8.0,
        cycle: 3.5345,
        weight: 0.5
      },
      {
        parameterId: this._idParamAngleZ,
        offset: 0.0,
        peak: 10.0,
        cycle: 5.5345,
        weight: 0.5
      },
      {
        parameterId: this._idParamBodyAngleX,
        offset: 0.0,
        peak: 4.0,
        cycle: 15.5345,
        weight: 0.5
      }
    ];
    this._breath.setParameters(breathParameters);
  }

  /**
   * 設置自動效果啟用狀態（眼睛眨眼、呼吸）
   */
  public setAutoEffectsEnabled(enabled: boolean): void {
    this._autoEffectsEnabled = enabled;
  }

  /**
   * 設置視線追蹤啟用狀態
   */
  public setEyeTrackingEnabled(enabled: boolean): void {
    this._eyeTrackingEnabled = enabled;
    // 關閉時重置拖曳位置
    if (!enabled) {
      this._dragX = 0;
      this._dragY = 0;
    }
  }

  /**
   * 設置物理效果啟用狀態
   */
  public setPhysicsEnabled(enabled: boolean): void {
    this._physicsEnabled = enabled;
  }

  // ─────────────────────────────────────────────
  //  AI 眨眼控制 API（blink_control tool 使用）
  // ─────────────────────────────────────────────

  /**
   * 強制立即眨眼（AI 優先，眨眼完成後暫停自動眨眼）
   * @param pauseAfterSec 眨眼完成後暫停自動眨眼的時間（秒），0=不暫停
   */
  public forceBlink(pauseAfterSec: number = 0): void {
    if (this._eyeBlink) {
      this._eyeBlink.forceBlink(pauseAfterSec);
    }
  }

  /**
   * 暫停自動眨眼（AI 接管期間）
   * @param durationSec 暫停時間（秒），0=無期限（需手動 resume）
   */
  public pauseAutoBlink(durationSec: number = 0): void {
    if (this._eyeBlink) {
      this._eyeBlink.pause(durationSec);
    }
  }

  /**
   * 恢復自動眨眼
   */
  public resumeAutoBlink(): void {
    if (this._eyeBlink) {
      this._eyeBlink.resume();
    }
  }

  /**
   * 設置自動眨眼間隔範圍
   * @param minSec 最小間隔（秒）
   * @param maxSec 最大間隔（秒）
   */
  public setBlinkInterval(minSec: number, maxSec: number): void {
    if (this._eyeBlink) {
      this._eyeBlink.setBlinkingIntervalRange(minSec, maxSec);
    }
  }

  /**
   * 取得眨眼狀態（供 UI 顯示）
   */
  public getBlinkState(): { paused: boolean; intervalMin: number; intervalMax: number } {
    if (this._eyeBlink) {
      return {
        paused: this._eyeBlink.isPaused(),
        intervalMin: this._eyeBlink['_blinkingIntervalMin'],
        intervalMax: this._eyeBlink['_blinkingIntervalMax'],
      };
    }
    return { paused: false, intervalMin: 1.0, intervalMax: 4.0 };
  }

  /**
   * 設置模型位置（絕對位置）
   */
  public setModelPosition(x: number, y: number): void {
    this._modelPositionX = x;
    this._modelPositionY = y;
    this.updateModelMatrix();
  }

  /**
   * 移動模型（相對移動）
   */
  public moveModel(deltaX: number, deltaY: number): void {
    this._modelPositionX += deltaX;
    this._modelPositionY += deltaY;
    this.updateModelMatrix();
  }

  /**
   * 設置模型縮放
   */
  public setModelScale(scale: number): void {
    // 限制縮放範圍在 0.1 到 500.0 之間
    this._modelScale = Math.max(0.1, Math.min(500.0, scale));
    this.updateModelMatrix();
  }

  /**
   * 獲取當前縮放
   */
  public getModelScale(): number {
    return this._modelScale;
  }

  /**
   * 更新模型矩陣（統一管理位置和縮放）
   */
  private updateModelMatrix(): void {
    // 1. 先重置矩陣，避免每次移動時重複疊加（這會導致模型飛出畫面）
    this._modelMatrix.loadIdentity();

    // 2. 設定基本大小（這會確保 _tr[0] 和 _tr[5] 被正確初始化並維持模型比例）
    // 避免 setWidth 和 setHeight 互相覆蓋
    if (this._modelMatrix.getScaleX() === 0 || this._modelMatrix.getScaleY() === 0) {
      this._modelMatrix.setHeight(2.0);
    } else {
      // 由於 CubismModelMatrix 設計，這裡直接固定使用邏輯高度 2.0 是最穩定的 Live2D 標準
      this._modelMatrix.setHeight(2.0);
    }

    // 3. 應用的位置（由於沒有 translateRelative 的疊加，直接 translate 設定絕對位置即可確保平移）
    if (this._modelPositionX !== 0 || this._modelPositionY !== 0) {
      this._modelMatrix.translate(this._modelPositionX, this._modelPositionY);
    }

    // 4. 應用縮放（相對基本尺寸再做縮放，避免覆蓋掉維持比例的 scale）
    if (this._modelScale !== 1.0) {
      this._modelMatrix.scaleRelative(this._modelScale, this._modelScale);
    }
  }

  /**
   * 重置模型變換（位置和縮放）
   */
  public resetTransform(): void {
    this._modelPositionX = 0;
    this._modelPositionY = 0;
    this._modelScale = 1.0;
    this.updateModelMatrix();
  }

  /**
   * 點擊檢測（檢查是否點擊在模型上）
   * @param x 歸一化座標 X (-1 到 1)
   * @param y 歸一化座標 Y (-1 到 1)
   * @param hitAreaName 點擊區域名稱 (例如 'Head', 'Body')
   * @returns 是否點擊在指定區域
   */
  public hitTest(x: number, y: number, hitAreaName?: string): boolean {
    if (!this._model) return false;

    // 將整個模型視為單一的可點擊 Body 區域（無論點擊模型哪個部分都算數）
    // 如果沒有傳入名稱，或者名稱是 'Body'，則檢測整個模型的外邊界
    if (!hitAreaName || hitAreaName.toLowerCase() === 'body') {
      const drawableCount = this._model.getDrawableCount();
      const tx: number = this._modelMatrix.invertTransformX(x);
      const ty: number = this._modelMatrix.invertTransformY(y);

      for (let i = 0; i < drawableCount; i++) {
        const vertexCount = this._model.getDrawableVertexCount(i);
        if (vertexCount === 0) continue;
        
        // 可選：跳過完全透明的部件
        if (this._model.getDrawableOpacity(i) <= 0.01) continue;

        const vertices = this._model.getDrawableVertices(i);
        
        let left = vertices[0];
        let right = vertices[0];
        let top = vertices[1];
        let bottom = vertices[1];

        for (let j = 1; j < vertexCount; ++j) {
          const vx = vertices[j * 2];
          const vy = vertices[j * 2 + 1];
          if (vx < left) left = vx;
          if (vx > right) right = vx;
          if (vy < top) top = vy;
          if (vy > bottom) bottom = vy;
        }

        // 只要點中任何一個有效網格的包圍盒即算作點擊了 Body
        if (left <= tx && tx <= right && top <= ty && ty <= bottom) {
          return true; 
        }
      }
      // 如果需要保留對傳統 HitArea 的兼容性，可以繼續往下執行，但我們為了統一，沒點中網格就直接返回 false
      return false;
    }

    // 若需要精確檢查其他特定的 Area (例如 Head)
    if (this._modelSetting) {
      const hitAreaCount = this._modelSetting.getHitAreasCount();
      for (let i = 0; i < hitAreaCount; i++) {
        const name = this._modelSetting.getHitAreaName(i);
        if (name && name.toLowerCase() === hitAreaName.toLowerCase()) {
          const drawableId = this._modelSetting.getHitAreaId(i);
          if (drawableId) {
            return this.isHit(drawableId, x, y);
          }
        }
      }
    }

    return false;
  }

  /**
   * 檢查是否點擊在頭部區域
   */
  public isHitHead(x: number, y: number): boolean {
    return this.hitTest(x, y, 'Head');
  }

  /**
   * 載入動作文件
   */
  private async loadMotions(): Promise<void> {
    if (!this._modelSetting || this._motionsLoaded) return;

    const motionGroupCount = this._modelSetting.getMotionGroupCount();
    LAppPal.log(`發現 ${motionGroupCount} 個動作群組`);

    for (let i = 0; i < motionGroupCount; i++) {
      const groupName = this._modelSetting.getMotionGroupName(i);
      const motionCount = this._modelSetting.getMotionCount(groupName);

      LAppPal.log(`載入動作群組 "${groupName}"：${motionCount} 個動作`);

      const motions: ACubismMotion[] = [];

      for (let j = 0; j < motionCount; j++) {
        const motionFileName = this._modelSetting.getMotionFileName(groupName, j);
        const motionPath = `${this._modelHomeDir}${motionFileName}`;

        try {
          const response = await fetch(motionPath);
          if (!response.ok) {
            LAppPal.printWarning(`動作文件載入失敗: ${motionPath}`);
            continue;
          }

          const arrayBuffer = await response.arrayBuffer();
          const motion = CubismMotion.create(arrayBuffer, arrayBuffer.byteLength);

          if (motion) {
            // 設置淡入淡出時間
            const fadeInTime = this._modelSetting.getMotionFadeInTimeValue(groupName, j);
            const fadeOutTime = this._modelSetting.getMotionFadeOutTimeValue(groupName, j);

            if (fadeInTime >= 0) {
              motion.setFadeInTime(fadeInTime);
            }
            if (fadeOutTime >= 0) {
              motion.setFadeOutTime(fadeOutTime);
            }

            // 關鍵：設置眨眼和嘴型同步的效果 ID
            motion.setEffectIds(this._eyeBlinkIds, this._lipSyncIds);

            motions.push(motion);
            LAppPal.log(`  ✓ ${motionFileName}`);
          }
        } catch (error) {
          LAppPal.printWarning(`動作載入錯誤: ${motionPath} - ${error}`);
        }
      }

      if (motions.length > 0) {
        this._motions.set(groupName, motions);
      }
    }

    this._motionsLoaded = true;
    LAppPal.log(`✓ 動作載入完成，共 ${this._motions.size} 個群組`);
  }

  /**
   * 取得可用的動作群組名稱
   */
  public getMotionGroupNames(): string[] {
    return Array.from(this._motions.keys());
  }

  /**
   * 取得指定群組的動作數量
   */
  public getMotionCount(groupName: string): number {
    const motions = this._motions.get(groupName);
    return motions ? motions.length : 0;
  }

  /**
   * 設置 LipSync 值（由外部調用）
   * @param value 嘴巴張開程度 (0.0 - 1.0)
   */
  public setLipSyncValue(value: number): void {
    this._lipSyncValue = Math.max(0, Math.min(1, value));
  }

  /**
   * 取得當前 LipSync 值
   */
  public getLipSyncValue(): number {
    return this._lipSyncValue;
  }

  /**
   * 設置 AI 行為參數 (頭部擺動、臉紅、左右眼、嘴形、眉毛、笑眼、眉毛水平)
   * @param headIntensity 頭部動作幅度 (0.0~1.0)
   * @param blushLevel 臉頰狀態 (-1.0=蒼白/緊張 Hiyori 專有, 0.0=自然, 1.0=臉紅)
   * @param eyeLOpen 左眼開合 (0.0=閉眼, 1.0=正常張開預設, 2.0=瞪大眼睛)
   * @param eyeROpen 右眼開合 (0.0=閉眼, 1.0=正常張開預設, 2.0=瞪大眼睛)
   * @param durationSec 持續時間(秒)
   * @param mouthForm 嘴角形狀 (-2.0~1.0, Hiyori 支援 -2; 其他模型 -1; +1=大笑)
   * @param browLY 左眉毛高低 (-1.0~1.0)
   * @param browRY 右眉毛高低 (-1.0~1.0)
   * @param browLAngle 左眉毛角度 (-1.0~1.0)
   * @param browRAngle 右眉毛角度 (-1.0~1.0)
   * @param browLForm 左眉毛凸彎 (-1.0~1.0)
   * @param browRForm 右眉毛凸彎 (-1.0~1.0)
   * @param eyeSync 是否同步左右眼與眉毛 (true=對稱)
   * @param eyeLSmile 左眼笑眼程度 (0.0=無笑意, 1.0=瞇眼大笑)
   * @param eyeRSmile 右眼笑眼程度 (0.0=無笑意, 1.0=瞇眼大笑)
   * @param browLX 左眉毛水平位移 (-1.0=向外, +1.0=向內)，Hiyori/Haru 有效
   * @param browRX 右眉毛水平位移 (-1.0=向外, +1.0=向內)，Hiyori/Haru 有效
   */
  public setAiBehavior(
    headIntensity: number, 
    blushLevel: number, 
    eyeLOpen: number, 
    eyeROpen: number, 
    durationSec: number = 5.0,
    mouthForm: number = 0.0,
    browLY: number = 0.0,
    browRY: number = 0.0,
    browLAngle: number = 0.0,
    browRAngle: number = 0.0,
    browLForm: number = 0.0,
    browRForm: number = 0.0,
    eyeSync: boolean = true,
    eyeLSmile: number = 0.0,
    eyeRSmile: number = 0.0,
    browLX: number = 0.0,
    browRX: number = 0.0,
  ): void {
    this._aiHeadIntensity = Math.max(0, Math.min(1, headIntensity));
    this._aiBlushLevel = Math.max(-1, Math.min(1, blushLevel));    // -1=蒼白(Hiyori), 0=自然, 1=臉紅
    this._aiEyeLOpen = Math.max(0, Math.min(2, eyeLOpen));          // 1.0=預設, 2.0=瞪大
    this._aiEyeROpen = Math.max(0, Math.min(2, eyeROpen));          // 1.0=預設, 2.0=瞪大
    this._aiEyeBaseLOpen = this._aiEyeLOpen;
    this._aiEyeBaseROpen = this._aiEyeROpen;
    this._aiMouthForm = Math.max(-2, Math.min(1, mouthForm));        // -2=Hiyori極悲傷, -1=悲傷, 0=中性, 1=大笑
    this._aiBrowLY = Math.max(-1, Math.min(1, browLY));
    this._aiBrowRY = Math.max(-1, Math.min(1, browRY));
    this._aiBrowLAngle = Math.max(-1, Math.min(1, browLAngle));
    this._aiBrowRAngle = Math.max(-1, Math.min(1, browRAngle));
    this._aiBrowLForm = Math.max(-1, Math.min(1, browLForm));
    this._aiBrowRForm = Math.max(-1, Math.min(1, browRForm));
    this._aiEyeSync = eyeSync;
    this._aiEyeLSmile = Math.max(0, Math.min(1, eyeLSmile));
    this._aiEyeRSmile = Math.max(0, Math.min(1, eyeRSmile));
    this._aiBrowLX = Math.max(-1, Math.min(1, browLX));
    this._aiBrowRX = Math.max(-1, Math.min(1, browRX));
    this._aiBehaviorTimer = durationSec;
  }

  /**
   * 取得目前 AI 控制的表情參數值（供 UI 面板即時讀取）
   */
  public getAiParams(): {
    headIntensity: number;
    blushLevel: number;
    eyeLOpen: number;
    eyeROpen: number;
    mouthForm: number;
    browLY: number;
    browRY: number;
    browLAngle: number;
    browRAngle: number;
    browLForm: number;
    browRForm: number;
    eyeSync: boolean;
    eyeLSmile: number;
    eyeRSmile: number;
    browLX: number;
    browRX: number;
    timerRemaining: number;
  } {
    return {
      // 回傳目前插值的「真實顯示值」，非目標值
      // 這樣在 AI 動作結束後「漸退」期間也能正確顯示
      headIntensity: this._aiHeadIntensity,
      blushLevel:    this._currentBlushLevel,
      eyeLOpen:      this._currentEyeLOpen,
      eyeROpen:      this._currentEyeROpen,
      mouthForm:     this._currentMouthForm,
      browLY:         this._currentBrowLY,
      browRY:         this._currentBrowRY,
      browLAngle:     this._currentBrowLAngle,
      browRAngle:     this._currentBrowRAngle,
      browLForm:      this._currentBrowLForm,
      browRForm:      this._currentBrowRForm,
      eyeSync:       this._aiEyeSync,
      eyeLSmile:     this._currentEyeLSmile,
      eyeRSmile:     this._currentEyeRSmile,
      browLX:        this._currentBrowLX,
      browRX:        this._currentBrowRX,
      timerRemaining: this._aiBehaviorTimer,
    };
  }

  // ─────────────────────────────────────────────
  //  原生參數手動覆蓋 API (NativeParamPanel 使用)
  // ─────────────────────────────────────────────

  /**
   * 取得模型所有 Cubism 原生參數（供 NativeParamPanel 動態列出）
   */
  public getAllParameters(): Array<{
    id: string;
    min: number;
    max: number;
    defaultVal: number;
    current: number;
  }> {
    if (!this._model) return [];
    const count = this._model.getParameterCount();
    const result: Array<{ id: string; min: number; max: number; defaultVal: number; current: number }> = [];
    for (let i = 0; i < count; i++) {
      result.push({
        id:         this._model.getParameterId(i).getString(),
        min:        this._model.getParameterMinimumValue(i),
        max:        this._model.getParameterMaximumValue(i),
        defaultVal: this._model.getParameterDefaultValue(i),
        current:    this._model.getParameterValueByIndex(i),
      });
    }
    return result;
  }

  /**
   * 設定原生參數手動覆蓋值（立即套用，5 秒無操作後由 update() 自動清除）
   * @param paramId  Cubism 參數 ID 字串（例如 "ParamAngleX"）
   * @param value    目標值（會被 clamp 到模型 min/max）
   */
  public setNativeParamOverride(paramId: string, value: number): void {
    this._nativeParamOverrides.set(paramId, { value, lastSetAt: performance.now() });
  }

  /**
   * 清除指定參數的手動覆蓋，恢復 AI / 眼動追蹤控制
   */
  public clearNativeParamOverride(paramId: string): void {
    this._nativeParamOverrides.delete(paramId);
  }

  /**
   * 清除所有手動覆蓋（全部重置按鈕）
   */
  public clearAllNativeParamOverrides(): void {
    this._nativeParamOverrides.clear();
  }

  /**
   * 取得目前所有活躍的原生參數覆蓋（供 NativeParamPanel 讀取倒數）
   */
  public getNativeParamOverrides(): ReadonlyMap<string, { value: number; lastSetAt: number }> {
    return this._nativeParamOverrides;
  }

  /**
   * 初始化渲染器（注意：此方法是同步的，但需要异步加载纹理）
   */
  public setupRenderer(gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    if (!this._model) {
      const error = 'setupRenderer: _model 为 null，loadAssets 可能未成功完成或模型创建失败';
      LAppPal.printError(error);
      throw new Error(error);
    }

    if (!gl) {
      const error = 'setupRenderer: WebGL 上下文为 null';
      LAppPal.printError(error);
      throw new Error(error);
    }

    this._textureManager.setGl(gl);

    // 獲取 Canvas 尺寸用於創建 renderer
    const canvas = gl.canvas as HTMLCanvasElement;
    const width = canvas.width || 800;
    const height = canvas.height || 600;

    // 保存 canvas 尺寸
    this._canvasWidth = width;
    this._canvasHeight = height;

    // 創建 renderer（必須在 initialize 之前調用）
    this.createRenderer(width, height);

    // 再次獲取 renderer 確認創建成功
    const renderer = this.getRenderer();
    if (!renderer) {
      const error = 'createRenderer() 調用後 getRenderer() 仍返回 null';
      LAppPal.printError(error);
      throw new Error(error);
    }

    // 設置渲染目標尺寸
    this.setRenderTargetSize(width, height);

    // startUp 設置 WebGL 狀態
    renderer.startUp(gl);

    // 載入著色器（SDK 5.3 關鍵步驟）
    const shaderPath = '/Shaders/WebGL/';
    LAppPal.log(`正在載入著色器: ${shaderPath}`);
    renderer.loadShaders(shaderPath);
    LAppPal.log('✓ 著色器載入完成');

    LAppPal.log('✓ Renderer 初始化成功');
  }

  /**
   * 更新模型
   */
  public update(): void {
    if (!this._model) return;

    const deltaTimeSeconds = LAppPal.getDeltaTime();

    // 更新拖曳
    this._dragManager.update(deltaTimeSeconds);
    this._dragX = this._dragManager.getX();
    this._dragY = this._dragManager.getY();

    // 更新動作
    this._model.loadParameters();

    // 只在自動播放啟用時才自動播放待機動作
    if (this._autoEffectsEnabled) {
      if (this._motionManager.isFinished()) {
        // 播放待機動作
        this.startRandomMotion('Idle', Priority.Idle);
      } else {
        this._motionManager.updateMotion(this._model, deltaTimeSeconds);
      }
    } else {
      // 即使不自動播放，如果有正在播放的動作也要更新完成
      if (!this._motionManager.isFinished()) {
        this._motionManager.updateMotion(this._model, deltaTimeSeconds);
      }
    }

    this._model.saveParameters();

    // ── AI 表情計算（先計算基礎值，供眨眼疊加使用）──
    let targetBlush: number;
    let targetEyeL: number;
    let targetEyeR: number;
    let targetMouthForm: number;
    let targetBrowLY: number;
    let targetBrowRY: number;
    let targetBrowLAngle: number;
    let targetBrowRAngle: number;
    let targetBrowLForm: number;
    let targetBrowRForm: number;
    let targetEyeLSmile: number;
    let targetEyeRSmile: number;
    let targetBrowLX: number;
    let targetBrowRX: number;

    if (this._aiBehaviorTimer > 0) {
      this._aiBehaviorTimer -= deltaTimeSeconds;

      let activeIntensity = this._aiHeadIntensity;
      if (this._aiBehaviorTimer < 1.0) {
        activeIntensity = this._aiHeadIntensity * this._aiBehaviorTimer;
      }

      if (activeIntensity > 0.001) {
        const time = Date.now() / 1000;
        const freqZ = 2.0 + (3.0 * this._aiHeadIntensity);
        const freqX = freqZ * 0.5;
        const freqY = freqZ * 0.5;
        const ampZ = 25 * activeIntensity;
        const ampX = 15 * activeIntensity;
        const ampY = 10 * activeIntensity;

        this._model.addParameterValueById(this._idParamAngleZ, Math.sin(time * freqZ) * ampZ);
        this._model.addParameterValueById(this._idParamAngleX, Math.cos(time * freqX) * ampX);
        this._model.addParameterValueById(this._idParamAngleY, Math.cos(time * freqY + Math.PI/4) * ampY);
        this._model.addParameterValueById(this._idParamBodyAngleX, Math.sin(time * freqZ) * (ampZ * 0.6));
      } else {
        // headIntensity 為 0 時跳過頭部搖晃，但不重置計時器
        // _aiBehaviorTimer 應自然倒計時，讓其他表情參數（mouthForm/blush/brow 等）繼續持續
        this._aiHeadIntensity = 0;
      }

      targetBlush     = this._aiBlushLevel;
      targetEyeL      = this._aiEyeLOpen;
      targetEyeR      = this._aiEyeROpen;
      targetMouthForm = this._aiMouthForm;
      targetBrowLY    = this._aiBrowLY;
      targetBrowRY    = this._aiBrowRY;
      targetBrowLAngle = this._aiBrowLAngle;
      targetBrowRAngle = this._aiBrowRAngle;
      targetBrowLForm = this._aiBrowLForm;
      targetBrowRForm = this._aiBrowRForm;
      targetEyeLSmile = this._aiEyeLSmile;
      targetEyeRSmile = this._aiEyeRSmile;
      targetBrowLX    = this._aiBrowLX;
      targetBrowRX    = this._aiBrowRX;
    } else {
      this._aiHeadIntensity = 0;
      targetBlush     = 0.0;
      targetEyeL      = 1.0;
      targetEyeR      = 1.0;
      targetMouthForm = 0.0;
      targetBrowLY    = 0.0;
      targetBrowRY    = 0.0;
      targetBrowLAngle = 0.0;
      targetBrowRAngle = 0.0;
      targetBrowLForm = 0.0;
      targetBrowRForm = 0.0;
      targetEyeLSmile = 0.0;
      targetEyeRSmile = 0.0;
      targetBrowLX    = 0.0;
      targetBrowRX    = 0.0;
    }

    const lerpFactor = Math.min(1.0, 5.0 * deltaTimeSeconds);
    this._currentBlushLevel += (targetBlush - this._currentBlushLevel) * lerpFactor;
    this._currentEyeLOpen += (targetEyeL - this._currentEyeLOpen) * lerpFactor;
    this._currentEyeROpen += (targetEyeR - this._currentEyeROpen) * lerpFactor;
    this._currentMouthForm += (targetMouthForm - this._currentMouthForm) * lerpFactor;
    this._currentBrowLY += (targetBrowLY - this._currentBrowLY) * lerpFactor;
    this._currentBrowRY += (targetBrowRY - this._currentBrowRY) * lerpFactor;
    this._currentBrowLAngle += (targetBrowLAngle - this._currentBrowLAngle) * lerpFactor;
    this._currentBrowRAngle += (targetBrowRAngle - this._currentBrowRAngle) * lerpFactor;
    this._currentBrowLForm += (targetBrowLForm - this._currentBrowLForm) * lerpFactor;
    this._currentBrowRForm += (targetBrowRForm - this._currentBrowRForm) * lerpFactor;
    this._currentEyeLSmile += (targetEyeLSmile - this._currentEyeLSmile) * lerpFactor;
    this._currentEyeRSmile += (targetEyeRSmile - this._currentEyeRSmile) * lerpFactor;
    this._currentBrowLX += (targetBrowLX - this._currentBrowLX) * lerpFactor;
    this._currentBrowRX += (targetBrowRX - this._currentBrowRX) * lerpFactor;

    if (this._aiEyeSync && (this._aiBehaviorTimer > 0 || targetEyeL < 0.99)) {
      this._currentBrowRY = this._currentBrowLY;
      this._currentBrowRAngle = -this._currentBrowLAngle;
      this._currentBrowRForm = this._currentBrowLForm;
      this._currentEyeROpen = this._currentEyeLOpen;
      this._currentEyeRSmile = this._currentEyeLSmile;
      this._currentBrowRX = -this._currentBrowLX;  // 鏡像：左眉向內時右眉對稱向內
    }

    // 更新 AI 眼睛基礎值（供眨眼疊加使用）
    this._aiEyeBaseLOpen = this._currentEyeLOpen;
    this._aiEyeBaseROpen = this._currentEyeROpen;

    // ── 眨眼（在 AI 眼睛基礎值上疊加）──
    if (this._eyeBlink) {
      // 先設置 AI 眼睛基礎值
      this._model.setParameterValueById(this._idParamEyeLOpen, this._aiEyeBaseLOpen);
      this._model.setParameterValueById(this._idParamEyeROpen, this._aiEyeBaseROpen);

      // 執行眨眼
      this._eyeBlink.updateParameters(this._model, deltaTimeSeconds);

      // 如果 AI 有設置瞇眼（基礎值 < 1.0），縮放眨眼結果實現「瞇小眼眨眼」
      if (this._aiEyeBaseLOpen < 0.99 || this._aiEyeBaseROpen < 0.99) {
        const blinkL = this._model.getParameterValueById(this._idParamEyeLOpen);
        const blinkR = this._model.getParameterValueById(this._idParamEyeROpen);
        this._model.setParameterValueById(this._idParamEyeLOpen, blinkL * this._aiEyeBaseLOpen);
        this._model.setParameterValueById(this._idParamEyeROpen, blinkR * this._aiEyeBaseROpen);
      }
    }

    // 表情
    if (this._expressionManager) {
      this._expressionManager.updateMotion(this._model, deltaTimeSeconds);
    }

    // 視線追蹤效果 - 只在視線追蹤啟用時
    if (this._eyeTrackingEnabled) {
      this._model.addParameterValueById(this._idParamAngleX, this._dragX * 45);
      this._model.addParameterValueById(this._idParamAngleY, this._dragY * 45);
      this._model.addParameterValueById(this._idParamAngleZ, this._dragX * this._dragY * -45);
      this._model.addParameterValueById(this._idParamBodyAngleX, this._dragX * 15);
      this._model.addParameterValueById(this._idParamEyeBallX, this._dragX * 1.5);
      this._model.addParameterValueById(this._idParamEyeBallY, this._dragY * 1.5);
    }

    // LipSync（嘴型同步）
    if (this._lipSyncValue > 0) {
      this._model.addParameterValueById(this._idParamMouthOpenY, this._lipSyncValue);
    }

    // 注入其他 AI 表情參數（不含眼睛，眼睛已由眨眼處理）
    const hasExpression = this._aiBehaviorTimer > 0
      || this._currentBlushLevel > 0.01
      || Math.abs(this._currentMouthForm) > 0.01
      || Math.abs(this._currentBrowLY) > 0.01
      || Math.abs(this._currentBrowRY) > 0.01
      || Math.abs(this._currentBrowLAngle) > 0.01
      || Math.abs(this._currentBrowRAngle) > 0.01
      || Math.abs(this._currentBrowLForm) > 0.01
      || Math.abs(this._currentBrowRForm) > 0.01
      || this._currentEyeLSmile > 0.01
      || this._currentEyeRSmile > 0.01
      || Math.abs(this._currentBrowLX) > 0.01
      || Math.abs(this._currentBrowRX) > 0.01;

    if (hasExpression) {
      // 臉紅：同時寫入 ParamTere（Haru 等舊模型）和 ParamCheek（Hiyori/huohuo 新模型）
      // Cubism SDK 對不存在的參數 ID 執行 set 為 no-op，因此雙寫安全。
      this._model.setParameterValueById(this._idParamTere, this._currentBlushLevel);
      this._model.setParameterValueById(this._idParamCheek, this._currentBlushLevel);
      this._model.setParameterValueById(this._idParamMouthForm, this._currentMouthForm);
      this._model.setParameterValueById(this._idParamBrowLY, this._currentBrowLY);
      this._model.setParameterValueById(this._idParamBrowRY, this._currentBrowRY);
      this._model.setParameterValueById(this._idParamBrowLAngle, this._currentBrowLAngle);
      this._model.setParameterValueById(this._idParamBrowRAngle, this._currentBrowRAngle);
      this._model.setParameterValueById(this._idParamBrowLForm, this._currentBrowLForm);
      this._model.setParameterValueById(this._idParamBrowRForm, this._currentBrowRForm);
      // 笑眼（三個模型均有 ParamEyeLSmile/RSmile）
      this._model.setParameterValueById(this._idParamEyeLSmile, this._currentEyeLSmile);
      this._model.setParameterValueById(this._idParamEyeRSmile, this._currentEyeRSmile);
      // 眉毛水平位移（Hiyori/Haru 有效；huohuo no-op）
      this._model.setParameterValueById(this._idParamBrowLX, this._currentBrowLX);
      this._model.setParameterValueById(this._idParamBrowRX, this._currentBrowRX);
    }

    // 呼吸（只在自動效果啟用時）
    if (this._breath && this._autoEffectsEnabled) {
      this._breath.updateParameters(this._model, deltaTimeSeconds);
    }

    // 物理（只在物理效果啟用時）
    if (this._physics && this._physicsEnabled) {
      this._physics.evaluate(this._model, deltaTimeSeconds);
    }

    // 姿勢
    if (this._pose) {
      this._pose.updateParameters(this._model, deltaTimeSeconds);
    }

    // ── 原生參數手動覆蓋（最高優先度，最後套用）──
    // 優先序：NativeParam > AI 表情 > 眼動追蹤 > 呼吸物理
    // 逾時 5 秒未操作自動清除
    if (this._nativeParamOverrides.size > 0) {
      const nowMs = performance.now();
      const toExpire: string[] = [];
      for (const [paramId, override] of this._nativeParamOverrides) {
        if (nowMs - override.lastSetAt > 5000) {
          toExpire.push(paramId);
        } else {
          // 以絕對值覆蓋（不受 add 累積影響）
          // 透過 IdManager 取得對應的 CubismIdHandle 物件（cached 引用相等性）
          this._model.setParameterValueById(
            CubismFramework.getIdManager().getId(paramId),
            override.value
          );
        }
      }
      for (const paramId of toExpire) {
        this._nativeParamOverrides.delete(paramId);
      }
    }

    this._model.update();
  }

  /**
   * 繪製模型
   */
  public draw(matrix: CubismMatrix44): void {
    if (!this._model) {
      console.warn('[Draw] _model is null');
      return;
    }

    const renderer = this.getRenderer();
    if (!renderer) {
      console.warn('[Draw] renderer is null');
      return;
    }

    // 獲取正確的 frameBuffer（來自 LAppDelegate）
    const delegate = LAppDelegate.getInstance();
    const frameBuffer = delegate.getFrameBuffer();

    // 設定 viewport（官方 SDK 需要這個）
    const viewport: number[] = [0, 0, this._canvasWidth, this._canvasHeight];
    renderer.setRenderState(frameBuffer as WebGLFramebuffer, viewport);

    // 計算模型矩陣
    const mvpMatrix = new CubismMatrix44();
    mvpMatrix.setMatrix(matrix.getArray());
    mvpMatrix.multiplyByMatrix(this._modelMatrix);

    // 調試：只輸出一次（使用私有屬性，切換模型時正確重置）
    if (!this._drawDebugLogged) {
      console.log('[Draw] MVP Matrix:', mvpMatrix.getArray());
      console.log('[Draw] Model Matrix:', this._modelMatrix.getArray());
      console.log('[Draw] Projection Matrix:', matrix.getArray());
      console.log('[Draw] Canvas Size:', this._canvasWidth, this._canvasHeight);
      console.log('[Draw] FrameBuffer:', frameBuffer);
      this._drawDebugLogged = true;
    }

    renderer.setMvpMatrix(mvpMatrix);
    renderer.drawModel(ResourcePath.ShaderPath);
  }

  /**
   * 開始隨機動作
   */
  public startRandomMotion(
    group: string,
    priority: number
  ): CubismMotionQueueEntryHandle {
    const motions = this._motions.get(group);
    if (!motions || motions.length === 0) {
      return InvalidMotionQueueEntryHandleValue;
    }

    const no = Math.floor(Math.random() * motions.length);
    return this.startMotion(group, no, priority);
  }

  /**
   * 開始指定動作
   */
  public startMotion(
    group: string,
    no: number,
    priority: number
  ): CubismMotionQueueEntryHandle {
    const motions = this._motions.get(group);
    if (!motions || no >= motions.length) {
      return InvalidMotionQueueEntryHandleValue;
    }

    const motion = motions[no];
    return this._motionManager.startMotionPriority(motion, false, priority);
  }

  /**
   * 設定表情
   */
  public setExpression(expressionId: string): void {
    const motion = this._expressions.get(expressionId);
    if (!motion) return;

    this._expressionManager.startMotionPriority(motion, false, 3);
  }

  /**
   * 取得模型矩陣
   */
  public getModelMatrix(): CubismModelMatrix {
    return this._modelMatrix;
  }

  /**
   * 取得模型配置
   */
  public getModelConfig(): ModelConfig | null {
    return this._modelConfig;
  }

  /**
   * 設定 Canvas 尺寸
   */
  public setCanvasSize(width: number, height: number): void {
    this._canvasWidth = width;
    this._canvasHeight = height;
  }

  /**
   * 取得 Canvas 寬度
   */
  public getCanvasWidth(): number {
    return this._canvasWidth;
  }

  /**
   * 取得 Canvas 高度
   */
  public getCanvasHeight(): number {
    return this._canvasHeight;
  }

  /**
   * 釋放資源
   */
  public release(): void {
    this._textureManager.release();
    this._expressions.clear();
    this._motions.clear();

    super.release();
  }
}
