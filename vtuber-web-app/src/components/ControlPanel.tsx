/**
 * 控制面板元件
 * 提供麥克風開關、模型切換、動作控制和其他功能按鈕
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useAppStore } from '@store/appStore';
import { useBackgroundStore, type BackgroundType, type BackgroundFit } from '../store/backgroundStore';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import { MotionController } from '../live2d/MotionController';
import { getModelConfig, Priority } from '../live2d/LAppDefine';
import { LipSyncManager } from '../audio/LipSyncManager';
import './ControlPanel.css';

export const ControlPanel = () => {
  const {
    microphoneEnabled,
    microphonePermission,
    eyeTrackingEnabled,
    autoPlayEnabled,
    modelLoaded,
    modelLoading,
    modelError,
    modelSwitching,
    currentModelName,
    availableModels,
    modelScale,
    hitAreaDebug,
    toggleMicrophone,
    toggleEyeTracking,
    toggleAutoPlay,
    toggleHitAreaDebug,
    scaleModelUp,
    scaleModelDown,
    resetModelTransform,
    showControls,
    setCurrentModelName,
    setModelSwitching,
    setModelLoading,
    setModelLoaded,
    setModelError,
    setMicrophonePermission,
  } = useAppStore();

  // 動作測試狀態
  const [motionGroups, setMotionGroups] = useState<string[]>([]);
  const [selectedMotionGroup, setSelectedMotionGroup] = useState<string>('Idle');
  const [lipSyncVolume, setLipSyncVolume] = useState<number>(0);

  // 背景設定狀態
  const {
    backgroundType,
    backgroundColor,
    backgroundImageUrl,
    backgroundImageFit,
    outputWidth,
    outputHeight,
    setBackgroundType,
    setBackgroundColor,
    setBackgroundImageUrl,
    setBackgroundImageFit,
    setOutputResolution,
  } = useBackgroundStore();

  // 輸出分辨率：自訂輸入暫存
  const [customW, setCustomW] = useState(String(outputWidth));
  const [customH, setCustomH] = useState(String(outputHeight));
  // 判斷目前是否為自訂（非常用預設值）
  const PRESETS = [
    { label: '1920×1080 (Full HD)', w: 1920, h: 1080 },
    { label: '1280×720 (HD)', w: 1280, h: 720 },
    { label: '2560×1440 (2K)', w: 2560, h: 1440 },
  ] as const;
  const isCustomResolution = !PRESETS.some((p) => p.w === outputWidth && p.h === outputHeight);

  const handleApplyCustomResolution = useCallback(() => {
    const w = parseInt(customW, 10);
    const h = parseInt(customH, 10);
    if (w > 0 && h > 0) {
      setOutputResolution(w, h);
    }
  }, [customW, customH, setOutputResolution]);

  // 本地圖片上傳狀態（若 store 中已存有 base64 data URL，顯示 [本地圖片] 而非原始字串）
  const [imageUrlInput, setImageUrlInput] = useState(
    backgroundImageUrl.startsWith('data:') ? '[本地圖片]' : backgroundImageUrl
  );
  const fileInputRef = useRef<HTMLInputElement>(null);

  // LipSync 更新循環
  const lipSyncLoopRef = useRef<number | null>(null);

  // 初始化動作群組
  useEffect(() => {
    if (modelLoaded) {
      const manager = LAppLive2DManager.getInstance();
      const model = manager.getActiveModel();
      if (model) {
        const groups = model.getMotionGroupNames();
        setMotionGroups(groups);
        if (groups.length > 0 && !groups.includes(selectedMotionGroup)) {
          setSelectedMotionGroup(groups[0]);
        }
      }
    }
  }, [modelLoaded, currentModelName]);

  // 自動播放控制
  useEffect(() => {
    const motionController = MotionController.getInstance();

    if (autoPlayEnabled && modelLoaded) {
      motionController.startAutoPlay();
    } else {
      motionController.stopAutoPlay();
    }

    // 清理函數
    return () => {
      motionController.stopAutoPlay();
    };
  }, [autoPlayEnabled, modelLoaded]);

  // 視線追蹤控制
  useEffect(() => {
    if (!modelLoaded) return;

    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      model.setEyeTrackingEnabled(eyeTrackingEnabled);
    }
  }, [eyeTrackingEnabled, modelLoaded]);

  // LipSync 循環
  useEffect(() => {
    const lipSyncManager = LipSyncManager.getInstance();

    const updateLipSync = () => {
      if (microphoneEnabled) {
        lipSyncManager.update();
        setLipSyncVolume(lipSyncManager.getCurrentMouthValue());
      }
      lipSyncLoopRef.current = requestAnimationFrame(updateLipSync);
    };

    if (microphoneEnabled) {
      lipSyncLoopRef.current = requestAnimationFrame(updateLipSync);
    }

    return () => {
      if (lipSyncLoopRef.current) {
        cancelAnimationFrame(lipSyncLoopRef.current);
      }
    };
  }, [microphoneEnabled]);

  // 麥克風開關處理
  const handleMicrophoneToggle = useCallback(async () => {
    const lipSyncManager = LipSyncManager.getInstance();

    if (!microphoneEnabled) {
      // 嘗試啟用麥克風
      try {
        const success = await lipSyncManager.enable();
        if (success) {
          setMicrophonePermission('granted');
          toggleMicrophone();
        } else {
          setMicrophonePermission('denied');
        }
      } catch (error) {
        console.error('麥克風啟用失敗:', error);
        setMicrophonePermission('denied');
      }
    } else {
      // 禁用麥克風
      lipSyncManager.disable();
      toggleMicrophone();

      // 重置模型的 LipSync 值
      const manager = LAppLive2DManager.getInstance();
      const model = manager.getActiveModel();
      if (model) {
        model.setLipSyncValue(0);
      }
    }
  }, [microphoneEnabled, toggleMicrophone, setMicrophonePermission]);

  // 模型切換處理函數
  const handleModelSwitch = useCallback(async (modelName: string) => {
    if (modelName === currentModelName || modelSwitching || modelLoading) {
      return;
    }

    const modelConfig = getModelConfig(modelName);
    if (!modelConfig) {
      setModelError(`找不到模型配置：${modelName}`);
      return;
    }

    try {
      setModelSwitching(true);
      setModelLoading(true);
      setModelLoaded(false);
      setModelError(null);

      const manager = LAppLive2DManager.getInstance();

      // 載入新模型（會自動設為活動模型）
      await manager.loadModel(modelConfig, true);

      setCurrentModelName(modelName);
      setModelLoaded(true);
      console.log(`模型切換成功：${modelConfig.displayName}`);

    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : '模型切換失敗';
      setModelError(errorMessage);
      console.error('模型切換失敗:', error);
    } finally {
      setModelSwitching(false);
      setModelLoading(false);
    }
  }, [currentModelName, modelSwitching, modelLoading, setCurrentModelName, setModelSwitching, setModelLoading, setModelLoaded, setModelError]);

  // 播放指定動作
  const handlePlayMotion = useCallback((index: number) => {
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      model.startMotion(selectedMotionGroup, index, Priority.Force);
      console.log(`播放動作: ${selectedMotionGroup}[${index}]`);
    }
  }, [selectedMotionGroup]);

  // 播放隨機動作
  const handlePlayRandomMotion = useCallback(() => {
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      model.startRandomMotion(selectedMotionGroup, Priority.Force);
      console.log(`播放隨機動作: ${selectedMotionGroup}`);
    }
  }, [selectedMotionGroup]);

  // 背景圖片：本地上傳 → 轉 data URL → 存入 store
  const handleFileUpload = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const url = ev.target?.result as string;
      setBackgroundImageUrl(url);
      setImageUrlInput('[本地圖片]');
      setBackgroundType('image');
    };
    reader.readAsDataURL(file);
  }, [setBackgroundImageUrl, setBackgroundType]);

  // 背景圖片：套用 URL 輸入
  const handleApplyImageUrl = useCallback(() => {
    if (imageUrlInput.trim() && imageUrlInput !== '[本地圖片]') {
      setBackgroundImageUrl(imageUrlInput.trim());
      setBackgroundType('image');
    }
  }, [imageUrlInput, setBackgroundImageUrl, setBackgroundType]);

  if (!showControls) return null;

  // 獲取當前模型的顯示名稱
  const currentModelConfig = availableModels.find(m => m.name === currentModelName);

  // 獲取當前選擇群組的動作數量
  const manager = LAppLive2DManager.getInstance();
  const model = manager.getActiveModel();
  const motionCount = model ? model.getMotionCount(selectedMotionGroup) : 0;

  return (
    <div className="control-panel">
      <div className="control-panel__header">
        <h3>虛擬主播控制台</h3>
      </div>

      <div className="control-panel__content">
        {/* 模型選擇區域 */}
        <div className="model-section">
          <h4>角色模型</h4>
          <div className="model-selector">
            <select
              value={currentModelName}
              onChange={(e) => handleModelSwitch(e.target.value)}
              disabled={modelSwitching || modelLoading}
              className="model-select"
            >
              {availableModels.map((model) => (
                <option key={model.name} value={model.name}>
                  {model.displayName}
                </option>
              ))}
            </select>
            {modelSwitching && (
              <span className="model-loading-indicator">切換中...</span>
            )}
          </div>
          {currentModelConfig?.description && (
            <p className="model-description">{currentModelConfig.description}</p>
          )}
        </div>

        {/* 模型狀態 */}
        <div className="status-section">
          <h4>模型狀態</h4>
          {modelLoading && (
            <div className="status-item loading">
              <span className="status-dot"></span>
              <span>模型載入中...</span>
            </div>
          )}
          {modelLoaded && !modelLoading && (
            <div className="status-item success">
              <span className="status-dot"></span>
              <span>模型已就緒</span>
            </div>
          )}
          {modelError && (
            <div className="status-item error">
              <span className="status-dot"></span>
              <span>{modelError}</span>
            </div>
          )}
        </div>

        {/* 功能控制 */}
        <div className="controls-section">
          <h4>功能控制</h4>

          {/* 麥克風開關 */}
          <div className="feature-item">
            <label className="feature-label">
              <span className="icon">🎤</span>
              <span>麥克風嘴型同步</span>
            </label>
            <button
              onClick={handleMicrophoneToggle}
              className={`toggle-button ${microphoneEnabled ? 'active' : ''}`}
              disabled={!modelLoaded || microphonePermission === 'denied'}
            >
              <div className="toggle-slider" />
            </button>
          </div>
          <div className="control-item">
            {microphoneEnabled && (
              <div className="volume-indicator">
                <span>音量：</span>
                <div className="volume-bar">
                  <div
                    className="volume-fill"
                    style={{ width: `${lipSyncVolume * 100}%` }}
                  />
                </div>
              </div>
            )}
            {microphonePermission === 'denied' && (
              <div className="permission-warning">
                麥克風權限已被拒絕，請在瀏覽器設定中允許
              </div>
            )}
          </div>

          {/* 視線追蹤開關 */}
          <div className="feature-item">
            <label className="feature-label">
              <span className="icon">👁️</span>
              <span>視線追蹤滑鼠</span>
            </label>
            <button
              onClick={toggleEyeTracking}
              className={`toggle-button ${eyeTrackingEnabled ? 'active' : ''}`}
              disabled={!modelLoaded}
            >
              <div className="toggle-slider" />
            </button>
          </div>

          {/* 自動播放開關 */}
          <div className="feature-item">
            <label className="feature-label">
              <span className="icon">🎬</span>
              <span>自動播放動作</span>
            </label>
            <button
              onClick={toggleAutoPlay}
              className={`toggle-button ${autoPlayEnabled ? 'active' : ''}`}
              disabled={!modelLoaded}
            >
              <div className="toggle-slider" />
            </button>
          </div>
        </div>

        {/* 模型調整控制 */}
        <div className="transform-section">
          <h4>🔍 模型調整</h4>

          {/* Hit Area 調試開關 */}
          <div className="feature-item">
            <label className="feature-label">
              <span className="icon">🎯</span>
              <span>點擊區域顯示</span>
            </label>
            <button
              className={`toggle-button ${hitAreaDebug ? 'active' : ''}`}
              onClick={toggleHitAreaDebug}
              disabled={!modelLoaded}
            >
              <div className="toggle-slider" />
            </button>
          </div>

          {/* 提示信息 */}
          <div className="control-item">
            <div className="info-text">
              💡 直接按住模型頭部即可拖動位置
            </div>
          </div>

          {/* 縮放控制 */}
          <div className="control-item scale-control">
            <label>
              📏 模型縮放
              <span className="scale-value">{Math.round(modelScale * 100)}%</span>
            </label>
            <div className="scale-controls">
              <button
                className="scale-button minus"
                onClick={scaleModelDown}
                disabled={!modelLoaded || modelScale <= 0.5}
                title="縮小 (0.1x)"
              >
                <span>−</span>
              </button>
              <button
                className="scale-button reset"
                onClick={resetModelTransform}
                disabled={!modelLoaded}
                title="重置位置與縮放"
              >
                <span>🔄</span>
              </button>
              <button
                className="scale-button plus"
                onClick={scaleModelUp}
                disabled={!modelLoaded || modelScale >= 2.0}
                title="放大 (0.1x)"
              >
                <span>+</span>
              </button>
            </div>
          </div>
        </div>

        {/* 背景設定區域 */}
        <div className="background-section">
          <h4>OBS 背景設定</h4>

          {/* OBS 提示 */}
          <div className="obs-hint">
            <span className="obs-hint__label">OBS Browser Source</span>
            <code className="obs-hint__url">
              {window.location.origin}/display
            </code>
            <button
              className="obs-hint__copy"
              onClick={() => navigator.clipboard?.writeText(`${window.location.origin}/display`)}
              title="複製網址"
            >
              複製
            </button>
          </div>

          {/* 輸出分辨率選擇 */}
          <div className="bg-resolution-section">
            <label className="bg-label">輸出分辨率</label>
            <div className="bg-resolution-presets">
              {PRESETS.map((p) => (
                <button
                  key={`${p.w}x${p.h}`}
                  className={`bg-res-btn ${outputWidth === p.w && outputHeight === p.h ? 'active' : ''}`}
                  onClick={() => {
                    setOutputResolution(p.w, p.h);
                    setCustomW(String(p.w));
                    setCustomH(String(p.h));
                  }}
                >
                  {p.label}
                </button>
              ))}
              <button
                className={`bg-res-btn ${isCustomResolution ? 'active' : ''}`}
                onClick={() => {
                  // 點「自訂」時不立即套用，讓使用者輸入後按確認
                }}
              >
                自訂
              </button>
            </div>
            {/* 自訂輸入（永遠顯示，方便調整；active 狀態時高亮） */}
            <div className="bg-resolution-custom">
              <input
                type="number"
                className="bg-res-input"
                value={customW}
                min={320}
                max={7680}
                onChange={(e) => setCustomW(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleApplyCustomResolution()}
                placeholder="寬"
              />
              <span className="bg-res-sep">×</span>
              <input
                type="number"
                className="bg-res-input"
                value={customH}
                min={240}
                max={4320}
                onChange={(e) => setCustomH(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleApplyCustomResolution()}
                placeholder="高"
              />
              <button className="bg-apply-btn" onClick={handleApplyCustomResolution}>
                套用
              </button>
            </div>
            <div className="info-text">
              目前：{outputWidth} × {outputHeight} px
            </div>
          </div>

          {/* 背景類型選擇 */}
          <div className="bg-type-selector">
            {(['transparent', 'color', 'image'] as BackgroundType[]).map((t) => (
              <button
                key={t}
                className={`bg-type-btn ${backgroundType === t ? 'active' : ''}`}
                onClick={() => setBackgroundType(t)}
              >
                {t === 'transparent' ? '透明' : t === 'color' ? '純色' : '圖片'}
              </button>
            ))}
          </div>

          {/* 純色設定 */}
          {backgroundType === 'color' && (
            <div className="bg-color-row">
              <label className="bg-label">背景顏色</label>
              <input
                type="color"
                value={backgroundColor}
                onChange={(e) => setBackgroundColor(e.target.value)}
                className="bg-color-picker"
              />
              <span className="bg-color-value">{backgroundColor}</span>
              <button
                className="bg-preset-btn"
                onClick={() => setBackgroundColor('#00b140')}
                title="綠幕 (Chroma Key)"
              >
                綠幕
              </button>
              <button
                className="bg-preset-btn"
                onClick={() => setBackgroundColor('#0047ab')}
                title="藍幕"
              >
                藍幕
              </button>
            </div>
          )}

          {/* 圖片設定 */}
          {backgroundType === 'image' && (
            <div className="bg-image-settings">
              <div className="bg-url-row">
                <input
                  type="text"
                  className="bg-url-input"
                  placeholder="圖片網址 (http://...)"
                  value={imageUrlInput === '[本地圖片]' ? '' : imageUrlInput}
                  onChange={(e) => setImageUrlInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleApplyImageUrl()}
                />
                <button className="bg-apply-btn" onClick={handleApplyImageUrl}>套用</button>
              </div>
              <div className="bg-url-row">
                <button
                  className="bg-upload-btn"
                  onClick={() => fileInputRef.current?.click()}
                >
                  上傳本地圖片
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  style={{ display: 'none' }}
                  onChange={handleFileUpload}
                />
                {backgroundImageUrl && (
                  <span className="bg-image-status">已設定</span>
                )}
              </div>
              <div className="bg-fit-row">
                <label className="bg-label">填充方式</label>
                {(['cover', 'contain', 'fill'] as BackgroundFit[]).map((f) => (
                  <button
                    key={f}
                    className={`bg-fit-btn ${backgroundImageFit === f ? 'active' : ''}`}
                    onClick={() => setBackgroundImageFit(f)}
                  >
                    {f === 'cover' ? '填滿' : f === 'contain' ? '包含' : '拉伸'}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* 動作測試區域 */}
        <div className="motion-section">
          <h4>動作測試</h4>
          <div className="motion-controls">
            <select
              value={selectedMotionGroup}
              onChange={(e) => setSelectedMotionGroup(e.target.value)}
              disabled={!modelLoaded || motionGroups.length === 0}
              className="motion-group-select"
            >
              {motionGroups.map((group) => (
                <option key={group} value={group}>{group}</option>
              ))}
            </select>
            <button
              onClick={handlePlayRandomMotion}
              className="motion-button"
              disabled={!modelLoaded || motionCount === 0}
              title="播放隨機動作"
            >
              🎲 隨機
            </button>
          </div>
          {motionCount > 0 && (
            <div className="motion-list">
              {Array.from({ length: motionCount }, (_, i) => (
                <button
                  key={i}
                  onClick={() => handlePlayMotion(i)}
                  className="motion-item-button"
                  disabled={!modelLoaded}
                >
                  動作 {i + 1}
                </button>
              ))}
            </div>
          )}
          <p className="motion-hint">
            💡 建議關閉自動播放以測試特定動作
          </p>
        </div>

      </div>

      {/* 開發資訊 */}
      <div className="control-panel__footer">
        <small>Live2D Cubism SDK 5-r.5-beta.3</small>
      </div>
    </div>
  );
};
