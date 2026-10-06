import { useAppStore } from '../store/appStore';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import type { ExpressionPlanPayload } from '../types/expressionPlan';
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
  startedAt?: number;
};

class Live2DAdapter {
  apply(plan: ExpressionPlanPayload): void {
    const store = useAppStore.getState();
    store.setExpressionPlan(plan);
    for (const command of plan.blinkPlan?.commands ?? []) {
      store.setBlinkControl(command.action, command.durationSec ?? 0, command.intervalMin, command.intervalMax);
    }
  }

  cancel(): void {
    LAppLive2DManager.getInstance().getActiveModel()?.cancelExpressionAction();
    useAppStore.getState().setBlinkControl('resume');
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
  private awaitingSpeechEnd = false;
  private native: { model: LAppModel; group?: string; index?: number; expressionId?: string } | null = null;
  private nativeTimer: ReturnType<typeof setTimeout> | null = null;
  private previewReleaseTimer: ReturnType<typeof setTimeout> | null = null;
  private isolatedModel: LAppModel | null = null;
  private previousAutoMotionSuspension = false;
  private previousBlinkState: { paused: boolean; intervalMin: number; intervalMax: number } | null = null;
  private playbackState: ActionPlaybackState = { kind: null, status: 'idle', elapsedMs: 0, loop: false };
  private readonly playbackListeners = new Set<(state: ActionPlaybackState) => void>();
  private previewRevision = 0;

  constructor() {
    useAppStore.subscribe((state, previous) => {
      if (previous.isSpeaking && !state.isSpeaking && this.awaitingSpeechEnd) this.finishCurrent();
    });
  }

  setReporter(report: ((status: Status, action: { id: string; turnId?: string }) => void) | null): void {
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
    if (!action) return { ...this.playbackState };
    const elapsedMs = Math.max(0, performance.now() - (action.startedAt ?? performance.now()));
    const idleAt = action.plan.idlePlan?.enterAfterMs;
    const settling = elapsedMs >= action.durationMs - 300;
    const inIdle = idleAt !== undefined && elapsedMs >= idleAt;
    return { ...this.playbackState, elapsedMs,
      status: action.source === 'debug' && inIdle ? 'idle' : settling ? 'settling' : 'playing',
      loop: action.source === 'debug' && inIdle,
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
    this.cancel();
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
    this.cancel();
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

  stopPreview(): void { this.cancel(); }

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
      const pending = this.pending;
      this.pending = null;
      if (pending) this.start(pending);
    }, 40);
  }

  submit(plan: ExpressionPlanPayload, source: Source, turnId?: string): string {
    if (source === 'debug') {
      this.previewRevision++;
      const model = LAppLive2DManager.getInstance().getActiveModel();
      if (model) this.isolateAutoMotion(model);
    }
    if (source === 'debug' && this.native) this.cancel();
    this.pending = null;
    if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
    this.cooldownTimer = null;
    const emergency = source === 'chat' && (
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
      plan, turnId, source,
      priority: source === 'debug' ? 100 : emergency ? 80 : 50,
      interruptible: !emergency,
      durationMs: Math.max(emergency ? 500 : 200,
        plan.basePose.durationSec * 1000,
        sequenceEnd,
        ...plan.microEvents.map(event => event.durationMs),
        plan.motionPlan?.durationMs ?? 0,
        plan.idlePlan?.enterAfterMs ?? 0,
        source === 'debug' && plan.motionPlan ? plan.motionPlan.durationMs + plan.motionPlan.blendOutMs : 0,
        source === 'debug' && plan.eyeMotionPlan ? plan.eyeMotionPlan.durationMs + plan.eyeMotionPlan.blendOutMs : 0,
      ),
    };
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
      this.publishPlayback({ ...this.playbackState, status: 'cancelled', elapsedMs });
    }
    this.cancelCurrent();
    this.adapter.cancel();
    this.releaseAutoMotion();
  }

  manualControl(durationMs = 3000): void {
    this.previewRevision++;
    if (this.native) this.cancel();
    this.pending = null;
    if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
    this.cooldownTimer = null;
    this.cancelCurrent();
    this.manualUntil = performance.now() + durationMs;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.manualUntil = 0;
      const pending = this.pending;
      this.pending = null;
      if (pending) this.start(pending);
    }, durationMs);
  }

  private cancelCurrent(): void {
    this.awaitingSpeechEnd = false;
    if (this.timer) clearTimeout(this.timer);
    if (this.unlockTimer) clearTimeout(this.unlockTimer);
    this.timer = null;
    this.unlockTimer = null;
    if (this.current) {
      const elapsedMs = this.getPlaybackState().elapsedMs;
      this.adapter.cancel();
      this.report?.('cancelled', this.current);
      this.current = null;
      this.publishPlayback({ ...this.playbackState, status: 'cancelled', elapsedMs });
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
    this.adapter.apply(action.plan);
    this.report?.('started', action);
    this.publishPlayback({ kind: 'procedural', status: 'playing', id: action.id, label: action.plan.basePose.preset,
      startedAtMs: action.startedAt, durationMs: action.durationMs, elapsedMs: 0, loop: false });
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
    this.timer = setTimeout(() => {
      if (this.current?.id !== action.id) return;
      this.timer = null;
      if (action.source === 'debug' && action.plan.idlePlan) {
        this.publishPlayback({ ...this.playbackState, status: 'idle', elapsedMs: action.durationMs, loop: true, phase: 'idle' });
        return;
      }
      if (action.source === 'chat' && useAppStore.getState().isSpeaking) {
        this.awaitingSpeechEnd = true;
        return;
      }
      this.finishCurrent();
    }, action.durationMs);
  }

  private finishCurrent(): void {
    this.awaitingSpeechEnd = false;
    const action = this.current;
    this.current = null;
    if (action) this.report?.('finished', action);
    if (action) this.publishPlayback({ ...this.playbackState, status: 'finished', elapsedMs: action.durationMs,
      phase: action.plan.idlePlan ? 'idle' : undefined });
    this.releaseAutoMotion();
    const pending = this.pending;
    this.pending = null;
    if (pending) this.start(pending);
  }
}

export const actionScheduler = new ActionScheduler();
