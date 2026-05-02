import type { DebugExpressionBackendIntent } from '../dev/expressionPlanDebugFixtures';
import type { ExpressionPlanPayload } from '../types/expressionPlan';

const _port = import.meta.env.BACKEND_PORT || '9999';
const BACKEND = `http://localhost:${_port}`;

export interface CompileExpressionPlanRequest {
  modelName: string;
  intent: DebugExpressionBackendIntent;
}

export interface CompileExpressionPlanResponse {
  plan: ExpressionPlanPayload;
  summary?: {
    preset?: string;
    bodyMotionProfile?: string;
    idlePlan?: string;
    emotion?: string;
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
