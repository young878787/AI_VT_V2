"""
REST API：Live2D 模型管理（列出、上傳、刪除）。
"""
import os
import re
import shutil
from typing import List

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from core.config import RESOURCES_DIR
from infrastructure.model_registry import (
    BUILTIN_MODELS,
    load_model_registry,
    save_model_registry,
    find_model3_json,
)

router = APIRouter()


@router.get("/api/models")
async def list_models():
    """回傳所有可用模型（內建 + 匯入）。"""
    registry = load_model_registry()
    # 合併，用 name 去重（匯入覆蓋同名內建）
    merged: dict[str, dict] = {m["name"]: m for m in BUILTIN_MODELS}
    for m in registry:
        merged[m["name"]] = m
    return {"models": list(merged.values())}


@router.post("/api/models/upload")
async def upload_model(files: List[UploadFile] = File(...)):
    """
    接收 Live2D 模型的所有檔案（含子目錄結構），
    儲存到 public/Resources/<ModelName>/，
    並將模型加入 model_registry.json。
    前端以 webkitRelativePath 作為 filename 傳送。
    """
    if not files:
        raise HTTPException(status_code=400, detail="未收到任何檔案")

    first_path = files[0].filename or ""
    parts = first_path.replace("\\", "/").split("/")
    model_dir_name = parts[0] if parts else ""

    if not model_dir_name:
        raise HTTPException(status_code=400, detail="無法判斷模型目錄名稱")

    if not re.match(r"^[A-Za-z0-9_\-]+$", model_dir_name):
        raise HTTPException(
            status_code=400, detail=f"目錄名稱含非法字元: {model_dir_name}"
        )

    target_dir = os.path.join(RESOURCES_DIR, model_dir_name)
    os.makedirs(target_dir, exist_ok=True)

    saved_files: list[str] = []
    for upload in files:
        rel = (upload.filename or "").replace("\\", "/")
        rel_sub = "/".join(rel.split("/")[1:]) if "/" in rel else rel
        if not rel_sub:
            continue
        dest_parts = rel_sub.split("/")
        dest = os.path.join(target_dir, *dest_parts)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        content = await upload.read()
        with open(dest, "wb") as f:
            f.write(content)
        saved_files.append(rel_sub)

    model_json_rel_dir, model_json_name = find_model3_json(target_dir)
    if not model_json_name:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail="找不到 .model3.json 檔案，請確認匯入的是完整的 Live2D 模型資料夾",
        )

    registry = load_model_registry()
    new_entry = {
        "name": model_dir_name,
        "directory": model_json_rel_dir,
        "fileName": model_json_name,
        "displayName": model_dir_name,
        "description": "匯入的模型",
        "imported": True,
    }
    registry = [m for m in registry if m["name"] != model_dir_name]
    registry.append(new_entry)
    save_model_registry(registry)

    print(f"[Model Upload] 匯入模型 '{model_dir_name}'，共 {len(saved_files)} 個檔案")
    return JSONResponse(
        {
            "success": True,
            "model": new_entry,
            "filesCount": len(saved_files),
        }
    )


@router.delete("/api/models/{model_name}")
async def delete_model(model_name: str):
    """刪除匯入的模型（不可刪除內建模型）。"""
    if not re.match(r"^[A-Za-z0-9_\-]+$", model_name):
        raise HTTPException(status_code=400, detail="非法模型名稱")

    builtin_names = {m["name"] for m in BUILTIN_MODELS}
    if model_name in builtin_names:
        raise HTTPException(status_code=403, detail="不可刪除內建模型")

    registry = load_model_registry()
    new_registry = [m for m in registry if m["name"] != model_name]
    if len(new_registry) == len(registry):
        raise HTTPException(status_code=404, detail="模型不存在於 registry")
    save_model_registry(new_registry)

    target_dir = os.path.join(RESOURCES_DIR, model_name)
    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir)
        print(f"[Model Delete] 已刪除模型目錄: {target_dir}")

    return {"success": True, "deleted": model_name}
