/**
 * Live2D 應用程式常數定義和模型配置
 * 支援多個模型的配置管理
 */

/**
 * 模型配置介面
 */
export interface ModelConfig {
  name: string;           // 模型名稱
  directory: string;      // 模型目錄路徑
  fileName: string;       // .model3.json 文件名
  displayName: string;    // 顯示名稱（中文）
  description?: string;   // 模型描述
  scale?: number;         // 縮放比例
  position?: {            // 初始位置
    x: number;
    y: number;
  };
}

/**
 * Canvas 設定
 */
export const CanvasSettings = {
  // 畫布尺寸（提高解析度）
  Width: 1920,
  Height: 1080,
  
  // 視圖縮放
  ViewScale: 1.0,
  ViewMaxScale: 2.0,
  ViewMinScale: 0.8,
  
  // 視圖邏輯座標
  ViewLogicalLeft: -1.0,
  ViewLogicalRight: 1.0,
  ViewLogicalBottom: -1.0,
  ViewLogicalTop: 1.0,
  
  // 視圖最大範圍（邏輯座標）
  ViewLogicalMaxLeft: -2.0,
  ViewLogicalMaxRight: 2.0,
  ViewLogicalMaxBottom: -2.0,
  ViewLogicalMaxTop: 2.0,
} as const;

/**
 * 資源路徑
 */
export const ResourcePath = {
  // 根路徑
  Root: '/Resources/',
  
  // 背景圖片
  BackImageName: 'back_class_normal.png',
  
  // 齒輪圖示
  GearImageName: 'icon_gear.png',
  
  // 電源圖示
  PowerImageName: 'CloseNormal.png',
  
  // 著色器路徑（SDK 5.3 需要）
  ShaderPath: '/Shaders/WebGL/',
} as const;

/**
 * 可用的模型列表配置
 * 可以輕鬆添加新模型
 */
export const AvailableModels: ModelConfig[] = [
  {
    name: 'Hiyori',
    directory: 'Hiyori',
    fileName: 'Hiyori.model3.json',
    displayName: 'Hiyori（日和）',
    description: '溫柔可愛的少女',
    scale: 1.0,
    position: { x: 0.0, y: 0.0 }
  },
  {
    name: 'Haru',
    directory: 'Haru',
    fileName: 'Haru.model3.json',
    displayName: 'Haru（春）',
    description: '元氣少女，活潑開朗',
    scale: 1.0,
    position: { x: 0.0, y: 0.0 }
  },
  // 未來可以添加更多模型範例：
  // {
  //   name: 'Mao',
  //   directory: 'Mao',
  //   fileName: 'Mao.model3.json',
  //   displayName: 'Mao（貓）',
  //   description: '貓耳少女',
  //   scale: 1.0,
  //   position: { x: 0.0, y: 0.0 }
  // },
];

/**
 * 動作組定義
 */
export const MotionGroup = {
  Idle: 'Idle',           // 待機動作
  TapBody: 'TapBody',     // 點擊身體
  TapHead: 'TapHead',     // 點擊頭部
} as const;

/**
 * 表情定義
 */
export const Expression = {
  Neutral: 'neutral',     // 中性
  Happy: 'happy',         // 開心
  Angry: 'angry',         // 生氣
  Sad: 'sad',             // 難過
  Surprised: 'surprised', // 驚訝
} as const;

/**
 * 動作優先級
 */
export const Priority = {
  None: 0,
  Idle: 1,
  Normal: 2,
  Force: 3,
} as const;

export type PriorityValue = (typeof Priority)[keyof typeof Priority];

/**
 * 除錯設定
 */
export const DebugSettings = {
  // 是否開啟日誌
  LogEnable: true,
  
  // 是否顯示 FPS
  ShowFps: true,
  
  // 是否顯示觸控區域
  ShowTouchArea: false,
} as const;

/**
 * 取得模型完整路徑
 */
export function getModelPath(modelConfig: ModelConfig): string {
  return `${ResourcePath.Root}${modelConfig.directory}/`;
}

/**
 * 取得模型 JSON 完整路徑
 */
export function getModelJsonPath(modelConfig: ModelConfig): string {
  return `${getModelPath(modelConfig)}${modelConfig.fileName}`;
}

/**
 * 根據名稱取得模型配置
 */
export function getModelConfig(name: string): ModelConfig | undefined {
  return AvailableModels.find(model => model.name === name);
}

/**
 * 取得預設模型
 */
export function getDefaultModel(): ModelConfig {
  return AvailableModels[0];
}
