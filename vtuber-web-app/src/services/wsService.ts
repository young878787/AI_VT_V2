import { useAppStore } from '../store/appStore';
import { TTSPlayer } from '../audio/TTSPlayer';

class WSService {
    private ws: WebSocket | null = null;
    private currentAssistantMessageId: string | null = null;
    private retryCount: number = 0;
    private readonly MAX_RETRIES = 5;
    private readonly RETRY_DELAY_MS = 3000;
    private readonly chatPersistenceEnabled: boolean;
    private readonly sessionStorageKey = 'vtuber-chat-session-id';
    private sessionId: string | null = null;
    
    // TTS 播放器
    private ttsPlayer: TTSPlayer;

    constructor() {
        this.ttsPlayer = TTSPlayer.getInstance();
        this.chatPersistenceEnabled = String(import.meta.env.VITE_CHAT_PERSISTENCE_ENABLED ?? 'false').toLowerCase() === 'true';
        if (this.chatPersistenceEnabled) {
            this.sessionId = this.getOrCreateSessionId();
        }
    }

    private getOrCreateSessionId(): string {
        try {
            const existing = localStorage.getItem(this.sessionStorageKey);
            if (existing) {
                return existing;
            }

            const raw = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
                ? crypto.randomUUID()
                : `session_${Date.now()}_${Math.random().toString(36).slice(2)}`;

            const normalized = raw.replace(/[^A-Za-z0-9_-]/g, '_');
            localStorage.setItem(this.sessionStorageKey, normalized);
            return normalized;
        } catch {
            return `session_${Date.now()}`;
        }
    }

    public connect() {
        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;

        // 已達重連上限，不再嘗試
        if (this.retryCount >= this.MAX_RETRIES) return;

        const backendPort = import.meta.env.BACKEND_PORT || '9999';
        this.ws = new WebSocket(`ws://localhost:${backendPort}/ws/chat`);

        this.ws.onopen = () => {
            console.log('WebSocket connected');
            this.retryCount = 0; // 成功連線後重置重試計數
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                const store = useAppStore.getState();

                if (data.type === 'text_stream') {
                    if (!this.currentAssistantMessageId) {
                        this.currentAssistantMessageId = store.appendChatMessage({ role: 'assistant', content: data.content });
                    } else {
                        const currentMsg = useAppStore.getState().chatHistory.find(m => m.id === this.currentAssistantMessageId);
                        if (currentMsg) {
                            store.updateChatMessage(this.currentAssistantMessageId, currentMsg.content + data.content);
                        }
                    }
                } else if (data.type === 'behavior') {
                    console.log(`Received AI behavior: head=${data.headIntensity}, blush=${data.blushLevel}, eyeL=${data.eyeLOpen}, eyeR=${data.eyeROpen}, mouth=${data.mouthForm}, sync=${data.eyeSync}`);
                    store.setAiBehavior(
                        data.headIntensity,
                        data.blushLevel,
                        data.eyeLOpen,
                        data.eyeROpen,
                        data.durationSec,
                        data.mouthForm ?? 0.0,
                        data.browLY ?? 0.0,
                        data.browRY ?? 0.0,
                        data.browLAngle ?? 0.0,
                        data.browRAngle ?? 0.0,
                        data.browLForm ?? 0.0,
                        data.browRForm ?? 0.0,
                        data.eyeSync ?? true
                    );
                } else if (data.type === 'stream_end') {
                    this.currentAssistantMessageId = null;
                    store.setAiTyping(false);
                } else if (data.type === 'voice') {
                    // TTS 語音播放
                    this.playVoice(data.audio, data.format || 'mp3');
                } else if (data.type === 'compressing') {
                    store.setCompressing(true);
                } else if (data.type === 'compress_done') {
                    store.setCompressing(false);
                } else if (data.type === 'jpaf_update') {
                    store.setJpafState({
                        persona: data.persona,
                        dominant: data.dominant,
                        auxiliary: data.auxiliary ?? '',
                        baseWeights: data.baseWeights ?? {},
                        turnCount: data.turnCount,
                    });
                } else if (data.type === 'error') {
                    store.appendChatMessage({ role: 'system', content: data.content });
                    store.setAiTyping(false);
                    this.currentAssistantMessageId = null;
                }
            } catch (e) {
                console.error('WebSocket message parsing error:', e);
            }
        };

        this.ws.onclose = () => {
            this.ws = null;
            this.currentAssistantMessageId = null;
            const store = useAppStore.getState();

            // 防呆：斷線時確保 AI 狀態歸零
            store.setAiTyping(false);
            store.setCompressing(false);

            this.retryCount++;
            if (this.retryCount >= this.MAX_RETRIES) {
                console.warn(`WebSocket 已斷線，重連 ${this.MAX_RETRIES} 次後仍失敗，停止重連。`);
                store.appendChatMessage({
                    role: 'system',
                    content: `⚠️ 系統：與後端的連線已中斷，嘗試重連 ${this.MAX_RETRIES} 次後失敗。請重新整理頁面。`
                });
            } else {
                console.log(`WebSocket 斷線，${this.RETRY_DELAY_MS / 1000} 秒後嘗試第 ${this.retryCount} 次重連...`);
                setTimeout(() => this.connect(), this.RETRY_DELAY_MS);
            }
        };

        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
            // 防呆：連線錯誤時確保 AI 打字狀態歸零
            useAppStore.getState().setAiTyping(false);
        };
    }

    public sendMessage(content: string) {
        const store = useAppStore.getState();
        const isConnected = this.ws && this.ws.readyState === WebSocket.OPEN;

        store.appendChatMessage({ role: 'user', content });

        if (isConnected) {
            store.setAiTyping(true);
            this.currentAssistantMessageId = null;
            // 送出訊息前停止當前 TTS 播放
            this.ttsPlayer.stop();
            const payload: Record<string, string> = { content };
            if (this.chatPersistenceEnabled && this.sessionId) {
                payload.session_id = this.sessionId;
            }
            this.ws!.send(JSON.stringify(payload));
        } else {
            console.error('WebSocket is not connected');
            store.appendChatMessage({ role: 'system', content: '系統提示：無法送出訊息，請確認 Python 後端伺服器 (FastAPI) 是否已啟動。' });
            store.setAiTyping(false);
        }
    }

    /**
     * 播放 TTS 語音
     */
    private async playVoice(audioBase64: string, format: string): Promise<void> {
        try {
            console.log(`[TTS] 開始播放語音 | 格式: ${format}`);
            await this.ttsPlayer.play(audioBase64, format);
        } catch (error) {
            console.error('[TTS] 播放失敗:', error);
        }
    }

    /**
     * 停止 TTS 播放
     */
    public stopTTS(): void {
        this.ttsPlayer.stop();
    }
}

export const wsService = new WSService();
