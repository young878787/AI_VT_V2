/**
 * NativeParamPanel — 原生 Live2D 參數即時控制面板
 *
 * - 動態讀取模型所有 Cubism 參數（getParameterCount / getId / min / max / default / current）
 * - 依類別分組，多欄格線顯示
 * - 每個滑桿獨立 5 秒倒數計時器；無操作後自動釋放控制權
 * - 覆蓋優先序：NativeParam > AI 表情 > 眼動追蹤（由 LAppModel.update() 保證）
 * - 自動偵測：滑鼠追蹤期間數值有變動的參數，永久移至「物理/追蹤驅動」區塊（localStorage 持久化）
 */
import React, {
  useState, useEffect, useCallback, useRef, useMemo
} from 'react';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import { useAppStore } from '../store/appStore';
import './NativeParamPanel.css';

const LS_KEY = 'native-panel-auto-detected';
const DETECT_THRESHOLD = 0.01; // 變化超過此值才視為被追蹤驅動

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
  // autoDetectedCount 僅用於觸發 re-render；實際資料存在 Ref 中避免 RAF stale closure
  const [autoDetectedCount, setAutoDetectedCount] = useState(0);

  const prevParamIdsRef      = useRef<string>('');
  const autoDetectedIdsRef   = useRef<Set<string>>(new Set());
  const prevValuesRef        = useRef<Record<string, number>>({});
  const eyeTrackingEnabledRef = useRef(true);
  // 使用者手動觸碰紀錄：paramId → 最後觸碰時間 (Date.now ms)
  // 保護窗口 = OVERRIDE_DURATION_MS + 500ms 緩衝，期間完全跳過自動偵測
  const userTouchedRef       = useRef<Map<string, number>>(new Map());

  // 讀取 eyeTrackingEnabled（Zustand），同步到 Ref 讓 RAF callback 可直接存取
  const eyeTrackingEnabled = useAppStore(state => state.eyeTrackingEnabled);
  useEffect(() => {
    eyeTrackingEnabledRef.current = eyeTrackingEnabled;
  }, [eyeTrackingEnabled]);

  // 從 localStorage 恢復自動偵測紀錄
  useEffect(() => {
    try {
      const stored = localStorage.getItem(LS_KEY);
      if (stored) {
        const ids: string[] = JSON.parse(stored);
        autoDetectedIdsRef.current = new Set(ids);
        setAutoDetectedCount(ids.length);
      }
    } catch { /* 忽略解析錯誤 */ }
  }, []);

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

          // 偵測模型切換（參數 ID 集合改變）→ 清除舊覆蓋與自動偵測紀錄
          const idsKey = fresh.map(p => p.id).join(',');
          if (prevParamIdsRef.current && idsKey !== prevParamIdsRef.current) {
            setOverrides({});
            autoDetectedIdsRef.current = new Set();
            setAutoDetectedCount(0);
            prevValuesRef.current = {};
            localStorage.removeItem(LS_KEY);
            if (typeof (model as any).clearAllNativeParamOverrides === 'function') {
              (model as any).clearAllNativeParamOverrides();
            }
          }
          prevParamIdsRef.current = idsKey;

          // ── 自動偵測：滑鼠追蹤啟用時，比較前後幀數值 ──────────────
          const prev = prevValuesRef.current;
          const nowDetect = Date.now();
          let hasNewDetected = false;
          for (const p of fresh) {
            // 只在 tracking 開啟且尚未偵測過時才檢查
            if (eyeTrackingEnabledRef.current && !autoDetectedIdsRef.current.has(p.id)) {
              // 保護窗口：使用者近期手動調整過此參數 → 跳過偵測
              // 保護時長 = OVERRIDE_DURATION_MS + 500ms 緩衝，涵蓋覆蓋期與覆蓋消退後的數值回彈
              const lastTouched = userTouchedRef.current.get(p.id);
              const isUserProtected =
                lastTouched !== undefined &&
                (nowDetect - lastTouched) < OVERRIDE_DURATION_MS + 500;
              if (!isUserProtected) {
                const prevVal = prev[p.id];
                if (prevVal !== undefined && Math.abs(p.current - prevVal) > DETECT_THRESHOLD) {
                  autoDetectedIdsRef.current.add(p.id);
                  hasNewDetected = true;
                }
              }
            }
            prev[p.id] = p.current; // 無論 tracking 是否開啟都更新基準值
          }
          if (hasNewDetected) {
            const ids = [...autoDetectedIdsRef.current];
            localStorage.setItem(LS_KEY, JSON.stringify(ids));
            setAutoDetectedCount(ids.length);
          }
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
    // 記錄使用者觸碰時間（保護窗口）：防止 RAF tick 誤把使用者操作引發的
    // 數值變化（含初次套用覆蓋的幀延遲、以及覆蓋消退後的數值回彈）
    // 誤判為「物理/追蹤驅動」而將參數移至下方區塊
    const now = Date.now();
    userTouchedRef.current.set(paramId, now);
    prevValuesRef.current[paramId] = value; // 輔助同步基準值（多重防護）
  }, []);

  // ── 全部重置 ─────────────────────────────────────────────────────────
  const handleReset = useCallback(() => {
    const model = LAppLive2DManager.getInstance().getActiveModel();
    if (model && typeof (model as any).clearAllNativeParamOverrides === 'function') {
      (model as any).clearAllNativeParamOverrides();
    }
    setOverrides({});
  }, []);

  // ── 清除自動偵測紀錄 ───────────────────────────────────────────────
  const handleClearAutoDetected = useCallback(() => {
    autoDetectedIdsRef.current = new Set();
    setAutoDetectedCount(0);
    localStorage.removeItem(LS_KEY);
  }, []);

  // ── 分組 & 過濾 ───────────────────────────────────────────────────────
  const displayParams = useMemo(() => {
    return activeGroup === '全部'
      ? params
      : params.filter(p => getCategory(p.id) === activeGroup);
  }, [params, activeGroup]);

  // 物理/追蹤分區僅在「全部」與「物理其他」篩選下啟用
  const shouldSplitPhysicsSection = activeGroup === '全部' || activeGroup === '物理其他';

  // ── 分離：可調整參數 vs 物理/追蹤驅動參數 ─────────────────────────
  const { stableParams, physicsParams } = useMemo(() => {
    if (!shouldSplitPhysicsSection) {
      return { stableParams: displayParams, physicsParams: [] as ParamInfo[] };
    }

    const stable: ParamInfo[] = [];
    const physics: ParamInfo[] = [];
    for (const p of displayParams) {
      if (autoDetectedIdsRef.current.has(p.id)) {
        physics.push(p);
      } else {
        stable.push(p);
      }
    }
    return { stableParams: stable, physicsParams: physics };
    // autoDetectedCount 用於在 ref 更新後觸發此 useMemo 重新計算
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [displayParams, autoDetectedCount, shouldSplitPhysicsSection]);

  // 統計各分類數量
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

  // ── 共用：渲染單一參數列 ──────────────────────────────────────────────
  const renderParamRow = (param: ParamInfo) => {
    const ts         = Date.now();
    const override   = overrides[param.id];
    const hasOverride = override && ts < override.expiresAt;
    const displayVal  = hasOverride ? override.value : param.current;
    const remainSec   = hasOverride ? Math.ceil((override.expiresAt - ts) / 1000) : 0;
    const range       = param.max - param.min;
    const pct         = range > 0 ? ((displayVal - param.min) / range) * 100 : 50;
    const cat         = getCategory(param.id);
    const catColor    = CATEGORY_COLORS[cat];
    const label       = PARAM_LABELS[param.id] ?? param.id;
    const fillColor   = hasOverride ? '#f59e0b' : catColor;

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
          {param.min < 0 && (
            <div className="native-row__center-line" />
          )}
          <input
            type="range"
            min={param.min}
            max={param.max}
            step={range > 2 ? 0.5 : 0.01}
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
  };

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
          {/* 追蹤偵測中指示燈 */}
          {eyeTrackingEnabled && (
            <span className="native-panel__tracking-indicator" title="滑鼠追蹤啟用中：自動偵測被驅動的參數">
              追蹤偵測中
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

        {/* ── 上方：可手動精調的參數 ── */}
        <div className="native-panel__grid">
          {stableParams.map(renderParamRow)}
        </div>

        {/* ── 分隔線 + 物理/追蹤驅動區塊 ── */}
        {shouldSplitPhysicsSection && physicsParams.length > 0 && (
          <>
            <div className="native-panel__physics-divider">
              <div className="native-panel__physics-divider-line" />
              <span className="native-panel__physics-divider-label">
                物理 / 追蹤驅動（自動偵測）
              </span>
              <div className="native-panel__physics-divider-line" />
              <button
                className="native-panel__clear-detected-btn"
                onClick={handleClearAutoDetected}
                title="清除自動偵測記錄，所有參數回到上方可調整區"
              >
                清除
              </button>
            </div>
            <div className="native-panel__grid native-panel__grid--physics">
              {physicsParams.map(renderParamRow)}
            </div>
          </>
        )}
      </div>
    </div>
  );
};
