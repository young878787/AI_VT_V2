import { useAppStore } from '../store/appStore';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import type { ExpressionPlanPayload } from '../types/expressionPlan';

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

  constructor() {
    useAppStore.subscribe((state, previous) => {
      if (previous.isSpeaking && !state.isSpeaking && this.awaitingSpeechEnd) this.finishCurrent();
    });
  }

  setReporter(report: ((status: Status, action: { id: string; turnId?: string }) => void) | null): void {
    this.report = report;
  }

  submit(plan: ExpressionPlanPayload, source: Source, turnId?: string): void {
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
      ),
    };
    if (source === 'chat' && performance.now() < this.manualUntil) {
      this.pending = action;
      return;
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
      return;
    }
    if (this.current && (
      (!this.current.interruptible && performance.now() - (this.current.startedAt ?? 0) < 500)
      || action.priority < this.current.priority
    )) {
      this.pending = action;
      return;
    }
    this.cancelCurrent();
    this.start(action);
  }

  cancel(): void {
    if (this.cooldownTimer) clearTimeout(this.cooldownTimer);
    this.cooldownTimer = null;
    this.pending = null;
    this.manualUntil = 0;
    this.cancelCurrent();
    this.adapter.cancel();
  }

  manualControl(durationMs = 3000): void {
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
      this.adapter.cancel();
      this.report?.('cancelled', this.current);
      this.current = null;
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
    this.lastStartByKind.set(action.plan.basePose.preset, performance.now());
    this.adapter.apply(action.plan);
    this.report?.('started', action);
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
    const pending = this.pending;
    this.pending = null;
    if (pending) this.start(pending);
  }
}

export const actionScheduler = new ActionScheduler();
