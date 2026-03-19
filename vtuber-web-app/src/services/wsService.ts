import { useAppStore } from '../store/appStore';

class WSService {
    private ws: WebSocket | null = null;
    private currentAssistantMessageId: string | null = null;

    public connect() {
        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;

        this.ws = new WebSocket('ws://localhost:8000/ws/chat');

        this.ws.onopen = () => {
            console.log('WebSocket connected');
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
                } else if (data.type === 'compressing') {
                    store.setCompressing(true);
                } else if (data.type === 'compress_done') {
                    store.setCompressing(false);
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
            console.log('WebSocket disconnected. Reconnecting...');
            this.ws = null;
            setTimeout(() => this.connect(), 3000);
        };

        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };
    }

    public sendMessage(content: string) {
        const store = useAppStore.getState();
        const isConnected = this.ws && this.ws.readyState === WebSocket.OPEN;

        store.appendChatMessage({ role: 'user', content });

        if (isConnected) {
            store.setAiTyping(true);
            this.currentAssistantMessageId = null;
            this.ws!.send(JSON.stringify({ content }));
        } else {
            console.error('WebSocket is not connected');
            store.appendChatMessage({ role: 'system', content: '💬 系統提示：無法送出訊息，請確認 Python 後端伺服器 (FastAPI) 是否已啟動。' });
            store.setAiTyping(false);
        }
    }
}

export const wsService = new WSService();
