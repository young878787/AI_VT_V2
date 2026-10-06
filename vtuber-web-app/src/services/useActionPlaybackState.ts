import { useEffect, useState } from 'react';
import { actionScheduler } from './actionScheduler';

/** 進度來自播放所有者；訂閱處理切換，定時取樣補上經過時間。 */
export function useActionPlaybackState() {
  const [state, setState] = useState(() => actionScheduler.getPlaybackState());
  useEffect(() => {
    const unsubscribe = actionScheduler.subscribePlaybackState(setState);
    const timer = window.setInterval(() => setState(actionScheduler.getPlaybackState()), 100);
    return () => {
      unsubscribe();
      window.clearInterval(timer);
    };
  }, []);
  return state;
}
