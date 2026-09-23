/**
 * Voice WebSocket 客戶端（/ws/voice）
 * 上送麥克風 PCM 幀、接收 ASR 結果；asr_final 直接轉入既有聊天管線
 * （wsService.sendMessage），語音輸入與打字輸入在聊天層等價。
 * 半雙工閘門：AI 播音期間（TTSPlayer.isPlaying）丟棄音訊，避免迴聲誤觸發。
 */
import { TTSPlayer } from '../audio/TTSPlayer';
import { useAppStore } from '../store/appStore';
import { wsService } from './wsService';

interface AsrStateMessage { type: 'asr_state'; state: string }
interface AsrFinalMessage { type: 'asr_final'; text: string; durationMs?: number }
interface ErrorMessage { type: 'error'; message: string }
type ServerMessage = AsrStateMessage | AsrFinalMessage | ErrorMessage;

class VoiceWsService {
    private ws: WebSocket | null = null;
    private gateOpen: boolean = false;
    private stopped: boolean = false;
    private retryCount: number = 0;
    private readonly MAX_RETRIES = 5;
    private readonly RETRY_DELAY_MS = 3000;
    private ttsPlayer: TTSPlayer;

    constructor() {
        this.ttsPlayer = TTSPlayer.getInstance();
    }

    public connect() {
        this.stopped = false;
        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
        if (this.retryCount >= this.MAX_RETRIES) {
            console.warn('[VoiceWS] 重連次數已達上限，停止重連');
            return;
        }

        const backendPort = import.meta.env.BACKEND_PORT || '9999';
        this.ws = new WebSocket(`ws://localhost:${backendPort}/ws/voice`);
        this.ws.binaryType = 'arraybuffer';

        this.ws.onopen = () => {
            console.log('[VoiceWS] connected');
            this.retryCount = 0;
            this.sendGate();
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data as string) as ServerMessage;
                if (data.type === 'asr_final') {
                    if (data.text.trim()) {
                        // 辨識完成 → 作為使用者訊息送進聊天管線
                        wsService.sendMessage(data.text.trim(), 'voice');
                    }
                } else if (data.type === 'error') {
                    console.error('[VoiceWS] 錯誤:', data.message);
                    useAppStore.getState().appendChatMessage({
                        role: 'system',
                        content: `⚠️ 語音輸入：${data.message}`,
                    });
                }
                // asr_state 目前僅供除錯 log
            } catch (e) {
                console.error('[VoiceWS] 訊息解析錯誤:', e);
            }
        };

        this.ws.onclose = () => {
            this.ws = null;
            this.gateOpen = false;
            if (this.stopped) return;

            this.retryCount++;
            if (this.retryCount >= this.MAX_RETRIES) {
                console.warn('[VoiceWS] 斷線重連失敗，停止重連');
                useAppStore.getState().appendChatMessage({
                    role: 'system',
                    content: '⚠️ 系統：語音輸入連線中斷，重連失敗。請重新開啟語音模式。',
                });
            } else {
                console.log(`[VoiceWS] 斷線，${this.RETRY_DELAY_MS / 1000} 秒後重連...`);
                setTimeout(() => this.connect(), this.RETRY_DELAY_MS);
            }
        };

        this.ws.onerror = (error) => {
            console.error('[VoiceWS] error:', error);
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
        this.stopped = true;
        if (this.ws) {
            this.ws.close();
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
