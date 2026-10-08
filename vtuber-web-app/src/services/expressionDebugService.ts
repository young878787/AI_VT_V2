import type { DebugExpressionIntensity, DebugExpressionKind } from '../dev/expressionPlanDebugFixtures';
import { isExpressionPlanPayload, type ExpressionPlanPayload } from '../types/expressionPlan';

const _port = import.meta.env.BACKEND_PORT || '9999';
const BACKEND = `http://localhost:${_port}`;

export type StudioExpressionKind = Exclude<DebugExpressionKind, 'happy' | 'teasing'>
  | 'calm' | 'thinking' | 'soft_smile' | 'closed_smile';
export type StudioExpressionPlan = ExpressionPlanPayload & { carryState?: Record<string, unknown> };

export interface CompileExpressionPlanRequest {
  modelName: string;
  intent?: Record<string, unknown>;
  kind?: StudioExpressionKind | 'neutral' | 'happy' | 'listening' | 'teasing';
  motionKind?: string;
  expressionVariant?: string;
  eyeMotionStyle?: string;
  blinkStyle?: string;
  idleStyle?: string;
  intensity?: DebugExpressionIntensity;
  random?: boolean;
  scenario?: string;
  seed?: number;
  previousState?: Record<string, unknown>;
}

export interface ExpressionDebugCatalog {
  modelName: string;
  expressionFamilies: Array<{ id: StudioExpressionKind; label: string; variants: Array<{ id: string; label: string }> }>;
  motions: Array<{ id: string; label: string; theme: string; expressionKind: StudioExpressionKind }>;
  eyeStyles: Array<{ id: string; label: string }>;
  blinkStyles: Array<{ id: string; label: string }>;
  idleStyles: Array<{ id: string; label: string; family: StudioExpressionKind }>;
  scenarios: Array<{ id: string; label: string; description: string }>;
}

export async function fetchExpressionDebugCatalog(apiBaseUrl = BACKEND): Promise<ExpressionDebugCatalog> {
  const response = await fetch(`${apiBaseUrl}/api/debug/expression-catalog`);
  if (!response.ok) throw new Error(`表情清單載入失敗 (${response.status})`);
  const catalog = await response.json() as ExpressionDebugCatalog;
  const rows = [catalog.expressionFamilies, catalog.motions, catalog.eyeStyles, catalog.blinkStyles, catalog.idleStyles, catalog.scenarios];
  if (typeof catalog.modelName !== 'string' || rows.some(items => !Array.isArray(items)
    || items.some(item => typeof item.id !== 'string' || typeof item.label !== 'string'))
    || catalog.expressionFamilies.some(family => !Array.isArray(family.variants)
      || family.variants.some(variant => typeof variant.id !== 'string' || typeof variant.label !== 'string'))) {
    throw new Error('表情清單格式不完整，請核對前後端版本。');
  }
  clearExpressionPlanCache();
  return catalog;
}

export interface CompileExpressionPlanResponse {
  plan: StudioExpressionPlan;
  summary?: {
    preset?: string;
    bodyMotionProfile?: string;
    idlePlan?: string;
    emotion?: string;
    label?: string;
    source?: string;
    rawReply?: string | null;
    spokenText?: string;
    motionKind?: string;
    expressionFamily?: string;
    expressionVariant?: string;
    seed?: number;
  };
}

const MAX_CACHED_PLANS = 128;
const compiledPlans = new Map<string, CompileExpressionPlanResponse>();
const pendingPlans = new Map<string, Promise<CompileExpressionPlanResponse>>();
let cacheGeneration = 0;

function clearExpressionPlanCache(): void {
  cacheGeneration += 1;
  compiledPlans.clear();
  pendingPlans.clear();
}

function requestCacheKey(request: CompileExpressionPlanRequest, apiBaseUrl: string): string | null {
  // 沒有固定 seed 時，每次都須保留後端重新隨機選擇的行為。
  if (typeof request.seed !== 'number' || !Number.isInteger(request.seed)
    || request.seed < 0 || request.seed > 2147483647) return null;
  return JSON.stringify([apiBaseUrl, request], (_key, value: unknown) => (
    value && typeof value === 'object' && !Array.isArray(value)
      ? Object.fromEntries(Object.entries(value).sort(([left], [right]) => left.localeCompare(right)))
      : value
  ));
}

async function fetchCompiledExpressionPlan(
  request: CompileExpressionPlanRequest,
  apiBaseUrl = BACKEND,
): Promise<CompileExpressionPlanResponse> {
  const res = await fetch(`${apiBaseUrl}/api/debug/expression-plan`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
  });

  if (!res.ok) {
    const errorPayload = await res.json().catch(() => ({}));
    const detail = typeof errorPayload.detail === 'string'
      ? errorPayload.detail
      : `後端編譯失敗 (${res.status})`;
    throw new Error(detail);
  }

  const response = await res.json() as CompileExpressionPlanResponse;
  if (!isExpressionPlanPayload(response?.plan)) throw new Error('表情資料格式不完整，請核對前後端版本。');
  const plan = response.plan;
  const checks = [
    [request.expressionVariant, plan.debug?.expressionVariant],
    [request.motionKind, plan.motionPlan?.variant],
    [request.eyeMotionStyle, plan.eyeMotionPlan?.style],
    [request.blinkStyle, plan.blinkPlan.style],
    [request.idleStyle, plan.idlePlan?.name],
  ];
  for (const [requested, resolved] of checks) {
    if (requested && requested !== resolved) throw new Error(`要求 ${requested}，實際編譯為 ${resolved ?? '未提供'}，已停止播放。`);
  }
  return response;
}

export async function compileDebugExpressionPlan(
  request: CompileExpressionPlanRequest,
  apiBaseUrl = BACKEND,
): Promise<CompileExpressionPlanResponse> {
  // 固定請求快照，避免呼叫者在等候回應時改動快取對應的設定。
  const snapshot = structuredClone(request);
  const key = requestCacheKey(snapshot, apiBaseUrl);
  if (key === null) return fetchCompiledExpressionPlan(snapshot, apiBaseUrl);
  const cached = compiledPlans.get(key);
  if (cached) {
    compiledPlans.delete(key);
    compiledPlans.set(key, cached);
    return structuredClone(cached);
  }

  let pending = pendingPlans.get(key);
  if (!pending) {
    const generation = cacheGeneration;
    pending = fetchCompiledExpressionPlan(snapshot, apiBaseUrl).then(response => {
      if (generation === cacheGeneration) {
        compiledPlans.set(key, response);
        if (compiledPlans.size > MAX_CACHED_PLANS) compiledPlans.delete(compiledPlans.keys().next().value!);
      }
      return response;
    });
    pendingPlans.set(key, pending);
  }
  try {
    // Scheduler/renderer 的播放狀態不得污染後續重播的原始計畫。
    return structuredClone(await pending);
  } finally {
    if (pendingPlans.get(key) === pending) pendingPlans.delete(key);
  }
}
