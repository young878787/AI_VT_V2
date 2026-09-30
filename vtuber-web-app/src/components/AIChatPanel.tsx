import React, { useState, useEffect, useRef } from 'react';
import { useAppStore } from '@store/appStore';
import { wsService } from '../services/wsService';
import './AIChatPanel.css';

export const AIChatPanel = () => {
    const { chatHistory, isAiTyping, isCompressing } = useAppStore();
    const [inputValue, setInputValue] = useState('');
    const messagesEndRef = useRef<HTMLDivElement>(null);

    // Initialize WebSocket connection
    useEffect(() => {
        wsService.connect();
    }, []);

    // Auto-scroll to bottom of chat
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [chatHistory, isAiTyping]);

    const handleSend = () => {
        if (!inputValue.trim()) return;
        wsService.sendMessage(inputValue);
        setInputValue('');
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter') {
            handleSend();
        }
    };

    return (
        <div className="ai-chat-panel">
            <div className="ai-chat-panel__header">
                <h3>AI 助理對話</h3>
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
                <div className="input-area">
                    <input
                        type="text"
                        placeholder="與 AI 助理對話..."
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
            </div>
        </div>
    );
};
