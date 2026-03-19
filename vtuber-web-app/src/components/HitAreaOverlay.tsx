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
  const [mousePos, setMousePos] = useState<[number, number]>([0, 0]);

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

      // 獲取模型的所有 hit areas
      const modelSetting = (model as any)._modelSetting;
      if (!modelSetting) return;

      const hitAreaCount = modelSetting.getHitAreasCount();

      // 處理所有配置的 hit areas
      const processedAreas: string[] = [];

      for (let i = 0; i < hitAreaCount; i++) {
        const name = modelSetting.getHitAreaName(i);
        const drawableId = modelSetting.getHitAreaId(i);

        if (!name || !drawableId || processedAreas.includes(name)) continue;
        processedAreas.push(name);

        // 從模型獲取 drawable 的實際頂點數據
        const coreModel = model.getModel();
        const drawableIndex = coreModel.getDrawableIndex(drawableId);

        if (drawableIndex < 0) {
          console.warn(`[Hit Area Overlay] Drawable ID "${drawableId}" not found`);
          continue;
        }

        // 獲取 drawable 的頂點數據來計算邊界框
        const vertexCount = coreModel.getDrawableVertexCount(drawableIndex);
        const vertexArray = coreModel.getDrawableVertices(drawableIndex);

        if (vertexCount === 0 || !vertexArray) {
          console.warn(`[Hit Area Overlay] No vertices for drawable "${drawableId}"`);
          continue;
        }

        // 計算頂點的邊界框（在模型座標系中）
        let minX = Infinity, maxX = -Infinity;
        let minY = Infinity, maxY = -Infinity;

        for (let v = 0; v < vertexCount; v++) {
          const x = vertexArray[v * 2];
          const y = vertexArray[v * 2 + 1];
          minX = Math.min(minX, x);
          maxX = Math.max(maxX, x);
          minY = Math.min(minY, y);
          maxY = Math.max(maxY, y);
        }

        // 將模型座標的邊界框轉換為屏幕座標
        // 取得完整的 MVP 變換矩陣
        const mvp = delegate.createMVPMatrix();
        if (!mvp) continue;

        const points: [number, number][] = [
          [minX, minY],  // 左下
          [maxX, minY],  // 右下
          [maxX, maxY],  // 右上
          [minX, maxY],  // 左上
        ].map(([mx, my]) => {
          // 應用 MVP 變換轉換為剪裁空間 (-1 到 1)
          const clipX = mvp.transformX(mx);
          const clipY = mvp.transformY(my);

          // 剪裁空間轉換為屏幕座標 (注意 WebGL Y 軸朝上)
          const screenX = rect.left + ((clipX + 1.0) / 2.0) * rect.width;
          const screenY = rect.top + ((1.0 - clipY) / 2.0) * rect.height;

          return [screenX, screenY];
        });

        // 檢查滑鼠是否在區域內
        let isHit = false;
        try {
          // 將滑鼠螢幕座標轉為 Clip 空間，然後只取反 Projection * View
          // (不能取反 Model，因為 hitTest 內建會處理 ModelMatrix 取反)
          const clipMouseX = ((mousePos[0] - rect.left) / rect.width) * 2.0 - 1.0;
          const clipMouseY = -(((mousePos[1] - rect.top) / rect.height) * 2.0 - 1.0);
          const projView = delegate.createProjViewMatrix();
          if (projView) {
            const inverseProjView = projView.getInvert();
            const viewMouseX = inverseProjView.transformX(clipMouseX);
            const viewMouseY = inverseProjView.transformY(clipMouseY);

            // 使用模型的 hitTest 方法，傳入區域名稱
            isHit = model.hitTest(viewMouseX, viewMouseY, name);
          }
        } catch (e) {
          // 忽略錯誤，繼續繪製
        }

        areas.push({
          name,
          points,
          isHit,
        });
      }

      setHitAreas(areas);
    };

    // 定期更新（模型可能在移動）
    const interval = setInterval(updateHitAreas, 100);
    updateHitAreas();

    return () => clearInterval(interval);
  }, [hitAreaDebug, mousePos]);

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

  // 追蹤滑鼠位置
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      setMousePos([e.clientX, e.clientY]);
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
