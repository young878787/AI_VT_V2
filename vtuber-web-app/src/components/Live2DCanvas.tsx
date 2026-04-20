/**
 * Live2D 畫布元件
 * 負責顯示 Live2D 模型和處理渲染
 * 加入背景即時預覽：在主頁即可看到 OBS 輸出的背景效果
 */
import { useEffect, useRef, useCallback, CSSProperties } from 'react';
import { useAppStore } from '@store/appStore';
import { useBackgroundStore } from '../store/backgroundStore';
import { LAppDelegate } from '../live2d/LAppDelegate';
import { canvasSyncService } from '../services/canvasSyncService';
import './Live2DCanvas.css';

export const Live2DCanvas = () => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const delegateRef = useRef<LAppDelegate | null>(null);
  const {
    setModelLoading,
    setModelLoaded,
    setModelError,
    eyeTrackingEnabled,
    modelScale,
    setModelScale,
  } = useAppStore();

  // 背景即時預覽
  const {
    backgroundType,
    backgroundColor,
    backgroundImageUrl,
    backgroundImageFit,
  } = useBackgroundStore();

  // 初始化 Live2D
  useEffect(() => {
    if (!canvasRef.current) return;

    const canvas = canvasRef.current;
    setModelLoading(true);

    // 等待 Core 腳本載入
    const initializeLive2D = async () => {
      try {
        console.log('開始初始化 Live2D...');
        
        // 檢查 Core 是否已載入
        if (typeof (window as any).Live2DCubismCore === 'undefined') {
          throw new Error('Live2DCubismCore 尚未載入，請檢查 index.html');
        }
        
        // 檢查 Core 的關鍵 API
        const core = (window as any).Live2DCubismCore;
        if (!core.Moc || !core.Model || !core.Version) {
          throw new Error('Live2DCubismCore API 不完整');
        }
        
        console.log('✓ Live2DCubismCore 已載入:', {
          version: core.Version?.versionNumber || 'Unknown',
          hasMoc: !!core.Moc,
          hasModel: !!core.Model
        });

        // 取得 LAppDelegate 實例
        const delegate = LAppDelegate.getInstance();
        if (!delegate) {
          throw new Error('LAppDelegate 實例創建失敗');
        }
        delegateRef.current = delegate;

        console.log('✓ LAppDelegate 實例已創建');

        // 初始化應用程式
        const success = await delegate.initialize(canvas);
        
        if (!success) {
          throw new Error('LAppDelegate.initialize() 返回 false');
        }

        console.log('✓ LAppDelegate 初始化成功');

        // 啟動渲染循環
        delegate.run();
        console.log('✓ 渲染循環已啟動');

        // Hook 進渲染迴圈：每幀 render() 後立即擷取畫面廣播給 /display 頁面
        // 相較於獨立 rAF，消除 0–1 幀延遲，確保顯示端與主頁面像素完全同步
        canvasSyncService.attachToRenderLoop(delegate);
        console.log('✓ Canvas 幀廣播已啟動（render loop hook）');

        setModelLoaded(true);
        setModelLoading(false);
        console.log('========================================');
        console.log('✓✓✓ Live2D 完整初始化成功 ✓✓✓');
        console.log('========================================');

      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : '未知錯誤';
        const errorDetails = error instanceof Error ? error.stack : '';
        
        setModelError(`Live2D 初始化失敗：${errorMessage}`);
        setModelLoading(false);
        
        console.error('========================================');
        console.error('Live2D 初始化錯誤：', error);
        if (errorDetails) {
          console.error('錯誤堆疊：', errorDetails);
        }
        console.error('========================================');
      }
    };

    // 延遲初始化，確保 Core 腳本已載入（增加延遲到 500ms）
    const timer = setTimeout(initializeLive2D, 500);

    // 清理函數
    return () => {
      clearTimeout(timer);
      canvasSyncService.stopBroadcasting();
      if (delegateRef.current) {
        delegateRef.current.stop();
        LAppDelegate.releaseInstance();
        delegateRef.current = null;
        console.log('Live2D 資源已清理');
      }
    };
  }, [setModelLoading, setModelLoaded, setModelError]);

  // 處理 Canvas 尺寸變化
  useEffect(() => {
    const handleResize = () => {
      if (delegateRef.current) {
        delegateRef.current.resizeCanvas();
      }
    };

    handleResize();
    window.addEventListener('resize', handleResize);
    
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, []);

  // 處理滑鼠移動（視線追蹤和拖移）
  useEffect(() => {
    const handleMouseMove = (event: MouseEvent) => {
      if (!delegateRef.current) return;
      
      // 始終更新拖移（如果正在拖移中）
      delegateRef.current.updateModelDrag(event.clientX, event.clientY);
      
      // 視線追蹤（如果啟用且不在拖移中）
      if (eyeTrackingEnabled) {
        delegateRef.current.onMouseMoved(event.clientX, event.clientY);
      }
    };

    const handleMouseUp = () => {
      if (delegateRef.current) {
        delegateRef.current.endModelDrag();
      }
    };

    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);

    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [eyeTrackingEnabled]);

  // 處理滑鼠按下（檢測是否點擊在模型上）
  const handleMouseDown = useCallback((event: React.MouseEvent<HTMLCanvasElement>) => {
    if (delegateRef.current) {
      // 使用 onTapped 來檢測點擊區域
      delegateRef.current.onTapped(event.clientX, event.clientY);
    }
  }, []);

  // 處理點擊事件（雙擊等其他互動）
  const handleCanvasClick = useCallback((event: React.MouseEvent<HTMLCanvasElement>) => {
    // 預留給其他互動使用
  }, []);

  // 處理滾輪事件（縮放模型）
  const handleWheel = useCallback((event: React.WheelEvent<HTMLCanvasElement>) => {
    // 阻止預設的網頁滾動
    // event.preventDefault(); // Note: cannot call on passive event in react synthetic event sometimes, but best effort

    // 判斷滾動方向，上滾放大，下滾縮小 (deltaY < 0 為向上滾)
    const factor = event.deltaY < 0 ? 1.1 : 0.9;
    
    // 計算新的縮放值，並限制範圍 (在 appStore 的 setModelScale 內已經有 clamping，但我們基於當前 scale 計算)
    const newScale = modelScale * factor;
    
    setModelScale(newScale);
  }, [modelScale, setModelScale]);

  // 背景即時預覽樣式 — 與 DisplayPage 共用相同邏輯
  const bgStyle: CSSProperties = (() => {
    if (backgroundType === 'color') {
      return { backgroundColor };
    }
    if (backgroundType === 'image' && backgroundImageUrl) {
      return {
        backgroundImage: `url("${backgroundImageUrl}")`,
        backgroundSize: backgroundImageFit === 'fill' ? '100% 100%' : backgroundImageFit,
        backgroundPosition: 'center',
        backgroundRepeat: 'no-repeat',
      };
    }
    // transparent — 保持容器預設的透明/白色
    return {};
  })();

  return (
    <div className="live2d-canvas-container" style={bgStyle}>
      {/* 背景類型標籤 */}
      <div className="live2d-bg-badge">
        {backgroundType === 'transparent' ? '🔲 透明背景' :
         backgroundType === 'color' ? `🎨 ${backgroundColor}` :
         '🖼️ 圖片背景'}
      </div>
      <canvas 
        ref={canvasRef}
        className="live2d-canvas"
        id="live2d-canvas"
        onMouseDown={handleMouseDown}
        onClick={handleCanvasClick}
        onWheel={handleWheel}
        style={{ cursor: 'pointer' }}
      />
    </div>
  );
};
