/**
 * TTS 音訊播放器
 * 負責播放後端傳來的 TTS 語音，並同步口型動畫
 */

import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import { LAppPal } from '../live2d/LAppPal';

export class TTSPlayer {
    private static s_instance: TTSPlayer | null = null;
    
    private audioContext: AudioContext | null = null;
    private currentSource: AudioBufferSourceNode | null = null;
    private analyser: AnalyserNode | null = null;
    private isPlaying: boolean = false;
    private animationFrameId: number | null = null;
    
    // 口型同步配置
    private readonly lipSyncConfig = {
        volumeMultiplier: 2.5,    // 音量放大倍數
        smoothingFactor: 0.3,     // 平滑係數（越小越平滑）
        minMouthOpen: 0.0,        // 最小嘴巴開合
        maxMouthOpen: 1.0,        // 最大嘴巴開合
    };
    
    private currentMouthValue: number = 0;
    
    /**
     * 取得單例實例
     */
    public static getInstance(): TTSPlayer {
        if (!this.s_instance) {
            this.s_instance = new TTSPlayer();
        }
        return this.s_instance;
    }
    
    /**
     * 釋放單例實例
     */
    public static releaseInstance(): void {
        if (this.s_instance) {
            this.s_instance.release();
            this.s_instance = null;
        }
    }
    
    private constructor() {
        LAppPal.printLog('[TTSPlayer] 已創建');
    }
    
    /**
     * 確保 AudioContext 已初始化
     */
    private async ensureContext(): Promise<AudioContext> {
        if (!this.audioContext) {
            this.audioContext = new AudioContext();
            LAppPal.printLog('[TTSPlayer] AudioContext 已創建');
        }
        
        // 處理瀏覽器的自動播放限制
        if (this.audioContext.state === 'suspended') {
            await this.audioContext.resume();
            LAppPal.printLog('[TTSPlayer] AudioContext 已恢復');
        }
        
        return this.audioContext;
    }
    
    /**
     * 播放 Base64 編碼的音訊
     * @param audioBase64 Base64 編碼的音訊資料
     * @param format 音訊格式（預設 mp3）
     * @returns Promise，播放完成時 resolve
     */
    public async play(audioBase64: string, _format: string = 'mp3'): Promise<void> {
        // 停止當前播放
        this.stop();
        
        try {
            const context = await this.ensureContext();
            
            // 解碼 Base64
            const binaryString = atob(audioBase64);
            const bytes = new Uint8Array(binaryString.length);
            for (let i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            
            // 解碼音訊資料
            const audioBuffer = await context.decodeAudioData(bytes.buffer.slice(0));
            
            // 建立音訊節點
            this.currentSource = context.createBufferSource();
            this.analyser = context.createAnalyser();
            this.analyser.fftSize = 256;
            this.analyser.smoothingTimeConstant = 0.8;
            
            // 連接節點：source -> analyser -> destination
            this.currentSource.buffer = audioBuffer;
            this.currentSource.connect(this.analyser);
            this.analyser.connect(context.destination);
            
            // 開始口型同步分析
            this.isPlaying = true;
            this.startLipSyncAnalysis();
            
            // 播放
            this.currentSource.start(0);
            
            LAppPal.printLog(`[TTSPlayer] 開始播放 | 時長: ${audioBuffer.duration.toFixed(2)}s`);
            
            // 等待播放完成
            return new Promise((resolve) => {
                if (this.currentSource) {
                    this.currentSource.onended = () => {
                        this.onPlaybackEnded();
                        resolve();
                    };
                } else {
                    resolve();
                }
            });
            
        } catch (error) {
            LAppPal.printError(`[TTSPlayer] 播放失敗: ${error}`);
            this.onPlaybackEnded();
            throw error;
        }
    }
    
    /**
     * 停止播放
     */
    public stop(): void {
        if (this.currentSource) {
            try {
                this.currentSource.stop();
            } catch (e) {
                // 已經停止，忽略錯誤
            }
            this.currentSource.disconnect();
            this.currentSource = null;
        }
        
        if (this.animationFrameId !== null) {
            cancelAnimationFrame(this.animationFrameId);
            this.animationFrameId = null;
        }
        
        this.isPlaying = false;
        this.updateMouthValue(0);
        
        LAppPal.printLog('[TTSPlayer] 已停止');
    }
    
    /**
     * 是否正在播放
     */
    public getIsPlaying(): boolean {
        return this.isPlaying;
    }
    
    /**
     * 開始口型同步分析
     */
    private startLipSyncAnalysis(): void {
        if (!this.analyser || !this.isPlaying) return;
        
        const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        
        const analyze = () => {
            if (!this.isPlaying || !this.analyser) {
                return;
            }
            
            // 取得頻率資料
            this.analyser.getByteFrequencyData(dataArray);
            
            // 計算平均音量（RMS）
            // 主要關注人聲頻率範圍（約 85-255 Hz，對應 index 1-10）
            let sum = 0;
            const startBin = 1;
            const endBin = Math.min(20, dataArray.length);
            
            for (let i = startBin; i < endBin; i++) {
                sum += dataArray[i] * dataArray[i];
            }
            
            const rms = Math.sqrt(sum / (endBin - startBin));
            
            // 映射到 0-1 範圍
            const normalizedVolume = Math.min(1.0, (rms / 128) * this.lipSyncConfig.volumeMultiplier);
            
            // 平滑過渡
            const targetMouth = Math.max(
                this.lipSyncConfig.minMouthOpen,
                Math.min(this.lipSyncConfig.maxMouthOpen, normalizedVolume)
            );
            
            this.currentMouthValue += (targetMouth - this.currentMouthValue) * this.lipSyncConfig.smoothingFactor;
            
            // 更新模型口型
            this.updateMouthValue(this.currentMouthValue);
            
            // 繼續下一幀
            this.animationFrameId = requestAnimationFrame(analyze);
        };
        
        analyze();
    }
    
    /**
     * 更新模型口型
     */
    private updateMouthValue(value: number): void {
        const manager = LAppLive2DManager.getInstance();
        const model = manager.getActiveModel();
        
        if (model) {
            // 使用現有的 setLipSyncValue 方法
            model.setLipSyncValue(value);
        }
    }
    
    /**
     * 播放結束時的處理
     */
    private onPlaybackEnded(): void {
        this.isPlaying = false;
        
        if (this.animationFrameId !== null) {
            cancelAnimationFrame(this.animationFrameId);
            this.animationFrameId = null;
        }
        
        // 平滑閉嘴
        this.smoothCloseMouth();
        
        LAppPal.printLog('[TTSPlayer] 播放結束');
    }
    
    /**
     * 平滑閉嘴動畫
     */
    private smoothCloseMouth(): void {
        const closeMouth = () => {
            if (this.currentMouthValue > 0.01) {
                this.currentMouthValue *= 0.85; // 快速衰減
                this.updateMouthValue(this.currentMouthValue);
                requestAnimationFrame(closeMouth);
            } else {
                this.currentMouthValue = 0;
                this.updateMouthValue(0);
            }
        };
        
        closeMouth();
    }
    
    /**
     * 釋放資源
     */
    public release(): void {
        this.stop();
        
        if (this.analyser) {
            this.analyser.disconnect();
            this.analyser = null;
        }
        
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }
        
        LAppPal.printLog('[TTSPlayer] 資源已釋放');
    }
}
