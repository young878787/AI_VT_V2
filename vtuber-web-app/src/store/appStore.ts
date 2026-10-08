/**
 * 應用程式狀態管理 - 使用 Zustand
 */
import { create } from 'zustand';
import { FixedModel } from '../live2d/LAppDefine';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import type { BlinkAction, ExpressionEyeMotionPlan, ExpressionIdlePlan, ExpressionMicroEvent, ExpressionMotionPlan, ExpressionPlanPayload } from '../types/expressionPlan';
import type { EmotionSource, EmotionState } from '../types/emotionState';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  status?: 'interrupted';
}

export interface ChatPerformance {
  firstTokenLatencyMs: number | null;
  tokensPerSecond: number | null;
  outputTokens: number | null;
}

interface AiBehaviorBridgeModel {
  setAiBehavior?: (headIntensity: number, blushLevel: number, eyeLOpen: number, eyeROpen: number, durationSec?: number, mouthForm?: number, browLY?: number, browRY?: number, browLAngle?: number, browRAngle?: number, browLForm?: number, browRForm?: number, eyeSync?: boolean, eyeLSmile?: number, eyeRSmile?: number, browLX?: number, browRX?: number, bodyAngleX?: number, bodyAngleY?: number, bodyAngleZ?: number, breathLevel?: number, physicsImpulse?: number, eyeBallX?: number, eyeBallY?: number) => void;
  setAiHappiness?: (headIntensity: number, durationSec?: number) => void;
  forceBlink?: (durationSec?: number) => void;
  pauseAutoBlink?: (durationSec?: number) => void;
  resumeAutoBlink?: () => void;
  setBlinkInterval?: (intervalMin: number, intervalMax: number) => void;
  applyBasePose?: (basePose: ExpressionPlanPayload['basePose']) => void;
  applyMotionPlan?: (motionPlan?: ExpressionMotionPlan) => void;
  applyEyeMotionPlan?: (eyeMotionPlan?: ExpressionEyeMotionPlan) => void;
  applyIdlePlan?: (idlePlan: ExpressionIdlePlan) => void;
  enqueueMicroEvent?: (event: ExpressionMicroEvent) => void;
  enqueueSequence?: (sequence: ExpressionMicroEvent[]) => void;
}

interface AppState {
  // 麥克風狀態
  microphoneEnabled: boolean;
  microphonePermission: 'granted' | 'denied' | 'prompt';

  // 模型載入狀態
  modelLoading: boolean;
  modelLoaded: boolean;
  modelError: string | null;

  // 固定角色名稱，供聊天與表情除錯識別模型。
  readonly currentModelName: string;

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
  isSpeaking: boolean;
  isCompressing: boolean;
  chatPerformance: ChatPerformance;
  aiBehavior: {
    headIntensity: number;
    blushLevel: number;
    eyeLOpen: number;
    eyeROpen: number;
  };
  expressionPlan: ExpressionPlanPayload | null;
  lastExpressionDebug: ExpressionPlanPayload['debug'] | null;
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
  updateChatMessageStatus: (id: string, status: ChatMessage['status']) => void;
  setAiTyping: (isTyping: boolean) => void;
  setSpeaking: (isSpeaking: boolean) => void;
  setCompressing: (isCompressing: boolean) => void;
  setChatPerformance: (metrics: Partial<ChatPerformance>) => void;
  clearChatPerformance: () => void;
  setAiBehavior: (headIntensity: number, blushLevel: number, eyeLOpen: number, eyeROpen: number, durationSec?: number, mouthForm?: number, browLY?: number, browRY?: number, browLAngle?: number, browRAngle?: number, browLForm?: number, browRForm?: number, eyeSync?: boolean, eyeLSmile?: number, eyeRSmile?: number, browLX?: number, browRX?: number, bodyAngleX?: number, bodyAngleY?: number, bodyAngleZ?: number, breathLevel?: number, physicsImpulse?: number, eyeBallX?: number, eyeBallY?: number) => void;
  setBlinkControl: (action: BlinkAction, durationSec?: number, intervalMin?: number, intervalMax?: number) => void;
  setExpressionPlan: (plan: ExpressionPlanPayload) => void;
  clearExpressionPlan: () => void;
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

  // JEV 情緒狀態
  emotionState: EmotionState | null;
  emotionSource: EmotionSource | null;
  setEmotionState: (state: EmotionState, source: EmotionSource) => void;
  clearChatHistory: () => void;
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
  modelDragEnabled: true,
  modelScale: 1.0,

  // Hit Area 調試初始狀態
  hitAreaDebug: false,

  // 日式動漫 AI 初始狀態
  chatHistory: [
    {
      id: "system-init",
      role: 'system',
      content: '露西亞在這裡。今天想聊些什麼？'
    }
  ],
  isAiTyping: false,
  isSpeaking: false,
  isCompressing: false,
  chatPerformance: {
    firstTokenLatencyMs: null,
    tokensPerSecond: null,
    outputTokens: null,
  },
  aiBehavior: {
    headIntensity: 0,
    blushLevel: 0,
    eyeLOpen: 1,
    eyeROpen: 1
  },
  expressionPlan: null,
  lastExpressionDebug: null,
  expressionEvents: [],

  // JEV 情緒初始狀態
  emotionState: null,
  emotionSource: null,

  currentModelName: FixedModel.name,

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
    })),

  setModelScale: (scale: number) => {
    if (!Number.isFinite(scale)) return;
    // 半身構圖的相對倍率，避免誤操作讓角色消失。
    const clampedScale = Math.max(0.75, Math.min(1.25, scale));
    set({ modelScale: clampedScale });

    // 同步到模型
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      model.setModelScale(clampedScale);
    }
  },

  scaleModelUp: () => get().setModelScale(get().modelScale + 0.05),

  scaleModelDown: () => get().setModelScale(get().modelScale - 0.05),

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

  setEmotionState: (state, source) => set({ emotionState: state, emotionSource: source }),

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

  updateChatMessageStatus: (id, status) => {
    set((state) => ({
      chatHistory: state.chatHistory.map((msg) =>
        msg.id === id ? { ...msg, status } : msg
      )
    }));
  },

  setAiTyping: (isTyping) => set({ isAiTyping: isTyping }),

  setSpeaking: (isSpeaking) => set({ isSpeaking }),

  setCompressing: (isCompressing) => set({ isCompressing }),

  setChatPerformance: (metrics) => set((state) => ({
    chatPerformance: { ...state.chatPerformance, ...metrics },
  })),

  clearChatPerformance: () => set({
    chatPerformance: {
      firstTokenLatencyMs: null,
      tokensPerSecond: null,
      outputTokens: null,
    },
  }),

  setAiBehavior: (headIntensity, blushLevel, eyeLOpen, eyeROpen, durationSec = 5.0, mouthForm = 0.0, browLY = 0.0, browRY = 0.0, browLAngle = 0.0, browRAngle = 0.0, browLForm = 0.0, browRForm = 0.0, eyeSync = true, eyeLSmile = 0.0, eyeRSmile = 0.0, browLX = 0.0, browRX = 0.0, bodyAngleX = 0.0, bodyAngleY = 0.0, bodyAngleZ = 0.0, breathLevel = 0.35, physicsImpulse = 0.0, eyeBallX = 0.0, eyeBallY = 0.0) => {
    set({ aiBehavior: { headIntensity, blushLevel, eyeLOpen, eyeROpen } });

    // 同步到 Live2D 模型
    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (model) {
      const bridgeModel = model as unknown as AiBehaviorBridgeModel;

      if (typeof bridgeModel.setAiBehavior === 'function') {
        bridgeModel.setAiBehavior(headIntensity, blushLevel, eyeLOpen, eyeROpen, durationSec, mouthForm, browLY, browRY, browLAngle, browRAngle, browLForm, browRForm, eyeSync, eyeLSmile, eyeRSmile, browLX, browRX, bodyAngleX, bodyAngleY, bodyAngleZ, breathLevel, physicsImpulse, eyeBallX, eyeBallY);
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
    set(state => ({ expressionPlan: plan, expressionEvents: plan.microEvents ?? [],
      lastExpressionDebug: plan.debug?.jevDecisionSource ? plan.debug : state.lastExpressionDebug }));

    const manager = LAppLive2DManager.getInstance();
    const model = manager.getActiveModel();
    if (!model) return;

    const bridgeModel = model as unknown as AiBehaviorBridgeModel;
    bridgeModel.applyBasePose?.(plan.basePose);
    bridgeModel.applyMotionPlan?.(plan.motionPlan);
    bridgeModel.applyEyeMotionPlan?.(plan.eyeMotionPlan);
    for (const event of plan.microEvents ?? []) {
      bridgeModel.enqueueMicroEvent?.(event);
    }
    if ((plan.sequence ?? []).length > 0) {
      bridgeModel.enqueueSequence?.(plan.sequence);
    }
    if (plan.idlePlan) {
      bridgeModel.applyIdlePlan?.(plan.idlePlan);
    }
  },

  enqueueExpressionEvents: (events) =>
    set((state) => ({ expressionEvents: [...state.expressionEvents, ...events] })),

  clearExpressionPlan: () => set({ expressionPlan: null, expressionEvents: [] }),

  clearExpressionEvents: () => set({ expressionEvents: [] }),
}));
