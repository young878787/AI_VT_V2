/**
 * Voice WebSocket 客戶端（/ws/voice）
 * 上送單次麥克風 PCM 幀並接收 ASR 結果；連線只在聊天框按下麥克風時建立。
 * 半雙工閘門：AI 播音期間（TTSPlayer.isPlaying）丟棄音訊，避免迴聲誤觸發。
 */
import { TTSPlayer } from '../audio/TTSPlayer';
import { useAppStore } from '../store/appStore';
import { wsService } from './wsService';

interface AsrStateMessage { type: 'asr_state'; state: string }
interface AsrFinalMessage { type: 'asr_final'; text: string; durationMs?: number }
interface ErrorMessage { type: 'error'; message: string }
type ServerMessage = AsrStateMessage | AsrFinalMessage | ErrorMessage;

export interface VoiceWsHandlers {
    onState?: (state: string) => void;
    onFinal?: (text: string) => void;
    onError?: (message: string) => void;
}

class VoiceWsService {
    private ws: WebSocket | null = null;
    private gateOpen: boolean = false;
    private intentionallyClosed = new WeakSet<WebSocket>();
    private ttsPlayer: TTSPlayer;
    private handlers: VoiceWsHandlers | null = null;
    private connectPromise: Promise<void> | null = null;
    private cancelPendingConnection: (() => void) | null = null;

    constructor() {
        this.ttsPlayer = TTSPlayer.getInstance();
    }

    public connect(): Promise<void> {
        if (this.ws?.readyState === WebSocket.OPEN && !this.connectPromise) {
            return Promise.resolve();
        }
        if (this.connectPromise) return this.connectPromise;

        const backendPort = import.meta.env.BACKEND_PORT || '9999';
        let cancelPending: () => void = () => undefined;
        const pending = new Promise<void>((resolve, reject) => {
            let socket: WebSocket | null = null;
            let ready = false;
            let settled = false;
            const settleReady = () => {
                if (settled) return;
                settled = true;
                ready = true;
                window.clearTimeout(timeoutId);
                resolve();
            };
            const settleError = (message: string) => {
                if (settled) return;
                settled = true;
                window.clearTimeout(timeoutId);
                reject(new Error(message));
            };
            cancelPending = () => settleError('語音收音已停止');
            const timeoutId = window.setTimeout(() => {
                const message = '語音服務連線逾時';
                settleError(message);
                this.handlers?.onError?.(message);
                if (socket && this.ws === socket) socket.close();
            }, 5000);

            try {
                socket = new WebSocket(`ws://localhost:${backendPort}/ws/voice`);
            } catch {
                window.clearTimeout(timeoutId);
                reject(new Error('無法建立語音服務連線'));
                return;
            }
            this.ws = socket;
            socket.binaryType = 'arraybuffer';

            socket.onopen = () => {
                console.log('[VoiceWS] connected');
                this.sendGate();
            };

            socket.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data as string) as ServerMessage;
                    if (data.type === 'asr_final') {
                        if (data.text.trim()) {
                            const text = data.text.trim();
                            if (this.handlers?.onFinal) this.handlers.onFinal(text);
                            else wsService.sendMessage(text, 'voice');
                        }
                    } else if (data.type === 'asr_state') {
                        if (data.state === 'ready' || data.state === 'idle') settleReady();
                        this.handlers?.onState?.(data.state);
                    } else if (data.type === 'error') {
                        console.error('[VoiceWS] 錯誤:', data.message);
                        settleError(data.message);
                        if (this.handlers?.onError) this.handlers.onError(data.message);
                        else useAppStore.getState().appendChatMessage({
                            role: 'system',
                            content: `⚠️ 語音收音：${data.message}`,
                        });
                    }
                } catch (error) {
                    console.error('[VoiceWS] 訊息解析錯誤:', error);
                }
            };

            socket.onclose = () => {
                if (this.ws === socket) {
                    this.ws = null;
                    this.gateOpen = false;
                }
                if (this.intentionallyClosed.delete(socket)) {
                    settleError('語音收音已停止');
                    return;
                }
                const message = ready ? '語音服務連線中斷' : '無法連線到語音服務';
                settleError(message);
                this.handlers?.onError?.(message);
            };

            socket.onerror = (error) => {
                console.error('[VoiceWS] error:', error);
                if (this.intentionallyClosed.has(socket)) return;
                const message = '無法連線到語音服務';
                settleError(message);
                this.handlers?.onError?.(message);
            };
        });
        this.connectPromise = pending;
        this.cancelPendingConnection = cancelPending;
        void pending.then(
            () => {
                if (this.connectPromise === pending) this.connectPromise = null;
                if (this.cancelPendingConnection === cancelPending) this.cancelPendingConnection = null;
            },
            () => {
                if (this.connectPromise === pending) this.connectPromise = null;
                if (this.cancelPendingConnection === cancelPending) this.cancelPendingConnection = null;
            },
        );
        return pending;
    }

    public setHandlers(handlers: VoiceWsHandlers | null): () => void {
        this.handlers = handlers;
        return () => {
            if (this.handlers === handlers) this.handlers = null;
        };
    }

    /**
     * 半雙工閘門：false 時丟棄音訊幀並通知後端停止收音
     */
    public setGate(open: boolean) {
        if (this.gateOpen === open) return;
        this.gateOpen = open;
        this.sendGate();
    }

    public sendAudioFrame(buf: ArrayBuffer) {
        if (!this.gateOpen) return;
        // 半雙工：AI 播音中丟棄（瀏覽器 AEC 為輔助，閘門為主要防線）
        if (this.ttsPlayer.getIsPlaying()) return;
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(buf);
        }
    }

    public disconnect() {
        this.cancelPendingConnection?.();
        this.cancelPendingConnection = null;
        this.connectPromise = null;
        const socket = this.ws;
        if (socket) {
            this.intentionallyClosed.add(socket);
            socket.close();
            this.ws = null;
        }
        this.gateOpen = false;
    }

    private sendGate() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'mic_state', active: this.gateOpen }));
        }
    }
}

export const voiceWsService = new VoiceWsService();
