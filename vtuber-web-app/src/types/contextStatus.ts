export type ContextKind = 'desktop' | 'activity' | 'screenshot';
export type ContextDeliveryStatus = 'reading' | 'ready' | 'sending' | 'sent' | 'unavailable' | 'omitted' | 'unconfirmed';
type SourceStatus = 'ok' | 'unavailable' | 'unsupported' | 'error';

export interface ContextStatusPayload {
  type: 'context_status';
  turn_id: string;
  kind: ContextKind;
  status: ContextDeliveryStatus;
  captured_at?: string | null;
  reason?: string | null;
  foreground?: { status: SourceStatus; app: string | null; title?: string; reason?: string | null };
  open_apps?: { status: SourceStatus; apps: string[]; truncated: boolean; reason?: string | null };
  target?: 'foreground' | 'screen';
}

export interface ChatContextState {
  turnId: string | null;
  phase: 'idle' | 'active' | 'completed' | 'cancelled' | 'failed' | 'disconnected';
  items: Partial<Record<ContextKind, ContextStatusPayload>>;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isText = (value: unknown, max: number) => typeof value === 'string' && Array.from(value).length <= max;
const isOptionalText = (value: unknown, max: number) => value == null || isText(value, max);
const isSourceStatus = (value: unknown) => typeof value === 'string' && ['ok', 'unavailable', 'unsupported', 'error'].includes(value);

export function isContextStatusPayload(value: unknown): value is ContextStatusPayload {
  if (!isObject(value) || value.type !== 'context_status' || !isText(value.turn_id, 128) || !value.turn_id) return false;
  if (typeof value.kind !== 'string' || !['desktop', 'activity', 'screenshot'].includes(value.kind) ||
      typeof value.status !== 'string' || !['reading', 'ready', 'sending', 'sent', 'unavailable', 'omitted', 'unconfirmed'].includes(value.status)) return false;
  if (Object.keys(value).some(key => !['type', 'turn_id', 'kind', 'status', 'captured_at', 'reason', 'foreground', 'open_apps', 'target'].includes(key))) return false;
  if (!isOptionalText(value.captured_at, 80) || !isOptionalText(value.reason, 80)) return false;
  if (value.foreground !== undefined) {
    const foreground = value.foreground;
    if (value.kind === 'screenshot' || !isObject(foreground) || !isSourceStatus(foreground.status) ||
        !(foreground.app === null || isText(foreground.app, 80)) || !isOptionalText(foreground.reason, 80)) return false;
    if (foreground.title !== undefined && (value.kind !== 'activity' || !isText(foreground.title, 160))) return false;
    if (foreground.status === 'ok' && !foreground.app) return false;
  }
  if (value.open_apps !== undefined) {
    const opened = value.open_apps;
    if (value.kind !== 'desktop' || !isObject(opened) || !isSourceStatus(opened.status) ||
        !Array.isArray(opened.apps) || opened.apps.length > 6 || !opened.apps.every(app => isText(app, 80)) ||
        typeof opened.truncated !== 'boolean' || !isOptionalText(opened.reason, 80)) return false;
  }
  if (value.target !== undefined && (value.kind !== 'screenshot' || typeof value.target !== 'string' || !['foreground', 'screen'].includes(value.target))) return false;
  if (['ready', 'sending', 'sent'].includes(value.status)) {
    if (value.kind === 'desktop' && (!isObject(value.foreground) || !isObject(value.open_apps))) return false;
    if (value.kind === 'desktop' && (value.foreground as Record<string, unknown>).status !== 'ok' &&
        (value.open_apps as Record<string, unknown>).status !== 'ok') return false;
    if (value.kind === 'activity' && (!isObject(value.foreground) || value.foreground.status !== 'ok')) return false;
    if (value.kind === 'screenshot' && value.target === undefined) return false;
  }
  return true;
}

export const emptyChatContext = (phase: ChatContextState['phase'] = 'idle'): ChatContextState =>
  ({ turnId: null, phase, items: {} });

export function beginChatContext(turnId: string): ChatContextState {
  return { turnId, phase: 'active', items: {
    desktop: { type: 'context_status', turn_id: turnId, kind: 'desktop', status: 'reading' },
  } };
}

export function receiveContextStatus(state: ChatContextState, payload: ContextStatusPayload): ChatContextState {
  if (state.phase !== 'active' || state.turnId !== payload.turn_id) return state;
  if (state.items[payload.kind]?.status === 'sent' && payload.status !== 'sent') return state;
  return { ...state, items: { ...state.items, [payload.kind]: payload } };
}

export function finishChatContext(state: ChatContextState, phase: 'completed' | 'cancelled' | 'failed'): ChatContextState {
  if (state.phase !== 'active') return state;
  const items = { ...state.items };
  for (const kind of ['desktop', 'activity', 'screenshot'] as const) {
    const item = items[kind];
    if (item && ['reading', 'ready', 'sending'].includes(item.status)) {
      items[kind] = { ...item, status: item.status === 'sending' ? 'unconfirmed' : 'omitted', reason: `turn_${phase}` };
    }
  }
  return { ...state, phase, items };
}

const REASONS: Record<string, string> = {
  timeout: '讀取逾時', source_busy: '桌面讀取忙碌', local_access_required: '需要本機連線',
  windows_required: '需要 Windows', desktop_unavailable: '桌面不可用', desktop_read_failed: '讀取失敗',
  no_foreground: '沒有前景視窗', access_denied: '無法讀取視窗', changed_during_capture: '擷取時視窗已切換',
  enumeration_failed: '應用清單讀取失敗', target_unavailable: '目標視窗不可用', empty_capture: '畫面為空',
  context_budget: '輸入預算不足', tool_result_budget: '輸入預算不足', image_context_budget: '圖片預算不足',
  tool_context_budget: '輸入預算不足', image_not_in_request: '圖片未納入請求', vision_not_supported: '模型未接受圖片',
  tool_deadline: '工具逾時', tool_call_limit: '工具次數已達上限', screenshot_limit: '本輪截圖已達上限',
  request_failed: '請求失敗', turn_cancelled: '回合已中斷', turn_failed: '回覆失敗', turn_completed: '狀態未確認',
};

export function formatContextStatus(item: ContextStatusPayload, phase: ChatContextState['phase']) {
  const label = item.kind === 'desktop' ? '桌面' : item.kind === 'activity' ? '視窗' : '畫面';
  const reason = item.reason ? REASONS[item.reason] || '資料不可用' : '資料不可用';
  const foreground = item.foreground;
  const app = foreground?.status === 'ok' ? foreground.app : null;
  const appName = app?.toLowerCase() === 'code.exe' ? 'VS Code' : app;
  let text: string;
  if (item.status === 'sent') {
    const details: string[] = [];
    if (item.kind === 'desktop') {
      details.push(appName ? `${appName}／前景程式` : '前景程式無法取得');
      details.push(item.open_apps?.status === 'ok' ? `應用資訊 ${item.open_apps.apps.length} 項` : '應用清單無法取得');
      if (item.open_apps?.truncated) details.push('清單已縮減');
    } else if (item.kind === 'activity') {
      details.push(foreground?.title || appName || '前景視窗無法取得');
    } else {
      details.push(item.target === 'screen' ? '桌面截圖' : '前景截圖');
    }
    text = `${phase === 'active' ? '本輪' : '上輪'}已送入 · ${details.join(' · ')}`;
  } else if (item.status === 'reading') {
    text = item.kind === 'screenshot' ? '擷取中…' : '讀取中…';
  } else if (item.status === 'ready') {
    text = `已${item.kind === 'screenshot' ? '擷取' : '取得'}${appName ? ` · ${appName}` : ''} · 準備送入`;
  } else if (item.status === 'sending') {
    text = `送入中${appName ? ` · ${appName}` : ''}…`;
  } else if (item.reason === 'vision_not_supported') {
    text = '模型未接受圖片';
  } else {
    text = `${item.status === 'omitted' ? '未送入' : item.status === 'unconfirmed' ? '送入未確認' : '無法取得'}（${reason}）`;
  }
  const partial = item.kind === 'desktop' && item.status === 'sent' &&
    (foreground?.status !== 'ok' || item.open_apps?.status !== 'ok');
  return {
    text: `${label}：${text}`,
    title: [text, item.captured_at ? `擷取時間：${item.captured_at}` : '',
      item.open_apps?.status === 'ok' ? `送入應用：${item.open_apps.apps.join('、') || '無'}` : ''].filter(Boolean).join('\n'),
    tone: item.status === 'sent' && !partial ? 'success' :
      ['unavailable', 'omitted', 'unconfirmed'].includes(item.status) || partial ? 'warning' : 'neutral',
  };
}
