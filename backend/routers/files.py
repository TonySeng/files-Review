"""文件上传与管理接口。"""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..routers import deps
from ..services import file_store, text_locator

router = APIRouter(prefix="/api/files", tags=["files"])


class LocateRequest(BaseModel):
    """原文定位请求。file_ids 为空时在全部已上传文件中查找。

    location 为模型标注的位置（如「第3页」「第1章」），用于把检索范围限定到
    对应页/章节内；candidates 为 finding 的渐进上下文（evidence→detail→title），
    用于在多个模糊命中间挑选与原文最契合的实例。两者均可选。
    """

    snippet: str
    file_ids: list[str] | None = None
    context_chars: int = 240
    location: str | None = None
    candidates: list[str] | None = None


@router.post("/upload", summary="上传文件（multipart），返回 file_id 与解析状态")
async def upload(
    files: list[UploadFile] = File(...),
    roles: str = Form(""),
    file_types: str = Form(""),
    caller: dict = Depends(deps.get_caller),
):
    """多文件上传并解析。roles / file_types 为逗号分隔列表，与 files 顺序一致。

    file_types 为用户手动指定的文件类型 id（系统不自动识别内容）；类型限定的
    后缀不匹配时该文件进入 errors，其余文件正常解析。
    """
    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    type_list = [t.strip() for t in file_types.split(",") if t.strip()]
    results, errors = [], []

    async def handle(idx: int, upload_file: UploadFile):
        role = role_list[idx] if idx < len(role_list) else "bid"
        ftype = type_list[idx] if idx < len(type_list) else None
        try:
            content = await upload_file.read()
            record = await file_store.save_and_parse(
                upload_file.filename or "unnamed", content, role, ftype
            )
            results.append(file_store._public(record))
        except file_store.StoreError as exc:
            errors.append({"filename": upload_file.filename, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 单文件异常不影响其余
            errors.append({"filename": upload_file.filename, "message": f"处理失败: {exc}"})

    # 解析含 OCR，可能耗时，并发处理
    await asyncio.gather(*(handle(i, f) for i, f in enumerate(files)))

    if not results and errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"files": results, "errors": errors}


@router.get("", summary="查询当前会话已上传文件列表")
async def list_files(caller: dict = Depends(deps.get_caller)):
    return {"files": file_store.list_files()}


@router.patch("/{file_id}/role", summary="修改文件角色（招标/投标/附件）")
async def update_role(file_id: str, payload: dict, caller: dict = Depends(deps.get_caller)):
    try:
        return file_store.set_role(file_id, payload.get("role", ""))
    except file_store.StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{file_id}/type", summary="修改文件类型（触发重新解析）")
async def update_type(
    file_id: str, payload: dict, caller: dict = Depends(deps.get_caller)
):
    """手动指定文件类型（系统不自动识别）。返回更新后的文件元信息。"""
    try:
        return file_store.set_file_type(file_id, payload.get("file_type") or None)
    except file_store.StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/locate", summary="按内容指纹定位文件在文档中的位置")
async def locate_snippet(
    req: LocateRequest, caller: dict = Depends(deps.get_caller)
):
    """在已上传文档中定位文本片段，返回带上下文的原文位置。"""
    snippet = (req.snippet or "").strip()
    if not snippet:
        raise HTTPException(status_code=400, detail="待定位的片段不能为空")

    if req.file_ids:
        try:
            docs = file_store.get_many(req.file_ids)
        except file_store.StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    else:
        # 未指定文件时在全部已上传文件中查找
        docs = [
            d for d in (file_store.get(f["file_id"]) for f in file_store.list_files())
            if d is not None
        ]

    matches = text_locator.locate_in_docs(
        docs,
        snippet,
        context_chars=max(0, min(req.context_chars, 1000)),
        location=req.location,
        candidates=req.candidates,
    )
    return {"snippet": snippet, "matches": matches}


@router.get("/{file_id}/text", summary="获取文件提取后的纯文本内容")
async def get_text(file_id: str, limit: int = 20000, caller: dict = Depends(deps.get_caller)):
    record = file_store.get(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="文件不存在")
    text = record.get("text") or ""
    return {
        "file_id": file_id,
        "filename": record["filename"],
        "char_count": len(text),
        "truncated": len(text) > limit,
        "text": text[:limit],
    }


@router.delete("/{file_id}", summary="删除已上传文件")
async def delete_file(file_id: str, caller: dict = Depends(deps.get_caller)):
    if not file_store.delete(file_id):
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"deleted": True}


@router.get("/{file_id}", summary="下载文件原始内容（支持中文文件名）")
async def download_file(file_id: str, caller: dict = Depends(deps.get_caller)):
    """下载原始上传文件（字节）。供历史任务详情页「下载」使用。

    文件字节持久化在数据卷 uploads 子目录，容器重启不会丢失；若记录存在但
    磁盘字节已缺失（极端情况），返回 404 提示不可用而非抛出。
    """
    record = file_store.get(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="文件不存在")
    path = record.get("path")
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="文件内容已不可用，可能已被清理")
    filename = record.get("filename") or "download"
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # Starlette 的 FileResponse 会在 filename 含非 ASCII 时自动以 RFC 5987
    # （filename*=UTF-8''...）编码，中文文件名下载不乱码。
    return FileResponse(path, media_type=media_type, filename=filename)
