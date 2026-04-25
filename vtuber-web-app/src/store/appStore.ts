/**
 * 應用程式狀態管理 - 使用 Zustand
 */
import { create } from 'zustand';
import { AvailableModels, type ModelConfig } from '../live2d/LAppDefine';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import { fetchAvailableModels, type RemoteModelConfig } from '../services/modelService';
import type { BlinkAction, ExpressionMicroEvent, ExpressionPlanPayload } from '../types/expressionPlan';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface JPAFState {
  persona: string;
  dominant: string;
  auxiliary: string;
  baseWeights: Record<string, number>;
  turnCount: number;
}

interface AiBehaviorBridgeModel {
  setAiBehavior?: (headIntensity: number, blushLevel: number, eyeLOpen: number, eyeROpen: number, durationSec?: number, mouthForm?: number, browLY?: number, browRY?: number, browLAngle?: number, browRAngle?: number, browLForm?: number, browRForm?: number, eyeSync?: boolean, eyeLSmile?: number, eyeRSmile?: number, browLX?: number, browRX?: number) => void;
  setAiHappiness?: (headIntensity: number, durationSec?: number) => void;
  forceBlink?: (durationSec?: number) => void;
  pauseAutoBlink?: (durationSec?: number) => void;
  resumeAutoBlink?: () => void;
  setBlinkInterval?: (intervalMin: number, intervalMax: number) => void;
  applyBasePose?: (basePose: ExpressionPlanPayload['basePose']) => void;
  enqueueMicroEvent?: (event: ExpressionMicroEvent) => void;
}

interface AppState {
  // 麥克風狀態
  microphoneEnabled: boolean;
  microphonePermission: 'granted' | 'denied' | 'prompt';

  // 模型載入狀態
  modelLoading: boolean;
  modelLoaded: boolean;
  modelError: string | null;

  // 模型管理狀態
  currentModelName: string;               // 當前選中的模型名稱
  availableModels: ModelConfig[];         // 可用的模型列表
  modelSwitching: boolean;                // 是否正在切換模型

  // 視線追蹤狀態
  eyeTrackingEnabled: boolean;

  // 自動播放動作狀態
  autoPlayEnabled: boolean;

  // 模型變換狀態
  modelDragEnabled: boolean;
  modelScale: number;

  // UI 狀態
  showControls: boolean;

  // Hit Area 調試狀態
  hitAreaDebug: boolean;

  // AI 聊天室與動作控制狀態
  chatHistory: ChatMessage[];
  isAiTyping: boolean;
  isCompressing: boolean;
  aiBehavior: {
    headIntensity: number;
    blushLevel: number;
    eyeLOpen: number;
    eyeROpen: number;
  };
  expressionPlan: ExpressionPlanPayload | null;
  expressionEvents: ExpressionMicroEvent[];

  // 動作
  toggleMicrophone: () => void;
  setMicrophonePermission: (permission: 'granted' | 'denied' | 'prompt') => void;
  setModelLoading: (loading: boolean) => void;
  setModelLoaded: (loaded: boolean) => void;
  setModelError: (error: string | null) => void;
  toggleEyeTracking: () => void;
  toggleAutoPlay: () => void;
  toggleControls: () => void;

  // 聊天與情緒控制
  appendChatMessage: (message: Omit<ChatMessage, 'id'>) => string;
  updateChatMessage: (id: string, content: string) => void;
  setAiTyping: (isTyping: boolean) => void;
  setCompressing: (isCompressing: boolean) => void;
  setAiBehavior: (headIntensity: number, blushLevel: number, eyeLOpen: number, eyeROpen: number, durationSec?: number, mouthForm?: number, browLY?: number, browRY?: number, browLAngle?: number, browRAngle?: number, browLForm?: number, browRForm?: number, eyeSync?: boolean, eyeLSmile?: number, eyeRSmile?: number, browLX?: number, browRX?: number) => void;
  setBlinkControl: (action: BlinkAction, durationSec?: number, intervalMin?: number, intervalMax?: number) => void;
  setExpressionPlan: (plan: ExpressionPlanPayload) => void;
  enqueueExpressionEvents: (events: ExpressionMicroEvent[]) => void;
  clearExpressionEvents: () => void;

  // 模型變換動作
  toggleModelDrag: () => void;
  setModelScale: (scale: number) => void;
  scaleModelUp: () => void;
  scaleModelDown: () => void;
  resetModelTransform: () => void;

  // Hit Area 調試
  toggleHitAreaDebug: () => void;

  // 模型管理動作
  setCurrentModelName: (name: string) => void;
  setModelSwitching: (switching: boolean) => void;
  getCurrentModelConfig: () => ModelConfig | undefined;

  // JPAF 狀態
  jpafState: JPAFState | null;
  setJpafState: (state: JPAFState) => void;
  clearChatHistory: () => void;

  // 動態模型清單管理
  loadAvailableModels: () => Promise<void>;
  addImportedModel: (model: RemoteModelConfig) => void;
  removeModel: (name: string) => void;
}

export const useAppStore = create<AppState>((set, get) => ({
  // 初始狀態
  microphoneEnabled: false,
  microphonePermission: 'prompt',
  modelLoading: false,
  modelLoaded: false,
  modelError: null,
  eyeTrackingEnabled: true,
  autoPlayEnabled: false,
  showControls: true,

  // 模型變換初始狀態
  modelDragEnabled: false,
  modelScale: 1.0,

  // Hit Area 調試初始狀態
  hitAreaDebug: false,

  // 日式動漫 AI 初始狀態
  chatHistory: [
    {
      id: "system-init",
      role: 'system',
      content: '系統：AI 對話模組準備就緒。請確保 Python 後端已經啟動。(uvicorn main:app)'
    }
  ],
  isAiTyping: false,
  isCompressing: false,
  aiBehavior: {
    headIntensity: 0,
    blushLevel: 0,
    eyeLOpen: 1,
    eyeROpen: 1
  },
  expressionPlan: null,
  expressionEvents: [],

  // JPAF 初始狀態
  jpafState: null,

  // 模型管理初始狀態
  currentModelName: AvailableModels[0]?.name || 'Hiyori',
  availableModels: AvailableModels,
  modelSwitching: false,

  // 動作實作
  toggleMicrophone: () =>
    set((state) => ({
      microphoneEnabled: !state.microphoneEnabled
    })),

  setMicrophonePermission: (permission) =>
    set({ microphonePermission: permission }),

  setModelLoading: (loading) =>
    set({ modelLoading: loading }),

  setModelLoaded: (loaded) =>
    set({ modelLoaded: loaded }),

  setModelError: (error) =>
    set({ modelError: error }),

  toggleEyeTracking: () =>
    set((state) => ({
      eyeTrackingEnabled: !state.eyeTrackingEnabled
    })),

  toggleAutoPlay: () => {
    const newState = !get().autoPlayEnabled;
    set({ autoPlayEnabled: newState });
    
    // 同步到模型
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      model.setAutoEffectsEnabled(newState);
    }
  },

  toggleControls: () =>
    set((state) => ({
      showControls: !state.showControls
    })),

  // 模型變換動作實作
  toggleModelDrag: () =>
    set((state) => ({
      modelDragEnabled: !state.modelDragEnabled,
      // 開啟拖移模式時，暂停視線追蹤避免衝突
      eyeTrackingEnabled: state.modelDragEnabled ? state.eyeTrackingEnabled : false
    })),

  setModelScale: (scale: number) => {
    // 限制縮放範圍從 0.1 到 500 倍
    const clampedScale = Math.max(0.1, Math.min(500.0, scale));
    set({ modelScale: clampedScale });

    // 同步到模型
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      model.setModelScale(clampedScale);
    }
  },

  scaleModelUp: () => {
    const currentScale = get().modelScale;
    // 使用乘法來實現平滑且能快速抵達 500 的縮放
    const delta = currentScale < 1.0 ? 0.1 : (currentScale * 0.1);
    get().setModelScale(currentScale + delta);
  },

  scaleModelDown: () => {
    const currentScale = get().modelScale;
    const delta = currentScale <= 1.0 ? 0.1 : (currentScale * 0.1);
    get().setModelScale(currentScale - delta);
  },

  resetModelTransform: () => {
    set({ modelScale: 1.0 });

    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      model.resetTransform();
    }
  },

  // Hit Area 調試動作實作
  toggleHitAreaDebug: () =>
    set((state) => ({
      hitAreaDebug: !state.hitAreaDebug
    })),

  // 模型管理動作實作
  setCurrentModelName: (name) =>
    set({ currentModelName: name }),

  setModelSwitching: (switching) =>
    set({ modelSwitching: switching }),

  getCurrentModelConfig: () => {
    const state = get();
    return state.availableModels.find(m => m.name === state.currentModelName);
  },

  // 動態模型清單管理
  loadAvailableModels: async () => {
    try {
      const remoteModels = await fetchAvailableModels();
      set({ availableModels: remoteModels as unknown as ModelConfig[] });
      // 若當前模型不在清單中，切換至第一個
      const current = get().currentModelName;
      if (!remoteModels.find(m => m.name === current) && remoteModels.length > 0) {
        set({ currentModelName: remoteModels[0].name });
      }
    } catch (e) {
      console.warn('[appStore] loadAvailableModels 失敗，使用內建清單:', e);
    }
  },

  addImportedModel: (model: RemoteModelConfig) => {
    set((state) => {
      const exists = state.availableModels.find(m => m.name === model.name);
      if (exists) {
        return {
          availableModels: state.availableModels.map(m =>
            m.name === model.name ? { ...m, ...model } as unknown as ModelConfig : m
          ),
        };
      }
      return {
        availableModels: [...state.availableModels, model as unknown as ModelConfig],
      };
    });
  },

  removeModel: (name: string) => {
    set((state) => ({
      availableModels: state.availableModels.filter(m => m.name !== name),
      currentModelName:
        state.currentModelName === name
          ? (state.availableModels.find(m => m.name !== name)?.name ?? '')
          : state.currentModelName,
    }));
  },

  setJpafState: (state) => set({ jpafState: state }),

  clearChatHistory: () => set({
    chatHistory: [{
      id: "system-init",
      role: 'system',
      content: '系統：AI 對話模組準備就緒。請確保 Python 後端已經啟動。(uvicorn main:app)'
    }],
  }),

  // 聊天與情緒控制動作實作
  appendChatMessage: (message) => {
    const id = Date.now().toString() + Math.random().toString(36).substring(2, 9);
    set((state) => ({
      chatHistory: [...state.chatHistory, { ...message, id }]
    }));
    return id;
  },

  updateChatMessage: (id, content) => {
    set((state) => ({
      chatHistory: state.chatHistory.map((msg) =>
        msg.id === id ? { ...msg, content } : msg
      )
    }));
  },

  setAiTyping: (isTyping) => set({ isAiTyping: isTyping }),

  setCompressing: (isCompressing) => set({ isCompressing }),

  setAiBehavior: (headIntensity, blushLevel, eyeLOpen, eyeROpen, durationSec = 5.0, mouthForm = 0.0, browLY = 0.0, browRY = 0.0, browLAngle = 0.0, browRAngle = 0.0, browLForm = 0.0, browRForm = 0.0, eyeSync = true, eyeLSmile = 0.0, eyeRSmile = 0.0, browLX = 0.0, browRX = 0.0) => {
    set({ aiBehavior: { headIntensity, blushLevel, eyeLOpen, eyeROpen } });

    // 同步到 Live2D 模型
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      const bridgeModel = model as unknown as AiBehaviorBridgeModel;

      if (typeof bridgeModel.setAiBehavior === 'function') {
        bridgeModel.setAiBehavior(headIntensity, blushLevel, eyeLOpen, eyeROpen, durationSec, mouthForm, browLY, browRY, browLAngle, browRAngle, browLForm, browRForm, eyeSync, eyeLSmile, eyeRSmile, browLX, browRX);
      } else {
        // Fallback to older method if available
        if (typeof bridgeModel.setAiHappiness === 'function') {
          bridgeModel.setAiHappiness(headIntensity, durationSec);
        }
      }
    }
  },

  setBlinkControl: (action, durationSec = 0, intervalMin, intervalMax) => {
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (!model) return;

    const bridgeModel = model as unknown as AiBehaviorBridgeModel;

    switch (action) {
      case 'force_blink':
        if (typeof bridgeModel.forceBlink === 'function') {
          bridgeModel.forceBlink(durationSec);
        }
        break;
      case 'pause':
        if (typeof bridgeModel.pauseAutoBlink === 'function') {
          bridgeModel.pauseAutoBlink(durationSec);
        }
        break;
      case 'resume':
        if (typeof bridgeModel.resumeAutoBlink === 'function') {
          bridgeModel.resumeAutoBlink();
        }
        break;
      case 'set_interval':
        if (typeof bridgeModel.setBlinkInterval === 'function' && intervalMin !== undefined && intervalMax !== undefined) {
          bridgeModel.setBlinkInterval(intervalMin, intervalMax);
        }
        break;
    }
  },

  setExpressionPlan: (plan) => {
    set({ expressionPlan: plan, expressionEvents: plan.microEvents ?? [] });

    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (!model) return;

    const bridgeModel = model as unknown as AiBehaviorBridgeModel;
    bridgeModel.applyBasePose?.(plan.basePose);
    for (const event of plan.microEvents ?? []) {
      bridgeModel.enqueueMicroEvent?.(event);
    }
  },

  enqueueExpressionEvents: (events) =>
    set((state) => ({ expressionEvents: [...state.expressionEvents, ...events] })),

  clearExpressionEvents: () => set({ expressionEvents: [] }),
}));
