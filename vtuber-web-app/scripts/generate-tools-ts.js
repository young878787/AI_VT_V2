/**
 * Build script：將 backend/domain/tools/Hiyori.json 轉換為前端 TypeScript 常量。
 * 用法：node scripts/generate-tools-ts.js
 * 輸出：src/generated/tools.ts
 *
 * 此檔案為單一角色 schema 的橋樑；每次修改 JSON 後重新執行即可同步前端。
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const SCHEMA_PATH = path.resolve(__dirname, '../../backend/domain/tools/Hiyori.json');
const OUT_DIR = path.resolve(__dirname, '../src/generated');
const OUT_FILE = path.join(OUT_DIR, 'tools.ts');

function main() {
  const raw = fs.readFileSync(SCHEMA_PATH, 'utf-8');
  const schema = JSON.parse(raw);

  const live2dUi = schema.ui_config.live2d;
  const openaiLive2d = schema.openai_tools.live2d;
  const tool = openaiLive2d[0];
  const toolName = tool.function.name;

  // 找出 blink_control 工具（如果存在）
  const blinkTool = openaiLive2d.find((t) => t.function.name === 'blink_control');
  const hasBlinkControl = !!blinkTool;

  // --- 產生 ParamDef interface ---
  const paramDefInterface = `export interface ParamDef {
  key: string;
  backendKey: string;
  label: string;
  min: number;
  max: number;
  step: number;
  emoji: string;
  color: string;
  default: number;
}`;

  // --- 產生 PARAM_DEFS ---
  const paramEntries = live2dUi.params.map((p) => {
    return `  {
    key: '${p.frontend_key}',
    backendKey: '${p.backend_key}',
    label: '${p.label}',
    min: ${p.min},
    max: ${p.max},
    step: ${p.step},
    emoji: '${p.emoji}',
    color: '${p.color}',
    default: ${p.default},
  }`;
  }).join(',\n');

  const paramDefsDecl = `export const PARAM_DEFS: ParamDef[] = [\n${paramEntries}\n];`;

  // --- 產生 MISSING_PARAMS ---
  const missingEntries = live2dUi.missing_params.map((p) => {
    return `  {
    backendKey: '${p.backend_key}',
    label: '${p.label}',
    emoji: '${p.emoji}',
    reason: '${p.reason.replace(/'/g, "\\'")}',
  }`;
  }).join(',\n');

  const missingDecl = `export const MISSING_PARAMS: {
  backendKey: string;
  label: string;
  emoji: string;
  reason: string;
}[] = [\n${missingEntries}\n];`;

  // --- 產生 DEFAULT_PARAMS ---
  const defaultEntries = live2dUi.params
    .map((p) => `  ${p.frontend_key}: ${p.default}`)
    .join(',\n');

  const defaultParamsDecl = `export const DEFAULT_PARAMS: Record<string, number> = {\n${defaultEntries}\n};`;

  // --- 產生 eyeSync default ---
  const eyeSyncDecl = `export const EYE_SYNC_DEFAULT = ${live2dUi.ui_extras.eye_sync_default};`;

  // --- 產生 coverage info ---
  const connected = live2dUi.params.length + 1; // +1 for eyeSync
  const total = connected + live2dUi.missing_params.length;
  const coverageDecl = `export const COVERAGE_INFO = { connected: ${connected}, total: ${total} };`;

  // --- 產生工具名稱 ---
  const toolNameDecl = `export const LIVE2D_TOOL_NAME = '${toolName}';`;

  // --- 產生 blink_control 工具資訊 ---
  const blinkToolDecl = hasBlinkControl
    ? `export const BLINK_CONTROL_TOOL = {
  name: '${blinkTool.function.name}',
  description: '${blinkTool.function.description.replace(/'/g, "\\'")}',
  actions: ${JSON.stringify(blinkTool.function.parameters.properties.action.enum)},
} as const;`
    : `export const BLINK_CONTROL_TOOL = null;`;

  // --- 組裝完整檔案 ---
  const output = `// ============================================
// 自動生成檔案 — 請勿手動修改
// 來源：backend/domain/tools/Hiyori.json
// 命令：node scripts/generate-tools-ts.js
// ============================================

${paramDefInterface}

${paramDefsDecl}

${missingDecl}

${defaultParamsDecl}

${eyeSyncDecl}

${coverageDecl}

${toolNameDecl}

${blinkToolDecl}
`;

  if (!fs.existsSync(OUT_DIR)) {
    fs.mkdirSync(OUT_DIR, { recursive: true });
  }
  fs.writeFileSync(OUT_FILE, output, 'utf-8');

  console.log(`✅ Generated ${path.relative(process.cwd(), OUT_FILE)}`);
  console.log(`   Params: ${live2dUi.params.length}, Missing: ${live2dUi.missing_params.length}`);
}

main();
