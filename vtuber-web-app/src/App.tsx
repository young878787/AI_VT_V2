/**
 * 主應用程式元件
 */
import { Live2DCanvas } from '@components/Live2DCanvas';
import { ControlPanel } from '@components/ControlPanel';
import { AIChatPanel } from '@components/AIChatPanel';
import { AnimeDecoration } from '@components/AnimeDecoration';
import { HitAreaOverlay } from '@components/HitAreaOverlay';
import { ModelParamPanel } from '@components/ModelParamPanel';
import './App.css';

function App() {
  return (
    <div className="app">
      <AnimeDecoration />
      <Live2DCanvas />
      <ControlPanel />
      <AIChatPanel />
      <ModelParamPanel />
      <HitAreaOverlay />
    </div>
  );
}

export default App;
