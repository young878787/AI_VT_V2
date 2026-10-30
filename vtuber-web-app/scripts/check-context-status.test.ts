import { afterAll, beforeEach, describe, expect, mock, test } from 'bun:test';
import { beginChatContext, finishChatContext, formatContextStatus, isContextStatusPayload, receiveContextStatus } from '../src/types/contextStatus';
import type { ContextStatusPayload } from '../src/types/contextStatus';

const payload = (turnId = 'first', status: ContextStatusPayload['status'] = 'sent'): ContextStatusPayload => ({
  type: 'context_status', turn_id: turnId, kind: 'desktop', status,
  foreground: { status: 'ok', app: 'Code.exe' },
  open_apps: { status: 'ok', apps: ['Code.exe', 'chrome.exe'], truncated: false },
});

describe('context status contract and display', () => {
  test('rejects malformed and oversized data, images, and automatic window titles', () => {
    expect(isContextStatusPayload(payload())).toBe(true);
    for (const invalid of [
      { ...payload(), kind: ['desktop'] }, { ...payload(), status: 'finished' },
      { ...payload(), turn_id: '' }, { ...payload(), image: 'private-image' },
      { ...payload(), foreground: { status: 'ok', app: null } },
      { ...payload(), foreground: { status: ['ok'], app: 'Code.exe' } },
      { ...payload(), foreground: { status: 'ok', app: 'Code.exe', title: 'private-title' } },
      { ...payload(), open_apps: { status: 'ok', apps: Array(7).fill('app.exe'), truncated: false } },
      { ...payload(), open_apps: { status: 'ok', apps: [false], truncated: false } },
      { ...payload(), foreground: undefined },
      { type: 'context_status', turn_id: 'first', kind: 'screenshot', status: 'sent' },
    ]) expect(isContextStatusPayload(invalid)).toBe(false);
  });

  test('keeps only the active turn and ignores late events after completion', () => {
    const state = beginChatContext('second');
    expect(receiveContextStatus(state, payload('first'))).toBe(state);
    const completed = finishChatContext(receiveContextStatus(state, payload('second')), 'completed');
    expect(receiveContextStatus(completed, payload('second'))).toBe(completed);
    expect(formatContextStatus(completed.items.desktop!, completed.phase).text).toContain('上輪已送入');
  });

  test('preserves confirmed delivery through later request failure and cancellation', () => {
    const state = receiveContextStatus(beginChatContext('first'), payload());
    expect(receiveContextStatus(state, payload('first', 'unconfirmed'))).toBe(state);
    expect(finishChatContext(state, 'cancelled').items.desktop?.status).toBe('sent');
    expect(beginChatContext('second').items.desktop?.status).toBe('reading');
  });

  test('finishes pending states without inventing delivery', () => {
    expect(finishChatContext(beginChatContext('first'), 'cancelled').items.desktop?.status).toBe('omitted');
    const sending = receiveContextStatus(beginChatContext('first'), payload('first', 'sending'));
    expect(finishChatContext(sending, 'failed').items.desktop?.status).toBe('unconfirmed');
  });

  test('shows actual count, partial source failure, and a shortened list', () => {
    const item = payload();
    item.open_apps!.truncated = true;
    const normal = formatContextStatus(item, 'active');
    expect(normal.text).toContain('VS Code／前景程式');
    expect(normal.text).toContain('應用資訊 2 項');
    expect(normal.text).toContain('清單已縮減');
    expect(normal.title).toContain('Code.exe、chrome.exe');
    item.foreground = { status: 'unavailable', app: null };
    expect(isContextStatusPayload(item)).toBe(true);
    expect(formatContextStatus(item, 'active').tone).toBe('warning');
    expect(formatContextStatus(item, 'active').text).toContain('前景程式無法取得');
  });

  test('shows a title only after delivery and names the actual screenshot target', () => {
    const activity: ContextStatusPayload = { ...payload(), kind: 'activity', open_apps: undefined,
      foreground: { status: 'ok', app: 'Code.exe', title: '目前視窗標題' } };
    expect(isContextStatusPayload(activity)).toBe(true);
    expect(formatContextStatus(activity, 'active').text).toContain('目前視窗標題');
    expect(formatContextStatus({ ...activity, status: 'ready' }, 'active').text).not.toContain('目前視窗標題');
    const image: ContextStatusPayload = { type: 'context_status', turn_id: 'first', kind: 'screenshot', status: 'sent', target: 'screen' };
    expect(formatContextStatus(image, 'active').text).toContain('桌面截圖');
    expect(formatContextStatus({ ...image, status: 'unavailable', reason: 'vision_not_supported' }, 'active').text).toContain('模型未接受圖片');
  });
});

mock.module('../src/live2d/LAppLive2DManager', () => ({ LAppLive2DManager: { getInstance: () => ({ getActiveModel: () => null }) } }));
mock.module('../src/audio/TTSPlayer', () => ({ TTSPlayer: { getInstance: () => ({ stop() {}, async play() {} }) } }));
mock.module('../src/services/actionScheduler', () => ({ actionScheduler: { cancel() {}, setReporter() {}, submit() {} } }));

class FakeWebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static sockets: FakeWebSocket[] = [];
  readyState = FakeWebSocket.OPEN;
  throwOnSend = false;
  sent: Record<string, string>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor() { FakeWebSocket.sockets.push(this); }
  send(data: string) {
    if (this.throwOnSend) throw new Error('fake send failure');
    this.sent.push(JSON.parse(data));
  }
  receive(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

const originalWebSocket = globalThis.WebSocket;
const originalSetTimeout = globalThis.setTimeout;
globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
const { useAppStore } = await import('../src/store/appStore');
const { wsService } = await import('../src/services/wsService');
wsService.connect();
let socket = FakeWebSocket.sockets.at(-1)!;
socket.onopen?.();

beforeEach(() => {
  useAppStore.getState().resetChatContext();
  useAppStore.getState().clearChatHistory();
});

afterAll(() => {
  globalThis.WebSocket = originalWebSocket;
  globalThis.setTimeout = originalSetTimeout;
});

describe('real WebSocket service and store', () => {
  test('text and voice start a new turn and stale events cannot change the hint', () => {
    wsService.sendMessage('第一句');
    const first = socket.sent.at(-1)!.turn_id;
    socket.receive(payload(first));
    expect(useAppStore.getState().chatContext.items.desktop?.status).toBe('sent');
    wsService.sendMessage('語音第二句', 'voice');
    const second = socket.sent.at(-1)!.turn_id;
    expect(socket.sent.at(-1)!.source).toBe('voice');
    socket.receive({ ...payload(first), foreground: { status: 'ok', app: 'old.exe' } });
    socket.receive({ type: 'turn_cancelled', turn_id: first });
    expect(useAppStore.getState().chatContext.turnId).toBe(second);
    expect(useAppStore.getState().chatContext.items.desktop?.status).toBe('reading');
    socket.receive(payload(second));
    socket.receive({ type: 'stream_end', turn_id: second });
    expect(useAppStore.getState().chatContext.phase).toBe('completed');
    expect(useAppStore.getState().chatContext.items.desktop?.status).toBe('sent');
  });

  test('malformed events, cancelled turns, and late events never claim delivery', () => {
    wsService.sendMessage('測試');
    const turnId = socket.sent.at(-1)!.turn_id;
    socket.receive({ ...payload(turnId), image: 'private-image' });
    expect(useAppStore.getState().chatContext.items.desktop?.status).toBe('reading');
    socket.receive({ type: 'turn_cancelled', turn_id: turnId });
    socket.receive(payload(turnId));
    expect(useAppStore.getState().chatContext.phase).toBe('cancelled');
    expect(useAppStore.getState().chatContext.items.desktop?.status).toBe('omitted');
  });

  test('request errors finish progress, and reset clears transient data', () => {
    wsService.sendMessage('測試');
    const turnId = socket.sent.at(-1)!.turn_id;
    socket.receive(payload(turnId, 'sending'));
    socket.receive({ type: 'error', turn_id: turnId, content: '測試錯誤' });
    expect(useAppStore.getState().chatContext.items.desktop?.status).toBe('unconfirmed');
    wsService.syncResetSession();
    expect(useAppStore.getState().chatContext.phase).toBe('idle');
    expect(useAppStore.getState().chatContext.items).toEqual({});
  });

  test('a synchronous send failure cannot leave a pending context or accept a late event', () => {
    socket.throwOnSend = true;
    try {
      wsService.sendMessage('送出失敗');
      expect(useAppStore.getState().isAiTyping).toBe(false);
      expect(useAppStore.getState().chatContext.phase).toBe('disconnected');
      expect(useAppStore.getState().chatContext.items).toEqual({});
      socket.receive(payload(socket.sent.at(-1)!.turn_id));
      expect(useAppStore.getState().chatContext.phase).toBe('disconnected');
    } finally {
      socket.throwOnSend = false;
    }
  });

  test('disconnect and failed send clear the old snapshot, reconnect starts idle', () => {
    wsService.sendMessage('測試');
    socket.receive(payload(socket.sent.at(-1)!.turn_id));
    globalThis.setTimeout = (() => 0) as unknown as typeof setTimeout;
    socket.onclose?.();
    expect(useAppStore.getState().chatContext.phase).toBe('disconnected');
    expect(useAppStore.getState().chatContext.items).toEqual({});
    wsService.sendMessage('斷線訊息');
    expect(useAppStore.getState().chatContext.phase).toBe('disconnected');
    wsService.connect();
    socket = FakeWebSocket.sockets.at(-1)!;
    socket.onopen?.();
    expect(useAppStore.getState().chatContext.phase).toBe('idle');
  });
});
