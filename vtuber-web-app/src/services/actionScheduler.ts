import { useAppStore } from '../store/appStore';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import type { ExpressionPlanPayload } from '../types/expressionPlan';

type Source = 'chat' | 'debug';
type Status = 'started' | 'finished' | 'cancelled';
type Action = {
  id: string;
  turnId?: string;
  plan: ExpressionPlanPayload;
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

  constructor() {
    let modelName = useAppStore.getState().currentModelName;
    useAppStore.subscribe((state) => {
      if (state.currentModelName !== modelName) {
        modelName = state.currentModelName;
        this.cancel();
      }
    });
  }

  setReporter(report: ((status: Status, action: { id: string; turnId?: string }) => void) | null): void {
    this.report = report;
  }

  submit(plan: ExpressionPlanPayload, source: Source, turnId?: string): void {
    const emergency = source === 'chat' && (
      plan.basePose.preset === 'shock_recoil' || plan.debug?.intentEmotion === 'surprised'
    );
    const action: Action = {
      id: typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `action_${Date.now()}`,
      plan, turnId,
      priority: source === 'debug' ? 100 : emergency ? 80 : 50,
      interruptible: !emergency,
      durationMs: Math.max(emergency ? 500 : 200, Math.min(10000, Math.max(
        plan.basePose.durationSec * 1000,
        plan.sequence.reduce((duration, event) => duration + event.durationMs, 0),
        plan.motionPlan?.durationMs ?? 0,
      ))),
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
      this.current = null;
      this.timer = null;
      this.report?.('finished', action);
      const pending = this.pending;
      this.pending = null;
      if (pending) this.start(pending);
    }, action.durationMs);
  }
}

export const actionScheduler = new ActionScheduler();
