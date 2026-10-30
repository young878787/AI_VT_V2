import { AvailableModels } from '../live2d/LAppDefine';
import { LAppLive2DManager } from '../live2d/LAppLive2DManager';
import { useAppStore } from '../store/appStore';
import { actionScheduler } from './actionScheduler';

/** 切換舞台資源並重綁常駐基準；短表演不在切換後重播。 */
export async function switchModel(directory: string): Promise<void> {
  const state = useAppStore.getState();
  const config = AvailableModels.find(model => model.directory === directory);
  if (!config || state.modelLoading || !state.modelLoaded || directory === state.currentModelDirectory) return;

  const manager = LAppLive2DManager.getInstance();
  useAppStore.setState({ modelLoading: true, modelLoaded: false, modelError: null });
  actionScheduler.cancel();

  try {
    const model = await manager.loadModel(directory);
    const current = useAppStore.getState();
    model.setModelScale(current.modelScale);
    model.setEyeTrackingEnabled(current.eyeTrackingEnabled);
    model.setAutoEffectsEnabled(current.autoPlayEnabled);
    model.setSpeaking(current.isSpeaking);

    actionScheduler.cancel();
    useAppStore.setState({ currentModelDirectory: config.directory, modelLoading: false, modelLoaded: true });
    if (current.expressionPlan?.idlePlan) model.applyIdlePlan(current.expressionPlan.idlePlan);
    if (current.ambientPlan) {
      const currentTargets = { ...current.ambientPlan };
      delete currentTargets.hop;
      model.applyAmbientPlan(currentTargets);
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : '未知錯誤';
    const previous = manager.getActiveModel();
    actionScheduler.cancel();
    useAppStore.setState({
      modelLoading: false,
      modelLoaded: previous !== null,
      modelError: `${detail}${previous ? `。仍使用 ${state.currentModelDirectory}，請重新選擇模型重試。` : ''}`,
    });
  }
}
