/**
 * modelService.ts — Live2D 模型管理 API
 * 後端 REST 端點的前端呼叫封裝
 */

// 與 wsService.ts 保持一致，使用相同的 BACKEND_PORT env var
const _port = import.meta.env.BACKEND_PORT || '9999';
const BACKEND = `http://localhost:${_port}`;

export interface RemoteModelConfig {
  name: string;
  directory: string;
  fileName: string;
  displayName: string;
  description?: string;
  imported?: boolean;
}

/** 取得後端所有可用模型（內建 + 匯入） */
export async function fetchAvailableModels(): Promise<RemoteModelConfig[]> {
  const res = await fetch(`${BACKEND}/api/models`);
  if (!res.ok) throw new Error(`fetchAvailableModels failed: ${res.status}`);
  const data = await res.json();
  return data.models as RemoteModelConfig[];
}

/**
 * 上傳 Live2D 模型資料夾或多個檔案至後端。
 * files: 由 <input webkitdirectory> 或 <input multiple> 取得的 FileList。
 * onProgress: 0-100 百分比回呼（XHR upload progress）。
 */
export async function uploadModelFiles(
  files: FileList,
  onProgress?: (pct: number) => void,
): Promise<RemoteModelConfig> {
  const form = new FormData();
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    // webkitRelativePath 保留了資料夾結構（如 "Mao/Mao.model3.json"）
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const relativePath = (file as any).webkitRelativePath || file.name;
    form.append('files', file, relativePath);
  }

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${BACKEND}/api/models/upload`);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    };

    xhr.onload = () => {
      if (xhr.status === 200) {
        try {
          const data = JSON.parse(xhr.responseText);
          resolve(data.model as RemoteModelConfig);
        } catch {
          reject(new Error('無法解析伺服器回應'));
        }
      } else {
        let msg = `上傳失敗 (${xhr.status})`;
        try {
          const err = JSON.parse(xhr.responseText);
          msg = err.detail ?? msg;
        } catch (parseError) {
          // ignore error if unparseable
          console.debug('Failed to parse error response', parseError);
        }
        reject(new Error(msg));
      }
    };

    xhr.onerror = () => reject(new Error('網路錯誤，請確認後端已啟動'));
    xhr.send(form);
  });
}

/** 刪除匯入的模型 */
export async function deleteImportedModel(modelName: string): Promise<void> {
  const res = await fetch(`${BACKEND}/api/models/${encodeURIComponent(modelName)}`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `刪除失敗 (${res.status})`);
  }
}
