import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { DisplayPage } from './pages/DisplayPage.tsx'

// 路徑路由：/display → 純 OBS 顯示頁；其他 → 完整控制頁
const isDisplayRoute = window.location.pathname === '/display'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {isDisplayRoute ? <DisplayPage /> : <App />}
  </StrictMode>,
)
