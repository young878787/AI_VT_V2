/**
 * Hit Area 覆蓋層元件
 * 視覺化顯示模型的點擊區域，用於調試和使用者指引
 */
import { useEffect, useRef, useState } from 'react';
import { useAppStore } from '@store/appStore';
import { LAppDelegate } from '../live2d/LAppDelegate';
import './HitAreaOverlay.css';

interface HitArea {
  name: string;
  points: [number, number][];  // 屏幕座標的多邊形點
  isHit: boolean;              // 當前是否被滑鼠懸停
}

export const HitAreaOverlay = () => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { hitAreaDebug } = useAppStore();
  const [hitAreas, setHitAreas] = useState<HitArea[]>([]);
  // useRef 不觸發 re-render，避免每次滑鼠移動都重建 setInterval
  const mousePosRef = useRef<[number, number]>([0, 0]);

  // 更新 Hit Areas 位置
  useEffect(() => {
    if (!hitAreaDebug) return;

    const updateHitAreas = () => {
      const delegate = LAppDelegate.getInstance();
      const model = delegate.getActiveModel();
      const canvas = delegate.getCanvas();

      if (!model || !canvas) return;

      const rect = canvas.getBoundingClientRect();
      const areas: HitArea[] = [];

      // 獲取模型核心物件
      const coreModel = model.getModel();
      if (!coreModel) return;

      // 不再依賴 model3.json 設定的 HitAreas（因為有些模型設定不良）
      // 我們動態計算整個模型所有 Drawable 網格的總邊界框，將整個模型當作一個統一的 'Body' 點擊區域
      const drawableCount = coreModel.getDrawableCount();
      let globalMinX = Infinity, globalMaxX = -Infinity;
      let globalMinY = Infinity, globalMaxY = -Infinity;
      let hasValidVertices = false;

      for (let i = 0; i < drawableCount; i++) {
        const vertexCount = coreModel.getDrawableVertexCount(i);
        if (vertexCount === 0) continue;
        // 過濾掉不透明度為 0 的部件或隱藏的部件（可選，但為了確保完整我們直接計算全部有效網格）
        // 若要更精確可檢查 coreModel.getDrawableOpacity(i) > 0，但通常全拿也可以
        if (coreModel.getDrawableOpacity(i) <= 0.01) continue; 

        const vertexArray = coreModel.getDrawableVertices(i);
        for (let v = 0; v < vertexCount; v++) {
          const vx = vertexArray[v * 2];
          const vy = vertexArray[v * 2 + 1];
          globalMinX = Math.min(globalMinX, vx);
          globalMaxX = Math.max(globalMaxX, vx);
          globalMinY = Math.min(globalMinY, vy);
          globalMaxY = Math.max(globalMaxY, vy);
          hasValidVertices = true;
        }
      }

      if (!hasValidVertices) return;

      // 取得完整的 MVP 變換矩陣將模型座標轉換為屏幕座標
      const mvp = delegate.createMVPMatrix();
      if (!mvp) return;

      const points: [number, number][] = [
        [globalMinX, globalMinY],  // 左下
        [globalMaxX, globalMinY],  // 右下
        [globalMaxX, globalMaxY],  // 右上
        [globalMinX, globalMaxY],  // 左上
      ].map(([mx, my]) => {
        // 應用 MVP 變換轉換為剪裁空間 (-1 到 1)
        const clipX = mvp.transformX(mx);
        const clipY = mvp.transformY(my);

        // 剪裁空間轉換為屏幕座標 (注意 WebGL Y 軸朝上)
        const screenX = rect.left + ((clipX + 1.0) / 2.0) * rect.width;
        const screenY = rect.top + ((1.0 - clipY) / 2.0) * rect.height;

        return [screenX, screenY];
      });

      // 檢查滑鼠是否在範圍內
      let isHit = false;
      try {
        const [mouseX, mouseY] = mousePosRef.current;
        const clipMouseX = ((mouseX - rect.left) / rect.width) * 2.0 - 1.0;
        const clipMouseY = -(((mouseY - rect.top) / rect.height) * 2.0 - 1.0);
        const projView = delegate.createProjViewMatrix();
        if (projView) {
          const inverseProjView = projView.getInvert();
          const viewMouseX = inverseProjView.transformX(clipMouseX);
          const viewMouseY = inverseProjView.transformY(clipMouseY);

          // 使用 'Body' 參數呼叫 hitTest，我們稍後會修改 LAppModel 讓它泛指全模型
          isHit = model.hitTest(viewMouseX, viewMouseY, 'Body');
        }
      } catch (e) {
        // 忽略錯誤
      }

      areas.push({
        name: 'Body',
        points,
        isHit,
      });

      setHitAreas(areas);
    };

    // interval 只依賴 hitAreaDebug，不再因 mousePos 重建
    const interval = setInterval(updateHitAreas, 100);
    updateHitAreas();

    return () => clearInterval(interval);
  }, [hitAreaDebug]);

  // 繪製 Hit Areas
  useEffect(() => {
    if (!hitAreaDebug || !canvasRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // 設置 Canvas 尺寸
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;

    // 清空畫布
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // 繪製每個 Hit Area
    hitAreas.forEach(area => {
      if (area.points.length < 3) return;

      ctx.beginPath();
      ctx.moveTo(area.points[0][0], area.points[0][1]);

      for (let i = 1; i < area.points.length; i++) {
        ctx.lineTo(area.points[i][0], area.points[i][1]);
      }

      ctx.closePath();

      // 填充顏色（滑鼠懸停時高亮）
      if (area.isHit) {
        ctx.fillStyle = 'rgba(255, 107, 157, 0.4)';  // 粉色高亮
        ctx.strokeStyle = '#FF6B9D';
        ctx.lineWidth = 3;
      } else {
        ctx.fillStyle = 'rgba(135, 206, 250, 0.2)';  // 淺藍色
        ctx.strokeStyle = '#87CEFA';
        ctx.lineWidth = 2;
      }

      ctx.fill();
      ctx.stroke();

      // 繪製標籤
      const centerX = area.points.reduce((sum, p) => sum + p[0], 0) / area.points.length;
      const centerY = area.points.reduce((sum, p) => sum + p[1], 0) / area.points.length;

      ctx.font = 'bold 16px "Noto Sans JP", sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      // 文字陰影
      ctx.shadowColor = 'rgba(0, 0, 0, 0.8)';
      ctx.shadowBlur = 4;
      ctx.shadowOffsetX = 2;
      ctx.shadowOffsetY = 2;

      ctx.fillStyle = area.isHit ? '#FF6B9D' : '#FFF';
      ctx.fillText(area.name, centerX, centerY);

      // 重置陰影
      ctx.shadowColor = 'transparent';
    });
  }, [hitAreas, hitAreaDebug]);

  // 追蹤滑鼠位置（使用 ref 避免 re-render）
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      mousePosRef.current = [e.clientX, e.clientY];
    };

    window.addEventListener('mousemove', handleMouseMove);
    return () => window.removeEventListener('mousemove', handleMouseMove);
  }, []);

  if (!hitAreaDebug) return null;

  return (
    <div className="hit-area-overlay">
      <canvas ref={canvasRef} className="hit-area-canvas" />
      <div className="hit-area-info">
        <h3>🎯 Hit Area 調試模式</h3>
        <p>藍色區域 = 可點擊區域</p>
        <p>粉色高亮 = 滑鼠懸停位置</p>
        <p>點擊模型任何有效區域（如 Body）即可拖動</p>
        <div className="hit-area-list">
          <strong>檢測到的區域：</strong>
          <ul>
            {hitAreas.map((area, idx) => (
              <li key={idx} className={area.isHit ? 'active' : ''}>
                {area.name} {area.isHit && '← 滑鼠在此'}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
};
