import { useAppStore } from '../store/appStore';
import { TTSPlayer } from '../audio/TTSPlayer';
import { isBlinkAction, isExpressionPlanPayload } from '../types/expressionPlan';
import { isEmotionUpdatePayload } from '../types/emotionState';
import { actionScheduler } from './actionScheduler';

class WSService {
    private ws: WebSocket | null = null;
    private currentAssistantMessageId: string | null = null;
    private readonly assistantMessageIds = new Map<string, string>();
    private activeTurnId: string | null = null;
    private activeTurnStartedAt: number | null = null;
    private appliedPlanTurnId: string | null = null;
    private retryCount: number = 0;
    private readonly MAX_RETRIES = 5;
    private readonly RETRY_DELAY_MS = 3000;
    private sessionId: string | null = null;
    
    // TTS 播放器
    private ttsPlayer: TTSPlayer;

    constructor() {
        this.ttsPlayer = TTSPlayer.getInstance();
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
            this.ws?.send(JSON.stringify({ type: 'sync' }));
            actionScheduler.setReporter((status, action) => {
                if (this.ws?.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({ type: 'action_state', status,
                        action_id: action.id, turn_id: action.turnId, stage: action.stage }));
                }
            });
        };

        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                const store = useAppStore.getState();
                if (data.type === 'session_ready') {
                    this.sessionId = typeof data.session_id === 'string' ? data.session_id : null;
                    return;
                }
                if (data.type === 'turn_cancelled') {
                    const cancelledTurnId = typeof data.turn_id === 'string' ? data.turn_id : null;
                    if (cancelledTurnId) {
                        const messageId = this.assistantMessageIds.get(cancelledTurnId);
                        if (messageId && data.status === 'interrupted') {
                            store.updateChatMessageStatus(messageId, 'interrupted');
                        }
                        this.assistantMessageIds.delete(cancelledTurnId);
                        if (cancelledTurnId === this.activeTurnId) {
                            actionScheduler.cancelTurn(cancelledTurnId);
                            this.ttsPlayer.stop();
                            this.currentAssistantMessageId = null;
                            this.activeTurnStartedAt = null;
                            store.setAiTyping(false);
                        }
                    }
                    return;
                }
                if (typeof data.turn_id === 'string' && data.turn_id !== this.activeTurnId
                    && data.type !== 'memory_status') return;
                if (data.speech_unavailable === true && typeof data.turn_id === 'string') {
                    actionScheduler.skipSpeechPlan(data.turn_id);
                }

                if (data.type === 'text_stream') {
                    if (store.chatPerformance.firstTokenLatencyMs === null && this.activeTurnStartedAt !== null) {
                        store.setChatPerformance({
                            firstTokenLatencyMs: performance.now() - this.activeTurnStartedAt,
                        });
                    }
                    if (!this.currentAssistantMessageId) {
                        this.currentAssistantMessageId = store.appendChatMessage({ role: 'assistant', content: data.content });
                        if (typeof data.turn_id === 'string') {
                            this.assistantMessageIds.set(data.turn_id, this.currentAssistantMessageId);
                        }
                    } else {
                        const currentMsg = useAppStore.getState().chatHistory.find(m => m.id === this.currentAssistantMessageId);
                        if (currentMsg) {
                            store.updateChatMessage(this.currentAssistantMessageId, currentMsg.content + data.content);
                        }
                    }
                } else if (data.type === 'behavior') {
                    if (this.appliedPlanTurnId === this.activeTurnId) return;
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
                        data.eyeSync ?? true,
                        data.eyeLSmile ?? 0.0,
                        data.eyeRSmile ?? 0.0,
                        data.browLX ?? 0.0,
                        data.browRX ?? 0.0,
                        data.bodyAngleX ?? 0.0,
                        data.bodyAngleY ?? 0.0,
                        data.bodyAngleZ ?? 0.0,
                        data.breathLevel ?? 0.35,
                        data.physicsImpulse ?? 0.0,
                    );
                } else if (data.type === 'blink_control') {
                    if (this.appliedPlanTurnId === this.activeTurnId) return;
                    console.log(`Received blink control: action=${data.action}, duration=${data.durationSec}`);
                    if (isBlinkAction(data.action)) {
                        store.setBlinkControl(
                            data.action,
                            data.durationSec ?? 0,
                            data.intervalMin,
                            data.intervalMax
                        );
                    }
                } else if (data.type === 'expression_plan') {
                    if (!isExpressionPlanPayload(data)) {
                        console.warn('Received invalid expression_plan payload:', data);
                        return;
                    }

                    const plan = data;

                    actionScheduler.submit(plan, 'chat', data.turn_id);
                    this.appliedPlanTurnId = this.activeTurnId;
                } else if (data.type === 'stream_end') {
                    if (typeof data.turn_id === 'string') {
                        this.assistantMessageIds.delete(data.turn_id);
                    }
                    this.currentAssistantMessageId = null;
                    const metrics = data.metrics;
                    if (metrics && typeof metrics === 'object') {
                        store.setChatPerformance({
                            firstTokenLatencyMs: typeof metrics.first_token_latency_ms === 'number'
                                ? metrics.first_token_latency_ms : store.chatPerformance.firstTokenLatencyMs,
                            tokensPerSecond: typeof metrics.tokens_per_second === 'number'
                                ? metrics.tokens_per_second : null,
                            outputTokens: typeof metrics.output_tokens === 'number'
                                ? metrics.output_tokens : null,
                        });
                    }
                    this.activeTurnStartedAt = null;
                    store.setAiTyping(false);
                    if (typeof data.turn_id === 'string') {
                        actionScheduler.completeText(data.turn_id, data.voice_expected === true, data.speech_expected === true);
                    }
                } else if (data.type === 'voice') {
                    // TTS 語音播放
                    void this.playVoice(data.audio, data.format || 'wav', data.turn_id);
                } else if (data.type === 'voice_unavailable') {
                    if (typeof data.turn_id === 'string') actionScheduler.completeVoice(data.turn_id);
                } else if (data.type === 'compressing') {
                    store.setCompressing(true);
                } else if (data.type === 'compress_done') {
                    store.setCompressing(false);
                } else if (data.type === 'memory_status') {
                    if (typeof data.content === 'string' && data.content.trim()) {
                        store.appendChatMessage({ role: 'system', content: data.content });
                    }
                } else if (data.type === 'emotion_update') {
                    if (isEmotionUpdatePayload(data)) {
                        store.setEmotionState(data.state, data.source);
                    } else {
                        console.warn('Received invalid emotion_update payload:', data);
                    }
                } else if (data.type === 'error') {
                    if (this.activeTurnId) actionScheduler.cancelTurn(this.activeTurnId);
                    this.ttsPlayer.stop();
                    store.appendChatMessage({ role: 'system', content: data.content });
                    store.setAiTyping(false);
                    this.currentAssistantMessageId = null;
                }
            } catch (e) {
                console.error('WebSocket message parsing error:', e);
            }
        };

        this.ws.onclose = () => {
            actionScheduler.cancel();
            this.ttsPlayer.stop();
            actionScheduler.setReporter(null);
            this.ws = null;
            this.currentAssistantMessageId = null;
            this.assistantMessageIds.clear();
            this.activeTurnId = null;
            this.activeTurnStartedAt = null;
            this.appliedPlanTurnId = null;
            this.sessionId = null;
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

    public sendMessage(content: string, source: 'text' | 'voice' = 'text') {
        const store = useAppStore.getState();
        const isConnected = this.ws && this.ws.readyState === WebSocket.OPEN;

        store.appendChatMessage({ role: 'user', content });
        store.clearChatPerformance();

        if (isConnected) {
            store.setAiTyping(true);
            this.currentAssistantMessageId = null;
            this.activeTurnId = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
                ? crypto.randomUUID() : `turn_${Date.now()}_${Math.random().toString(36).slice(2)}`;
            actionScheduler.beginTurn(this.activeTurnId);
            this.activeTurnStartedAt = performance.now();
            this.appliedPlanTurnId = null;
            // 送出訊息前停止當前 TTS 播放
            this.ttsPlayer.stop();
            const payload: Record<string, string> = {
                content,
                model_name: store.currentModelName,
                turn_id: this.activeTurnId,
                source,
            };
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
    private async playVoice(audioBase64: string, format: string, turnId?: string): Promise<void> {
        try {
            console.log(`[TTS] 開始播放語音 | 格式: ${format}`);
            await this.ttsPlayer.play(audioBase64, format, clock => {
                if (turnId) actionScheduler.startVoice(turnId, clock);
            });
        } catch (error) {
            console.error('[TTS] 播放失敗:', error);
        } finally {
            if (turnId) actionScheduler.completeVoice(turnId);
        }
    }

    /**
     * 停止 TTS 播放
     */
    public stopTTS(): void {
        this.ttsPlayer.stop();
    }

    /** REST owner reset 會使後端關閉舊寫入連線；此處只清理前端回合狀態。 */
    public syncResetSession(): void {
        actionScheduler.cancel();
        this.activeTurnId = null;
        this.activeTurnStartedAt = null;
        this.currentAssistantMessageId = null;
        this.assistantMessageIds.clear();
        this.appliedPlanTurnId = null;
        this.ttsPlayer.stop();
        useAppStore.getState().clearChatPerformance();
    }

    public getSessionId(): string | null {
        return this.sessionId;
    }
}

export const wsService = new WSService();
