/**
 * NativeParamPanel — 原生 Live2D 參數即時控制面板
 *
 * - 動態讀取模型所有 Cubism 參數（getParameterCount / getId / min / max / default / current）
 * - 依類別分組，多欄格線顯示
 * - 每個滑桿獨立 5 秒倒數計時器；無操作後自動釋放控制權
 * - 覆蓋優先序：NativeParam > AI 表情 > 眼動追蹤（由 LAppModel.update() 保證）
 */
import React, {
  useState, useEffect, useCallback, useRef, useMemo
} from 'react';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import './NativeParamPanel.css';

// ── 已知參數 ID → 中文名稱 ──────────────────────────────────────────
const PARAM_LABELS: Record<string, string> = {
  // 頭部 / 身體
  ParamAngleX:      '頭部 X',
  ParamAngleY:      '頭部 Y',
  ParamAngleZ:      '頭部 Z',
  ParamBodyAngleX:  '身體 X',
  ParamBodyAngleY:  '身體 Y',
  ParamBodyAngleZ:  '身體 Z',
  // 眼部
  ParamEyeBallX:    '眼球 X',
  ParamEyeBallY:    '眼球 Y',
  ParamEyeLOpen:    '左眼開合',
  ParamEyeROpen:    '右眼開合',
  ParamEyeLSmile:   '左眼微笑',
  ParamEyeRSmile:   '右眼微笑',
  // 眉毛
  ParamBrowLY:      '左眉高度',
  ParamBrowRY:      '右眉高度',
  ParamBrowLAngle:  '左眉角度',
  ParamBrowRAngle:  '右眉角度',
  ParamBrowLForm:   '左眉形狀',
  ParamBrowRForm:   '右眉形狀',
  // 嘴部
  ParamMouthOpenY:  '嘴巴開合',
  ParamMouthForm:   '嘴角形狀',
  // 臉部特效
  ParamCheek:       '臉頰',
  ParamTere:        '臉紅',
  // 頭髮
  ParamHairFront:   '前髮',
  ParamHairBack:    '後髮',
  ParamHairSide:    '側髮',
  ParamHairFluffy:  '蓬鬆髮',
  // 物理 / 其他
  ParamBreath:      '呼吸',
  ParamBustX:       '胸部 X',
  ParamBustY:       '胸部 Y',
  ParamBaseX:       '基準 X',
  ParamBaseY:       '基準 Y',
  ParamArmLA:       '左臂 A',
  ParamArmRA:       '右臂 A',
  ParamArmLB:       '左臂 B',
  ParamArmRB:       '右臂 B',
  ParamHandL:       '左手',
  ParamHandR:       '右手',
};

// ── 類別判定（依 ID 前綴） ────────────────────────────────────────────
type Category = '頭部身體' | '眼部' | '眉毛' | '嘴部' | '臉部' | '頭髮' | '物理其他';

const CATEGORY_ORDER: Category[] = ['頭部身體', '眼部', '眉毛', '嘴部', '臉部', '頭髮', '物理其他'];

const CATEGORY_COLORS: Record<Category, string> = {
  '頭部身體': '#7dd3fc',
  '眼部':     '#a5b4fc',
  '眉毛':     '#fde68a',
  '嘴部':     '#86efac',
  '臉部':     '#f9a8d4',
  '頭髮':     '#fdba74',
  '物理其他': '#d1d5db',
};

function getCategory(id: string): Category {
  if (/Angle|Body/i.test(id))              return '頭部身體';
  if (/EyeBall|EyeL|EyeR|Eye/i.test(id))  return '眼部';
  if (/Brow/i.test(id))                    return '眉毛';
  if (/Mouth|Teeth/i.test(id))             return '嘴部';
  if (/Cheek|Tere/i.test(id))              return '臉部';
  if (/Hair/i.test(id))                    return '頭髮';
  return '物理其他';
}

// ── 資料型別 ──────────────────────────────────────────────────────────
interface ParamInfo {
  id: string;
  min: number;
  max: number;
  defaultVal: number;
  current: number;
}

interface OverrideEntry {
  value: number;
  expiresAt: number;   // Date.now() + 5000
}

const OVERRIDE_DURATION_MS = 5000;

// ── 元件 ──────────────────────────────────────────────────────────────
export const NativeParamPanel: React.FC = () => {
  const [params, setParams]       = useState<ParamInfo[]>([]);
  const [overrides, setOverrides] = useState<Record<string, OverrideEntry>>({});
  const [activeGroup, setActiveGroup] = useState<Category | '全部'>('全部');
  const prevParamIdsRef = useRef<string>('');

  // ── 10fps 輪詢：讀取模型參數 + 清除過期覆蓋 ──────────────────────
  useEffect(() => {
    let lastTime = 0;
    let rafId: number;

    const tick = (time: number) => {
      if (time - lastTime > 100) {
        lastTime = time;
        const model = LAppLive2DManager.getInstance().getActiveModel();
        if (model && typeof (model as any).getAllParameters === 'function') {
          const fresh: ParamInfo[] = (model as any).getAllParameters();
          setParams(fresh);

          // 偵測模型切換（參數 ID 集合改變）→ 清除舊覆蓋
          const idsKey = fresh.map(p => p.id).join(',');
          if (prevParamIdsRef.current && idsKey !== prevParamIdsRef.current) {
            setOverrides({});
            if (typeof (model as any).clearAllNativeParamOverrides === 'function') {
              (model as any).clearAllNativeParamOverrides();
            }
          }
          prevParamIdsRef.current = idsKey;
        }

        // 清除過期的 React side 覆蓋記錄（model 端由 update() 自動清除）
        const now = Date.now();
        setOverrides(prev => {
          let changed = false;
          const next = { ...prev };
          for (const id of Object.keys(next)) {
            if (now >= next[id].expiresAt) {
              delete next[id];
              changed = true;
            }
          }
          return changed ? next : prev;
        });
      }
      rafId = requestAnimationFrame(tick);
    };

    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, []);

  // ── 滑桿變更 ─────────────────────────────────────────────────────────
  const handleChange = useCallback((paramId: string, value: number) => {
    // 注入模型覆蓋
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model && typeof (model as any).setNativeParamOverride === 'function') {
      (model as any).setNativeParamOverride(paramId, value);
    }
    // 更新 React 端覆蓋（含倒數時間）
    setOverrides(prev => ({
      ...prev,
      [paramId]: { value, expiresAt: Date.now() + OVERRIDE_DURATION_MS },
    }));
  }, []);

  // ── 全部重置 ─────────────────────────────────────────────────────────
  const handleReset = useCallback(() => {
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model && typeof (model as any).clearAllNativeParamOverrides === 'function') {
      (model as any).clearAllNativeParamOverrides();
    }
    setOverrides({});
  }, []);

  // ── 分組 & 過濾 ───────────────────────────────────────────────────────
  const displayParams = useMemo(() => {
    return activeGroup === '全部'
      ? params
      : params.filter(p => getCategory(p.id) === activeGroup);
  }, [params, activeGroup]);

  // 統計各分類數量（含覆蓋標記）
  const categoryCounts = useMemo(() => {
    const counts: Partial<Record<Category | '全部', number>> = { '全部': params.length };
    for (const p of params) {
      const cat = getCategory(p.id);
      counts[cat] = (counts[cat] ?? 0) + 1;
    }
    return counts;
  }, [params]);

  // 活躍覆蓋總數（用於標題徽章）
  const activeOverrideCount = Object.keys(overrides).length;
  const now = Date.now();

  // ── 空狀態 ────────────────────────────────────────────────────────────
  if (params.length === 0) {
    return (
      <div className="native-panel native-panel--empty">
        <div className="native-panel__empty-msg">
          等待模型載入...
        </div>
      </div>
    );
  }

  // ── 渲染 ──────────────────────────────────────────────────────────────
  return (
    <div className="native-panel">
      {/* ── 標題列 ── */}
      <div className="native-panel__header">
        <div className="native-panel__header-left">
          <span className="native-panel__title">原生參數</span>
          {activeOverrideCount > 0 && (
            <span className="native-panel__override-badge">
              {activeOverrideCount} 覆蓋中
            </span>
          )}
        </div>

        {/* 分類篩選 Chips */}
        <div className="native-panel__filter-chips">
          {(['全部', ...CATEGORY_ORDER] as Array<Category | '全部'>).map(cat => (
            <button
              key={cat}
              className={`native-panel__chip ${activeGroup === cat ? 'native-panel__chip--active' : ''}`}
              style={activeGroup === cat && cat !== '全部'
                ? { '--chip-color': CATEGORY_COLORS[cat as Category] } as React.CSSProperties
                : undefined}
              onClick={() => setActiveGroup(cat)}
            >
              {cat}
              {categoryCounts[cat] !== undefined && (
                <span className="native-panel__chip-count">{categoryCounts[cat]}</span>
              )}
            </button>
          ))}
        </div>

        <button className="native-panel__reset-btn" onClick={handleReset}>
          ↺ 全部重置
        </button>
      </div>

      {/* ── 參數格線 ── */}
      <div className="native-panel__body">
        <div className="native-panel__grid">
          {displayParams.map(param => {
            const override   = overrides[param.id];
            const hasOverride = override && now < override.expiresAt;
            const displayVal  = hasOverride ? override.value : param.current;
            const remainSec   = hasOverride ? Math.ceil((override.expiresAt - now) / 1000) : 0;
            const range       = param.max - param.min;
            const pct         = range > 0 ? ((displayVal - param.min) / range) * 100 : 50;
            const cat         = getCategory(param.id);
            const catColor    = CATEGORY_COLORS[cat];
            const label       = PARAM_LABELS[param.id] ?? param.id;

            // 百分比填充 (用於 slider 背景)
            const fillColor = hasOverride ? '#f59e0b' : catColor;

            return (
              <div
                key={param.id}
                className={`native-row ${hasOverride ? 'native-row--active' : ''}`}
              >
                {/* 左：標籤 + 值 */}
                <div className="native-row__left">
                  <span
                    className="native-row__dot"
                    style={{ background: catColor }}
                  />
                  <span className="native-row__label" title={param.id}>
                    {label}
                  </span>
                  <span
                    className="native-row__value"
                    style={{ color: fillColor }}
                  >
                    {displayVal.toFixed(1)}
                  </span>
                </div>

                {/* 滑桿 */}
                <div className="native-row__track">
                  {/* 中心線（params 有正負值時顯示） */}
                  {param.min < 0 && (
                    <div className="native-row__center-line" />
                  )}
                  <input
                    type="range"
                    min={param.min}
                    max={param.max}
                    step={(range) > 2 ? 0.5 : 0.01}
                    value={displayVal}
                    onChange={e => handleChange(param.id, parseFloat(e.target.value))}
                    className="native-row__slider"
                    style={{
                      '--fill':  fillColor,
                      '--pct':   `${pct}%`,
                    } as React.CSSProperties}
                  />
                </div>

                {/* 右：倒數 Badge */}
                <div className="native-row__badge-col">
                  {hasOverride && (
                    <span
                      className={`native-row__timer ${remainSec <= 1 ? 'native-row__timer--urgent' : ''}`}
                    >
                      {remainSec}s
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};
