/**
 * 麥克風輸入管理器
 * 使用 Web Audio API 捕獲麥克風音訊並分析音量
 */

import { LAppPal } from '../live2d/LAppPal';

/**
 * 音訊分析結果
 */
export interface AudioAnalysisResult {
  volume: number;           // 音量 (0.0 - 1.0)
  rawVolume: number;        // 原始音量值
  isSpeaking: boolean;      // 是否正在說話
  frequency?: number;       // 主要頻率（可選）
}

/**
 * 麥克風輸入配置
 */
export interface MicrophoneConfig {
  sampleRate?: number;      // 採樣率（預設 48000）
  fftSize?: number;         // FFT 大小（預設 256）
  smoothingFactor?: number; // 平滑係數（0-1，預設 0.8）
  volumeThreshold?: number; // 音量閾值（預設 0.01）
  volumeScale?: number;     // 音量縮放係數（預設 2.0）
}

/**
 * 麥克風輸入管理器
 */
export class MicrophoneManager {
  private static s_instance: MicrophoneManager | null = null;

  private _audioContext: AudioContext | null = null;
  private _mediaStream: MediaStream | null = null;
  private _sourceNode: MediaStreamAudioSourceNode | null = null;
  private _analyserNode: AnalyserNode | null = null;
  private _dataArray: Uint8Array | null = null;
  
  private _isInitialized: boolean = false;
  private _isEnabled: boolean = false;
  private _volume: number = 0;
  private _smoothedVolume: number = 0;
  
  private _config: Required<MicrophoneConfig>;

  /**
   * 取得單例實例
   */
  public static getInstance(): MicrophoneManager {
    if (!this.s_instance) {
      this.s_instance = new MicrophoneManager();
    }
    return this.s_instance;
  }

  /**
   * 釋放單例實例
   */
  public static releaseInstance(): void {
    if (this.s_instance) {
      this.s_instance.release();
    }
    this.s_instance = null;
  }

  private constructor() {
    this._config = {
      sampleRate: 48000,
      fftSize: 256,
      smoothingFactor: 0.8,
      volumeThreshold: 0.01,
      volumeScale: 2.0,
    };
    LAppPal.printLog('MicrophoneManager 已創建');
  }

  /**
   * 初始化麥克風
   */
  public async initialize(config?: MicrophoneConfig): Promise<boolean> {
    if (this._isInitialized) {
      LAppPal.printLog('麥克風已經初始化');
      return true;
    }

    // 合併配置
    if (config) {
      this._config = { ...this._config, ...config };
    }

    try {
      // 檢查瀏覽器支援
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error('瀏覽器不支援 getUserMedia API');
      }

      // 請求麥克風權限
      LAppPal.printLog('正在請求麥克風權限...');
      this._mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });

      LAppPal.printLog('麥克風權限已獲取');

      // 創建 AudioContext
      this._audioContext = new AudioContext({
        sampleRate: this._config.sampleRate,
      });

      // 創建音訊處理節點
      this._sourceNode = this._audioContext.createMediaStreamSource(this._mediaStream);
      this._analyserNode = this._audioContext.createAnalyser();
      this._analyserNode.fftSize = this._config.fftSize;
      this._analyserNode.smoothingTimeConstant = this._config.smoothingFactor;

      // 連接節點（不連接到 destination 以避免回授）
      this._sourceNode.connect(this._analyserNode);

      // 創建數據陣列
      this._dataArray = new Uint8Array(this._analyserNode.frequencyBinCount);

      this._isInitialized = true;
      LAppPal.printLog('麥克風初始化完成');
      return true;

    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : '未知錯誤';
      LAppPal.printError(`麥克風初始化失敗: ${errorMessage}`);
      return false;
    }
  }

  /**
   * 啟用麥克風輸入
   */
  public async enable(): Promise<boolean> {
    if (!this._isInitialized) {
      const success = await this.initialize();
      if (!success) return false;
    }

    // 如果 AudioContext 被暫停，恢復它
    if (this._audioContext && this._audioContext.state === 'suspended') {
      await this._audioContext.resume();
    }

    this._isEnabled = true;
    LAppPal.printLog('麥克風已啟用');
    return true;
  }

  /**
   * 禁用麥克風輸入
   */
  public disable(): void {
    this._isEnabled = false;
    this._volume = 0;
    this._smoothedVolume = 0;
    LAppPal.printLog('麥克風已禁用');
  }

  /**
   * 是否已啟用
   */
  public isEnabled(): boolean {
    return this._isEnabled;
  }

  /**
   * 是否已初始化
   */
  public isInitialized(): boolean {
    return this._isInitialized;
  }

  /**
   * 分析當前音訊並返回結果
   */
  public analyze(): AudioAnalysisResult {
    if (!this._isEnabled || !this._analyserNode || !this._dataArray) {
      return {
        volume: 0,
        rawVolume: 0,
        isSpeaking: false,
      };
    }

    // 獲取時域數據
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    this._analyserNode.getByteTimeDomainData(this._dataArray as any);

    // 計算音量（RMS）
    let sum = 0;
    for (let i = 0; i < this._dataArray.length; i++) {
      const normalized = (this._dataArray[i] - 128) / 128;
      sum += normalized * normalized;
    }
    const rms = Math.sqrt(sum / this._dataArray.length);
    
    // 應用縮放和閾值
    this._volume = Math.min(1.0, rms * this._config.volumeScale);
    
    // 平滑處理
    this._smoothedVolume = 
      this._smoothedVolume * this._config.smoothingFactor + 
      this._volume * (1 - this._config.smoothingFactor);

    // 判斷是否正在說話
    const isSpeaking = this._smoothedVolume > this._config.volumeThreshold;

    return {
      volume: this._smoothedVolume,
      rawVolume: rms,
      isSpeaking,
    };
  }

  /**
   * 取得當前音量（已平滑）
   */
  public getVolume(): number {
    return this._smoothedVolume;
  }

  /**
   * 取得當前配置
   */
  public getConfig(): Required<MicrophoneConfig> {
    return { ...this._config };
  }

  /**
   * 更新配置
   */
  public updateConfig(config: Partial<MicrophoneConfig>): void {
    this._config = { ...this._config, ...config };
    
    // 更新 analyser 設置
    if (this._analyserNode) {
      if (config.fftSize) {
        this._analyserNode.fftSize = config.fftSize;
        this._dataArray = new Uint8Array(this._analyserNode.frequencyBinCount);
      }
      if (config.smoothingFactor !== undefined) {
        this._analyserNode.smoothingTimeConstant = config.smoothingFactor;
      }
    }
  }

  /**
   * 釋放資源
   */
  public release(): void {
    this.disable();

    if (this._sourceNode) {
      this._sourceNode.disconnect();
      this._sourceNode = null;
    }

    if (this._analyserNode) {
      this._analyserNode.disconnect();
      this._analyserNode = null;
    }

    if (this._mediaStream) {
      this._mediaStream.getTracks().forEach(track => track.stop());
      this._mediaStream = null;
    }

    if (this._audioContext) {
      this._audioContext.close();
      this._audioContext = null;
    }

    this._dataArray = null;
    this._isInitialized = false;

    LAppPal.printLog('麥克風資源已釋放');
  }
}
