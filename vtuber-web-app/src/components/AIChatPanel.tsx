import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useAppStore } from '@store/appStore';
import { MicrophoneManager } from '../audio/MicrophoneManager';
import { voiceWsService } from '../services/voiceWsService';
import { wsService } from '../services/wsService';
import './AIChatPanel.css';

type VoiceCaptureState = 'idle' | 'connecting' | 'permission' | 'starting' | 'listening' | 'processing';

const NO_SPEECH_TIMEOUT_MS = 4000;
const MAX_CAPTURE_TIME_MS = 20000;

const formatMilliseconds = (value: number | null) =>
    value === null ? '--' : `${Math.round(value)}`;

const formatTokensPerSecond = (value: number | null) =>
    value === null ? '--' : value.toFixed(1);

export const AIChatPanel = () => {
    const {
        chatHistory,
        isAiTyping,
        isCompressing,
        chatPerformance,
        microphonePermission,
        setMicrophonePermission,
    } = useAppStore();
    const [inputValue, setInputValue] = useState('');
    const [voiceState, setVoiceState] = useState<VoiceCaptureState>('idle');
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const voiceCaptureRef = useRef(false);
    const voiceMonitorRef = useRef<number | null>(null);
    const noSpeechTimerRef = useRef<number | null>(null);
    const maxCaptureTimerRef = useRef<number | null>(null);
    const voiceHasSoundRef = useRef(false);

    useEffect(() => {
        wsService.connect();
    }, []);

    const stopVoiceCapture = useCallback(() => {
        voiceCaptureRef.current = false;
        voiceHasSoundRef.current = false;
        if (voiceMonitorRef.current !== null) {
            cancelAnimationFrame(voiceMonitorRef.current);
            voiceMonitorRef.current = null;
        }
        if (noSpeechTimerRef.current !== null) {
            window.clearTimeout(noSpeechTimerRef.current);
            noSpeechTimerRef.current = null;
        }
        if (maxCaptureTimerRef.current !== null) {
            window.clearTimeout(maxCaptureTimerRef.current);
            maxCaptureTimerRef.current = null;
        }

        voiceWsService.setGate(false);
        voiceWsService.disconnect();
        const microphone = MicrophoneManager.getInstance();
        microphone.stopVoiceStreaming();
        if (!useAppStore.getState().microphoneEnabled) microphone.disable();
        setVoiceState('idle');
    }, []);

    useEffect(() => {
        const removeHandlers = voiceWsService.setHandlers({
            onState: (state) => {
                if (!voiceCaptureRef.current) return;
                if (state === 'processing') setVoiceState('processing');
                else if (state === 'listening') setVoiceState('listening');
            },
            onFinal: (text) => {
                if (!voiceCaptureRef.current) return;
                stopVoiceCapture();
                wsService.sendMessage(text, 'voice');
            },
            onError: (message) => {
                if (!voiceCaptureRef.current) return;
                stopVoiceCapture();
                useAppStore.getState().appendChatMessage({
                    role: 'system',
                    content: `⚠️ 語音收音：${message}`,
                });
            },
        });

        return () => {
            removeHandlers();
            stopVoiceCapture();
        };
    }, [stopVoiceCapture]);

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [chatHistory, isAiTyping]);

    const handleSend = () => {
        const content = inputValue.trim();
        if (!content) return;
        stopVoiceCapture();
        wsService.sendMessage(content);
        setInputValue('');
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter') {
            handleSend();
        }
    };

    const handleVoiceCapture = useCallback(async () => {
        if (voiceCaptureRef.current) {
            stopVoiceCapture();
            return;
        }

        voiceCaptureRef.current = true;
        voiceHasSoundRef.current = false;
        setVoiceState('connecting');

        try {
            await voiceWsService.connect();
        } catch (error) {
            if (!voiceCaptureRef.current) return;
            stopVoiceCapture();
            useAppStore.getState().appendChatMessage({
                role: 'system',
                content: `⚠️ 語音收音：${error instanceof Error ? error.message : '無法連線到語音服務'}`,
            });
            return;
        }
        if (!voiceCaptureRef.current) return;

        setVoiceState('permission');
        const microphone = MicrophoneManager.getInstance();
        const enabled = await microphone.enable();
        if (!voiceCaptureRef.current) {
            if (!useAppStore.getState().microphoneEnabled) microphone.disable();
            return;
        }
        if (!enabled) {
            setMicrophonePermission('denied');
            stopVoiceCapture();
            return;
        }
        setMicrophonePermission('granted');

        setVoiceState('starting');
        const streaming = await microphone.startVoiceStreaming((buffer) => {
            voiceWsService.sendAudioFrame(buffer);
        });
        if (!voiceCaptureRef.current) {
            microphone.stopVoiceStreaming();
            if (!useAppStore.getState().microphoneEnabled) microphone.disable();
            return;
        }
        if (!streaming) {
            stopVoiceCapture();
            useAppStore.getState().appendChatMessage({
                role: 'system',
                content: '⚠️ 語音收音啟動失敗，請稍後再試。',
            });
            return;
        }

        voiceWsService.setGate(true);
        setVoiceState('listening');

        noSpeechTimerRef.current = window.setTimeout(() => {
            if (!voiceHasSoundRef.current) stopVoiceCapture();
        }, NO_SPEECH_TIMEOUT_MS);
        maxCaptureTimerRef.current = window.setTimeout(() => {
            stopVoiceCapture();
        }, MAX_CAPTURE_TIME_MS);

        const monitorVolume = () => {
            if (!voiceCaptureRef.current) return;
            const analysis = microphone.analyze();
            if (analysis.isSpeaking || analysis.rawVolume > 0.012) {
                voiceHasSoundRef.current = true;
                if (noSpeechTimerRef.current !== null) {
                    window.clearTimeout(noSpeechTimerRef.current);
                    noSpeechTimerRef.current = null;
                }
            }
            voiceMonitorRef.current = requestAnimationFrame(monitorVolume);
        };
        voiceMonitorRef.current = requestAnimationFrame(monitorVolume);
    }, [setMicrophonePermission, stopVoiceCapture]);

    return (
        <div className="ai-chat-panel">
            <div className="ai-chat-panel__header">
                <h3>與露西亞對話</h3>
                <div className="chat-performance" aria-label="本次回覆效能">
                    <span>速率 {formatTokensPerSecond(chatPerformance.tokensPerSecond)} tok/s</span>
                    <span>首字 {formatMilliseconds(chatPerformance.firstTokenLatencyMs)} ms</span>
                </div>
            </div>
            <div className="ai-chat-panel__content">
                <div className="chat-history">
                    {chatHistory.map((msg) => (
                        <div key={msg.id} className={`chat-message ${msg.role}`}>
                            {msg.role === 'system' && <span className="icon">🤖</span>}
                            <div className="message-content">
                                {msg.content}
                                {msg.status === 'interrupted' && <span className="message-status">（已中斷）</span>}
                            </div>
                        </div>
                    ))}
                    {isAiTyping && (
                        <div className="chat-message assistant typing">
                            <div className="message-content">
                                <span className="dot"></span>
                                <span className="dot"></span>
                                <span className="dot"></span>
                            </div>
                        </div>
                    )}
                    {isCompressing && (
                        <div className="chat-message system compressing">
                            <div className="message-content">
                                <span className="compress-icon">&#x2699;</span>
                                記憶整理中...
                            </div>
                        </div>
                    )}
                    <div ref={messagesEndRef} />
                </div>
            </div>
            <div className="ai-chat-panel__footer">
                <p className="context-note">露西亞會按對話需要查詢現況、臺灣天氣或擷取單張畫面；本機應用與畫面資料會傳給聊天模型，不持續監控。</p>
                <div className="input-area">
                    <button
                        type="button"
                        className={`voice-button ${voiceState !== 'idle' ? 'active' : ''}`}
                        onClick={() => void handleVoiceCapture()}
                        disabled={microphonePermission === 'denied'}
                        aria-label={voiceState !== 'idle' ? '停止本次收音' : '開始單次收音'}
                        title={voiceState !== 'idle' ? '停止本次收音' : '單次收音'}
                    >
                        {voiceState !== 'idle' ? '■' : '🎤'}
                    </button>
                    <input
                        type="text"
                        placeholder="想對露西亞說些什麼…"
                        value={inputValue}
                        onChange={(e) => setInputValue(e.target.value)}
                        onKeyDown={handleKeyDown}
                        className="chat-input"
                    />
                    <button
                        className="send-button"
                        onClick={handleSend}
                        disabled={!inputValue.trim()}
                    >
                        發送
                    </button>
                </div>
                {voiceState !== 'idle' && (
                    <div className="voice-status" role="status" aria-live="polite">
                        {voiceState === 'connecting' ? '正在連接語音服務…' :
                            voiceState === 'permission' ? '正在等待麥克風權限…' :
                            voiceState === 'starting' ? '正在啟動麥克風…' :
                            voiceState === 'processing' ? '正在辨識這段聲音…' : '請說話；沒有聲音會自動關閉'}
                    </div>
                )}
            </div>
        </div>
    );
};
