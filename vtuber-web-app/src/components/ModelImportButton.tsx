/**
 * ModelImportButton — 匯入 Live2D 模型（資料夾或多檔案）
 * 支援兩種匯入方式：
 * 1. 📁 匯入資料夾：使用 webkitdirectory 選取整個資料夾
 * 2. 📄 匯入檔案：手動選取模型的所有相關檔案
 */
import { useRef, useState } from 'react';
import { uploadModelFiles } from '../services/modelService';
import { useAppStore } from '@store/appStore';
import './ModelImportButton.css';

export const ModelImportButton = () => {
  const folderInputRef = useRef<HTMLInputElement>(null);
  const fileInputRef   = useRef<HTMLInputElement>(null);
  const addImportedModel = useAppStore(s => s.addImportedModel);

  const [uploading, setUploading] = useState(false);
  const [progress,  setProgress]  = useState(0);
  const [status, setStatus] = useState<{
    msg: string;
    type: 'idle' | 'error' | 'success';
  }>({ msg: '', type: 'idle' });

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;

    setUploading(true);
    setProgress(0);
    setStatus({ msg: `準備上傳 (${files.length} 個檔案)...`, type: 'idle' });

    try {
      const model = await uploadModelFiles(files, (pct) => {
        setProgress(pct);
        setStatus({ msg: `上傳中 ${pct}%…`, type: 'idle' });
      });
      addImportedModel(model);
      setStatus({ msg: `✓ 已匯入「${model.displayName}」`, type: 'success' });
    } catch (e: any) {
      setStatus({ msg: e.message ?? '上傳失敗', type: 'error' });
    } finally {
      setUploading(false);
      setProgress(0);
      // 重置 input 以便同一資料夾可以重複上傳
      if (folderInputRef.current) folderInputRef.current.value = '';
      if (fileInputRef.current)   fileInputRef.current.value   = '';
    }
  };

  return (
    <div className="model-import-zone">
      <div className="model-import-btn-row">
        {/* 資料夾匯入按鈕 */}
        <button
          className="model-import-btn"
          disabled={uploading}
          title="選取 Live2D 模型資料夾（自動遞迴包含所有子目錄）"
          onClick={() => folderInputRef.current?.click()}
          id="model-import-folder-btn"
        >
          📁 匯入資料夾
        </button>

        {/* 多檔案匯入按鈕 */}
        <button
          className="model-import-btn"
          disabled={uploading}
          title="手動選取模型的所有檔案（.model3.json, .moc3 等）"
          onClick={() => fileInputRef.current?.click()}
          id="model-import-files-btn"
        >
          📄 匯入檔案
        </button>
      </div>

      {/* 隱藏的資料夾 input（webkitdirectory 模式） */}
      <input
        ref={folderInputRef}
        type="file"
        style={{ display: 'none' }}
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        {...({ webkitdirectory: '', directory: '' } as any)}
        multiple
        onChange={e => handleFiles(e.target.files)}
        aria-hidden="true"
      />

      {/* 隱藏的多檔案 input */}
      <input
        ref={fileInputRef}
        type="file"
        style={{ display: 'none' }}
        multiple
        accept=".model3.json,.moc3,.physics3.json,.motion3.json,.exp3.json,.png,.jpg,.jpeg,.bmp,.cdi3.json,.userdata3.json,.pose3.json"
        onChange={e => handleFiles(e.target.files)}
        aria-hidden="true"
      />

      {/* 進度條（上傳期間顯示） */}
      {uploading && (
        <div className="model-import-progress" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}>
          <div
            className="model-import-progress__fill"
            style={{ width: `${progress}%` }}
          />
        </div>
      )}

      {/* 狀態文字 */}
      {status.msg && (
        <div className={`model-import-status model-import-status--${status.type}`}>
          {status.msg}
        </div>
      )}
    </div>
  );
};
