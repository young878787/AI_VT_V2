import { useAppStore } from '../store/appStore';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import type { ExpressionPlanPayload, ExpressionStage } from '../types/expressionPlan';
import type { AudioPlaybackClock } from '../audio/TTSPlayer';
import type { LAppModel } from '../live2d/LAppModel';

export interface ActionPlaybackState {
  kind: 'procedural' | 'native-motion' | 'native-expression' | null;
  status: 'idle' | 'queued' | 'playing' | 'settling' | 'finished' | 'cancelled' | 'failed';
  id?: string;
  label?: string;
  startedAtMs?: number;
  durationMs?: number;
  elapsedMs: number;
  loop: boolean;
  error?: string;
  phase?: 'base' | 'sequence' | 'settling' | 'idle';
  stage?: ExpressionStage;
  responsePhase?: 'reaction' | 'waiting' | 'speaking' | 'settling' | 'idle';
  timingSource?: 'audio' | 'estimated';
  segmentId?: number;
  activeEvents?: string[];
}

type Source = 'chat' | 'debug';
type Status = 'started' | 'finished' | 'cancelled';
type Action = {
  id: string;
  turnId?: string;
  plan: ExpressionPlanPayload;
  source: Source;
  priority: number;
  interruptible: boolean;
  durationMs: number;
  idleAtMs?: number;
  startedAt?: number;
  stage: ExpressionStage;
};

type Response = {
  turnId: string;
  pending: boolean;
  textDone: boolean;
  speechExpected: boolean;
  voiceExpected: boolean;
  voiceEnded: boolean;
  voiceStarted: boolean;
  speechAction: Action | null;
  clock: AudioPlaybackClock | null;
  timingSource?: 'audio' | 'estimated';
};

function fitSpeechDuration(plan: ExpressionPlanPayload, durationMs: number, timingSource: 'audio' | 'estimated'): ExpressionPlanPayload {
  const speech = plan.speech!;
  const scale = durationMs / speech.durationMs;
  let microEvents = plan.microEvents.map(event => {
    const atMs = (event.atMs ?? 0) * scale;
    const duration = Math.max(0, Math.min(event.durationMs, durationMs - atMs));
    return { ...event, atMs, durationMs: duration,
      fadeInMs: event.fadeInMs === undefined ? undefined : Math.min(event.fadeInMs, duration / 2),
      fadeOutMs: event.fadeOutMs === undefined ? undefined : Math.min(event.fadeOutMs, duration / 2) };
  }).filter(event => event.durationMs > 0);
  if (timingSource === 'estimated' && scale < 1) {
    let nextCueAtMs = 0;
    microEvents = microEvents.sort((left, right) => left.atMs - right.atMs).filter(event => {
      if (event.atMs < nextCueAtMs) return false;
      // Drop crowded cues instead of delaying them or stretching a short gesture.
      nextCueAtMs = Math.max(event.atMs + 2500, event.atMs + event.durationMs + 500);
      return true;
    });
  }
  return {
    ...plan,
    speech: { durationMs, timingSource, segments: speech.segments.map(segment => ({
      ...segment, startMs: segment.startMs * scale, endMs: segment.endMs * scale,
    })) },
    microEvents,
    idlePlan: plan.idlePlan ? { ...plan.idlePlan,
      enterAfterMs: durationMs + (plan.idlePlan.source?.postSpeechHoldMs ?? 300),
      ambientEnterAfterMs: plan.idlePlan.ambientEnterAfterMs === undefined ? undefined
        : durationMs + (plan.idlePlan.ambientEnterAfterMs - speech.durationMs),
    } : undefined,
  };
}

class Live2DAdapter {
  setClock(clock: (() => number) | null): void {
    LAppLive2DManager.getInstance().getActiveModel()?.setExpressionClock(clock);
  }

  setResponseActive(active: boolean, releaseDelayMs = 0): void {
    LAppLive2DManager.getInstance().getActiveModel()?.setResponseActive(active, releaseDelayMs);
  }

  apply(plan: ExpressionPlanPayload): void {
    const store = useAppStore.getState();
    store.setExpressionPlan(plan);
    for (const command of plan.blinkPlan?.commands ?? []) {
      store.setBlinkControl(command.action, command.durationSec ?? 0, command.intervalMin, command.intervalMax);
    }
  }

  cancel(): void {
    LAppLive2DManager.getInstance().getActiveModel()?.cancelExpressionAction();
    const store = useAppStore.getState();
    store.clearExpressionPlan();
    store.setBlinkControl('resume');
  }
}

export class ActionScheduler {
  private current: Action | null = null;
  private pending: Action | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private unlockTimer: ReturnType<typeof setTimeout> | null = null;
  private cooldownTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly adapter = new Live2DAdapter();
  private readonly lastStartByKind = new Map<string, number>();
  private report: ((status: Status, action: Action) => void) | null = null;
  private manualUntil = 0;
  private response: Response | null = null;
  private responseTimer: ReturnType<typeof setTimeout> | null = null;
  private native: { model: LAppModel; group?: string; index?: number; expressionId?: string } | null = null;
  private nativeTimer: ReturnType<typeof setTimeout> | null = null;
  private previewReleaseTimer: ReturnType<typeof setTimeout> | null = null;
  private isolatedModel: LAppModel | null = null;
  private previousAutoMotionSuspension = false;
  private previousBlinkState: { paused: boolean; intervalMin: number; intervalMax: number } | null = null;
  private playbackState: ActionPlaybackState = { kind: null, status: 'idle', elapsedMs: 0, loop: false };
  private readonly playbackListeners = new Set<(state: ActionPlaybackState) => void>();
  private previewRevision = 0;

  beginTurn(turnId: string): void {
    this.cancel();
    this.response = { turnId, pending: true, textDone: false, speechExpected: false,
      voiceExpected: false, voiceEnded: false, voiceStarted: false, speechAction: null, clock: null };
  }

  completeText(turnId: string, voiceExpected: boolean, speechExpected = false): void {
    const response = this.response;
    if (response?.turnId !== turnId) return;
    response.textDone = true;
    response.voiceExpected = voiceExpected;
    response.speechExpected ||= speechExpected;
    if (!response.speechExpected) {
      if (!voiceExpected) this.completeResponse();
      return;
    }
    this.tryStartSpeech();
  }

  startVoice(turnId: string, clock: AudioPlaybackClock): void {
    const response = this.response;
    if (response?.turnId !== turnId || !response.pending || response.clock) return;
    response.voiceStarted = true;
    response.clock = clock;
    response.timingSource = 'audio';
    this.tryStartSpeech();
  }

  completeVoice(turnId: string): void {
    const response = this.response;
    if (response?.turnId !== turnId || !response.pending) return;
    response.voiceEnded = true;
    if (!response.speechExpected || response.voiceStarted) this.completeResponse();
    else this.tryStartSpeech();
  }

  skipSpeechPlan(turnId: string): void {
    const response = this.response;
    if (response?.turnId !== turnId) return;
    response.speechExpected = false;
    response.speechAction = null;
    if (response.textDone && (!response.voiceExpected || response.voiceEnded)) this.completeResponse();
  }

  private completeResponse(): void {
    const response = this.response;
    if (!response?.pending) return;
    response.pending = false;
    if (this.responseTimer) clearTimeout(this.responseTimer);
    this.responseTimer = null;
    const action = this.current;
    if (action?.source !== 'chat' || action.turnId !== response.turnId) return;
    const elapsedMs = performance.now() - (action.startedAt ?? performance.now());
    const settleMs = action.plan.idlePlan?.source?.postSpeechHoldMs ?? 300;
    this.adapter.setResponseActive(false, settleMs);
    this.adapter.setClock(null);
    action.durationMs = action.stage === 'speech' ? elapsedMs + settleMs : Math.max(action.durationMs, elapsedMs + settleMs);
    if (action.idleAtMs !== undefined) action.idleAtMs = action.stage === 'speech'
      ? elapsedMs + settleMs : Math.max(action.idleAtMs, elapsedMs + settleMs);
    this.publishPlayback({ ...this.playbackState, durationMs: action.durationMs, responsePhase: 'settling' });
    this.scheduleFinish(action);
  }

  private tryStartSpeech(): boolean {
    const response = this.response;
    if (!response?.pending || !response.speechAction || !response.textDone) return false;
    if (!response.clock) {
      if (response.voiceExpected && !response.voiceEnded) return false;
      const durationMs = Math.min(8000, response.speechAction.plan.speech!.durationMs);
      const startedAt = performance.now();
      response.clock = { durationMs, readElapsedMs: () => Math.min(durationMs, performance.now() - startedAt) };
      response.timingSource = 'estimated';
      this.responseTimer = setTimeout(() => {
        this.responseTimer = null;
        if (this.response === response) this.completeResponse();
      }, durationMs);
    }
    if (this.native || performance.now() < this.manualUntil || this.current?.source === 'debug') return false;
    if (this.current === response.speechAction) return true;
    const action = response.speechAction;
    const elapsedMs = response.clock.readElapsedMs();
    if (elapsedMs >= response.clock.durationMs) return false;
    action.plan = fitSpeechDuration(action.plan, response.clock.durationMs, response.timingSource!);
    action.durationMs = response.clock.durationMs;
    action.idleAtMs = action.plan.idlePlan?.enterAfterMs;
    this.pending = null;
    this.start(action);
    return true;
  }

  private resumeResponse(): boolean {
    const response = this.response;
    if (!response?.speechAction || !response.clock) return false;
    if (response.pending && response.clock.readElapsedMs() < response.clock.durationMs) return this.tryStartSpeech();
    if (response.pending) this.completeResponse();
    const plan = response.speechAction.plan;
    if (plan.idlePlan) {
      this.adapter.setClock(null);
      this.adapter.apply({ ...plan, basePose: { ...plan.idlePlan.settlePose, durationSec: 0 },
        microEvents: [], sequence: [], motionPlan: undefined, eyeMotionPlan: undefined,
        idlePlan: { ...plan.idlePlan, enterAfterMs: 0, ambientEnterAfterMs: 900 } });
      this.publishPlayback({ kind: 'procedural', status: 'idle', id: response.speechAction.id, label: plan.idlePlan.settlePose.preset,
        elapsedMs: 0, startedAtMs: performance.now(), loop: true, phase: 'idle', stage: 'speech', responsePhase: 'idle' });
    }
    this.pending = null;
    return true;
  }

  private isResponseActive(action: Action): boolean {
    const response = this.response;
    return action.source === 'chat' && response !== null && response.turnId === action.turnId && response.pending;
  }

  cancelTurn(turnId: string): void {
    if (this.response?.turnId === turnId) {
      this.response = null;
      if (this.responseTimer) clearTimeout(this.responseTimer);
      this.responseTimer = null;
    }
    if (this.pending?.source === 'chat' && this.pending.turnId === turnId) {
      this.pending = null;
      if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
      this.cooldownTimer = null;
    }
    if (this.current?.source === 'chat' && this.current.turnId === turnId) this.cancelCurrent();
  }

  setReporter(report: ((status: Status, action: { id: string; turnId?: string; stage: ExpressionStage }) => void) | null): void {
    this.report = report;
  }

  subscribePlaybackState(listener: (state: ActionPlaybackState) => void): () => void {
    this.playbackListeners.add(listener);
    return () => this.playbackListeners.delete(listener);
  }

  getPreviewRevision(): number { return this.previewRevision; }

  getPlaybackState(): ActionPlaybackState {
    if (this.native) {
      const native = this.native.model.getNativePlaybackState();
      return { ...this.playbackState,
        elapsedMs: this.native.expressionId
          ? Math.max(0, performance.now() - (this.playbackState.startedAtMs ?? performance.now()))
          : (native?.elapsedSec ?? 0) * 1000 };
    }
    const action = this.current;
    const activeEvents = LAppLive2DManager.getInstance().getActiveModel()?.getExpressionPlaybackState?.().activeEvents;
    if (!action) {
      const state = this.playbackState;
      return { ...state, activeEvents, elapsedMs: state.status === 'idle' && state.loop && state.startedAtMs !== undefined
        ? Math.max(0, performance.now() - state.startedAtMs) : state.elapsedMs };
    }
    const elapsedMs = Math.max(0, performance.now() - (action.startedAt ?? performance.now()));
    const responseActive = this.isResponseActive(action);
    const response = action.source === 'chat' && this.response?.turnId === action.turnId ? this.response : null;
    const speechElapsed = action.stage === 'speech' && responseActive ? response?.clock?.readElapsedMs() : undefined;
    const idleAt = action.idleAtMs;
    const settling = !responseActive && elapsedMs >= action.durationMs - 300;
    const inIdle = !responseActive && idleAt !== undefined && elapsedMs >= idleAt;
    const segmentId = speechElapsed === undefined ? undefined : action.plan.speech?.segments.find(
      segment => speechElapsed >= segment.startMs && speechElapsed < segment.endMs)?.id;
    return { ...this.playbackState, elapsedMs: speechElapsed ?? elapsedMs,
      durationMs: speechElapsed === undefined ? this.playbackState.durationMs : response?.clock?.durationMs,
      stage: action.stage, timingSource: action.stage === 'speech' ? response?.timingSource : undefined,
      segmentId, activeEvents,
      responsePhase: action.source !== 'chat' ? undefined : inIdle ? 'idle' : !responseActive ? 'settling'
        : action.stage === 'speech' ? 'speaking' : elapsedMs >= action.durationMs || response?.textDone ? 'waiting' : 'reaction',
      status: inIdle ? 'idle' : settling ? 'settling' : 'playing',
      loop: inIdle,
      phase: inIdle ? 'idle' : settling ? 'settling'
        : action.plan.sequence.length > 0 ? 'sequence' : 'base' };
  }

  private publishPlayback(state: ActionPlaybackState): void {
    this.playbackState = state;
    for (const listener of this.playbackListeners) listener(this.getPlaybackState());
  }

  private isolateAutoMotion(model: LAppModel): void {
    if (this.previewReleaseTimer) clearTimeout(this.previewReleaseTimer);
    this.previewReleaseTimer = null;
    if (this.isolatedModel === model) return;
    this.restoreAutoMotion();
    this.isolatedModel = model;
    this.previousAutoMotionSuspension = model.isAutoMotionSuspended?.() ?? false;
    this.previousBlinkState = model.getBlinkState?.() ?? null;
    model.setAutoMotionSuspended?.(true);
  }

  private restoreAutoMotion(): void {
    this.isolatedModel?.setAutoMotionSuspended?.(this.previousAutoMotionSuspension);
    if (this.isolatedModel && this.previousBlinkState) {
      this.isolatedModel.setBlinkInterval(this.previousBlinkState.intervalMin, this.previousBlinkState.intervalMax);
      if (this.previousBlinkState.paused) this.isolatedModel.pauseAutoBlink();
      else this.isolatedModel.resumeAutoBlink();
    }
    this.isolatedModel = null;
    this.previousBlinkState = null;
  }

  private releaseAutoMotion(): void {
    if (!this.isolatedModel) return;
    if (this.previewReleaseTimer) return;
    this.previewReleaseTimer = setTimeout(() => {
      this.previewReleaseTimer = null;
      this.restoreAutoMotion();
    }, 300);
  }

  playNativeMotion(group: string, index: number): boolean {
    const initialModel = LAppLive2DManager.getInstance().getActiveModel();
    if (initialModel) this.isolateAutoMotion(initialModel);
    this.cancelPlayback();
    const model = LAppLive2DManager.getInstance().getActiveModel();
    const info = model?.getNativeMotionCatalog().find(item => item.group === group && item.index === index);
    if (!model || !info || info.status !== 'loaded') return this.failNative(`無法載入 ${group}[${index}]`, 'native-motion');
    this.isolateAutoMotion(model);
    model.startMotion(group, index, 3);
    if (model.getNativePlaybackState()?.status !== 'playing') {
      this.releaseAutoMotion();
      return this.failNative(`動作無法開始 ${group}[${index}]`, 'native-motion');
    }
    this.native = { model, group, index };
    this.manualUntil = Number.POSITIVE_INFINITY;
    this.publishPlayback({ kind: 'native-motion', status: 'playing', id: `${group}:${index}`, label: info.name,
      startedAtMs: performance.now(), durationMs: info.durationSec * 1000, elapsedMs: 0, loop: info.loop });
    this.pollNative();
    return true;
  }

  playNativeExpression(id: string): boolean {
    const initialModel = LAppLive2DManager.getInstance().getActiveModel();
    if (initialModel) this.isolateAutoMotion(initialModel);
    this.cancelPlayback();
    const model = LAppLive2DManager.getInstance().getActiveModel();
    const info = model?.getNativeExpressionCatalog().find(item => item.id === id);
    if (!model || !info || info.status !== 'loaded') return this.failNative(`無法載入表情 ${id}`, 'native-expression');
    this.isolateAutoMotion(model);
    if (!model.setExpression(id)) {
      this.releaseAutoMotion();
      return this.failNative(`表情無法開始 ${id}`, 'native-expression');
    }
    this.native = { model, expressionId: id };
    this.manualUntil = Number.POSITIVE_INFINITY;
    this.publishPlayback({ kind: 'native-expression', status: 'playing', id, label: id,
      startedAtMs: performance.now(), elapsedMs: 0, loop: true });
    return true;
  }

  clearNativeExpression(): void { this.stopPreview(); }

  stopPreview(): void {
    this.cancelPlayback();
    this.resumeResponse();
  }

  private failNative(error: string, kind: ActionPlaybackState['kind']): false {
    this.publishPlayback({ kind, status: 'failed', elapsedMs: 0, loop: false, error });
    return false;
  }

  private pollNative(): void {
    this.nativeTimer = setTimeout(() => {
      this.nativeTimer = null;
      if (!this.native || this.native.expressionId) return;
      const state = this.native.model.getNativePlaybackState();
      if (state?.status === 'playing') { this.pollNative(); return; }
      this.native = null;
      this.manualUntil = 0;
      this.publishPlayback({ ...this.playbackState, status: state?.status === 'finished' ? 'finished' : 'cancelled',
        elapsedMs: (state?.elapsedSec ?? 0) * 1000 });
      this.releaseAutoMotion();
      if (this.resumeResponse()) return;
      const pending = this.pending;
      this.pending = null;
      if (pending) this.start(pending);
    }, 40);
  }

  submit(plan: ExpressionPlanPayload, source: Source, turnId?: string): string {
    if (source === 'chat' && this.response && this.response.turnId !== turnId) return '';
    if (source === 'debug') {
      this.previewRevision++;
      const model = LAppLive2DManager.getInstance().getActiveModel();
      if (model) this.isolateAutoMotion(model);
    }
    if (source === 'debug' && this.native) this.cancelPlayback();
    this.pending = null;
    if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
    this.cooldownTimer = null;
    const emergency = source === 'chat' && plan.stage !== 'speech' && (
      plan.basePose.preset === 'shock_recoil' || plan.debug?.intentEmotion === 'surprised'
    );
    let sequenceStart = 0;
    let sequenceEnd = 0;
    plan.sequence.forEach((event, index) => {
      sequenceEnd = Math.max(sequenceEnd, sequenceStart + event.durationMs);
      const overlap = Math.min(event.fadeOutMs ?? 0, plan.sequence[index + 1]?.fadeInMs ?? 0);
      sequenceStart += Math.max(1, event.durationMs - overlap);
    });
    const action: Action = {
      id: typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `action_${Date.now()}`,
      plan, turnId, source, stage: plan.stage ?? 'reaction',
      idleAtMs: plan.idlePlan?.enterAfterMs,
      priority: source === 'debug' ? 100 : emergency ? 80 : 50,
      interruptible: !emergency,
      durationMs: Math.max(emergency ? 500 : 200,
        plan.basePose.durationSec * 1000,
        sequenceEnd,
        ...plan.microEvents.map(event => (event.atMs ?? 0) + event.durationMs),
        plan.motionPlan?.durationMs ?? 0,
        plan.idlePlan?.enterAfterMs ?? 0,
        source === 'debug' && plan.motionPlan ? plan.motionPlan.durationMs + plan.motionPlan.blendOutMs : 0,
        source === 'debug' && plan.eyeMotionPlan ? plan.eyeMotionPlan.durationMs + plan.eyeMotionPlan.blendOutMs : 0,
      ),
    };
    if (source === 'chat' && plan.stage === 'speech') {
      const response = this.response;
      if (!response || response.turnId !== turnId || !response.pending || response.speechAction) return action.id;
      response.speechExpected = true;
      response.speechAction = action;
      this.tryStartSpeech();
      return action.id;
    }
    if (source === 'chat' && this.response && this.response.turnId === turnId && this.response.speechAction) return action.id;
    if (source === 'chat' && performance.now() < this.manualUntil) {
      this.pending = action;
      if (!this.native && !this.current) this.publishPlayback({ kind: 'procedural', status: 'queued', id: action.id,
        label: plan.basePose.preset, durationMs: action.durationMs, elapsedMs: 0, loop: false });
      return action.id;
    }
    const sinceLast = performance.now() - (this.lastStartByKind.get(plan.basePose.preset) ?? -3000);
    if (emergency && sinceLast < 3000) {
      this.pending = action;
      if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
      this.cooldownTimer = setTimeout(() => {
        this.cooldownTimer = null;
        if (!this.current && this.pending) {
          const pending = this.pending;
          this.pending = null;
          this.start(pending);
        }
      }, 3000 - sinceLast);
      return action.id;
    }
    if (this.current && (
      (!this.current.interruptible && performance.now() - (this.current.startedAt ?? 0) < 500)
      || action.priority < this.current.priority
    )) {
      this.pending = action;
      return action.id;
    }
    this.cancelCurrent();
    this.start(action);
    return action.id;
  }

  cancel(): void {
    this.response = null;
    if (this.responseTimer) clearTimeout(this.responseTimer);
    this.responseTimer = null;
    this.cancelPlayback();
  }

  private cancelPlayback(): void {
    this.previewRevision++;
    if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
    this.cooldownTimer = null;
    this.pending = null;
    this.manualUntil = 0;
    if (this.nativeTimer) clearTimeout(this.nativeTimer);
    this.nativeTimer = null;
    if (this.native) {
      const elapsedMs = this.getPlaybackState().elapsedMs;
      this.native.model.cancelExpressionAction();
      this.native = null;
      this.publishPlayback({ ...this.playbackState, status: 'cancelled', elapsedMs, loop: false, phase: undefined });
    }
    this.cancelCurrent();
    this.adapter.cancel();
    this.releaseAutoMotion();
  }

  manualControl(durationMs = 3000): void {
    this.previewRevision++;
    if (this.native) this.cancelPlayback();
    this.pending = null;
    if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
    this.cooldownTimer = null;
    this.cancelCurrent();
    this.manualUntil = performance.now() + durationMs;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.manualUntil = 0;
      if (this.resumeResponse()) return;
      const pending = this.pending;
      this.pending = null;
      if (pending) this.start(pending);
    }, durationMs);
  }

  private cancelCurrent(): void {
    if (this.timer) clearTimeout(this.timer);
    if (this.unlockTimer) clearTimeout(this.unlockTimer);
    this.timer = null;
    this.unlockTimer = null;
    if (this.current || useAppStore.getState().expressionPlan) {
      const elapsedMs = this.getPlaybackState().elapsedMs;
      this.adapter.cancel();
      if (this.current) this.report?.('cancelled', this.current);
      this.current = null;
      this.publishPlayback({ ...this.playbackState, status: 'cancelled', elapsedMs, loop: false, phase: undefined });
      this.releaseAutoMotion();
    }
  }

  private start(action: Action): void {
    const sinceLast = performance.now() - (this.lastStartByKind.get(action.plan.basePose.preset) ?? -3000);
    if (action.priority === 80 && sinceLast < 3000) {
      this.pending = action;
      if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
      this.cooldownTimer = setTimeout(() => {
        this.cooldownTimer = null;
        if (!this.current && this.pending) {
          const pending = this.pending;
          this.pending = null;
          this.start(pending);
        }
      }, 3000 - sinceLast);
      return;
    }
    this.cancelCurrent();
    action.startedAt = performance.now();
    this.current = action;
    if (action.source === 'debug') {
      const model = LAppLive2DManager.getInstance().getActiveModel();
      if (model) {
        this.isolateAutoMotion(model);
        model.cancelExpressionAction();
      }
    }
    this.lastStartByKind.set(action.plan.basePose.preset, performance.now());
    const response = action.stage === 'speech' && this.response?.turnId === action.turnId ? this.response : null;
    const readElapsedMs = response?.clock?.readElapsedMs;
    this.adapter.setClock(readElapsedMs ?? null);
    const elapsedMs = readElapsedMs?.() ?? 0;
    const plan = readElapsedMs && elapsedMs > 0 ? { ...action.plan,
      microEvents: action.plan.microEvents.filter(event => (event.atMs ?? 0) >= elapsedMs),
    } : action.plan;
    this.adapter.apply(plan);
    this.adapter.setResponseActive(this.isResponseActive(action));
    this.report?.('started', action);
    this.publishPlayback({ kind: 'procedural', status: 'playing', id: action.id, label: action.plan.basePose.preset,
      startedAtMs: action.startedAt, durationMs: action.durationMs, elapsedMs: 0, loop: false, stage: action.stage });
    if (!action.interruptible) {
      this.unlockTimer = setTimeout(() => {
        this.unlockTimer = null;
        if (this.current?.id !== action.id || !this.pending || this.pending.priority < action.priority) return;
        const pending = this.pending;
        this.pending = null;
        this.cancelCurrent();
        this.start(pending);
      }, 500);
    }
    this.scheduleFinish(action);
  }

  private scheduleFinish(action: Action): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      if (this.current?.id !== action.id) return;
      this.timer = null;
      if (action.source === 'debug' && action.plan.idlePlan) {
        this.publishPlayback({ ...this.playbackState, status: 'idle', elapsedMs: action.durationMs, loop: true, phase: 'idle' });
        return;
      }
      if (this.isResponseActive(action)) return;
      this.finishCurrent();
    }, Math.max(0, action.durationMs - (performance.now() - (action.startedAt ?? performance.now()))));
  }

  private finishCurrent(): void {
    this.adapter.setResponseActive(false);
    const action = this.current;
    const elapsedMs = this.getPlaybackState().elapsedMs;
    this.current = null;
    if (action) this.report?.('finished', action);
    // WS 結束有限演出；renderer 的 idle 仍持續，由本地播放狀態呈現。
    if (action) this.publishPlayback({ ...this.playbackState, status: action.plan.idlePlan ? 'idle' : 'finished', elapsedMs,
      loop: Boolean(action.plan.idlePlan), phase: action.plan.idlePlan ? 'idle' : undefined,
      responsePhase: action.source === 'chat' ? 'idle' : undefined });
    this.releaseAutoMotion();
    if (action?.source === 'debug' && this.resumeResponse()) return;
    const pending = this.pending;
    this.pending = null;
    if (pending) this.start(pending);
  }
}

export const actionScheduler = new ActionScheduler();
