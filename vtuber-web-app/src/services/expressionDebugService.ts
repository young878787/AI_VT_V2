import type { DebugExpressionIntensity, DebugExpressionKind } from '../dev/expressionPlanDebugFixtures';
import type { ExpressionPlanPayload } from '../types/expressionPlan';

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

export async function compileDebugExpressionPlan(
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

  return await res.json() as CompileExpressionPlanResponse;
}
