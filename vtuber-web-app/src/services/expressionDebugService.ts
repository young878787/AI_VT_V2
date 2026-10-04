import type { DebugExpressionIntensity, DebugExpressionKind, DebugMotionKind } from '../dev/expressionPlanDebugFixtures';
import type { ExpressionPlanPayload } from '../types/expressionPlan';

const _port = import.meta.env.BACKEND_PORT || '9999';
const BACKEND = `http://localhost:${_port}`;

export type StudioExpressionKind = DebugExpressionKind | 'neutral' | 'calm' | 'listening' | 'thinking' | 'soft_smile' | 'closed_smile';
export type StudioExpressionPlan = ExpressionPlanPayload & { carryState?: Record<string, unknown> };

export interface CompileExpressionPlanRequest {
  modelName: string;
  intent?: Record<string, unknown>;
  kind?: StudioExpressionKind;
  motionKind?: DebugMotionKind;
  intensity?: DebugExpressionIntensity;
  random?: boolean;
  scenario?: 'speaking_micro' | 'brow_eye_micro';
  seed?: number;
  previousState?: Record<string, unknown>;
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
): Promise<CompileExpressionPlanResponse> {
  const res = await fetch(`${BACKEND}/api/debug/expression-plan`, {
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
