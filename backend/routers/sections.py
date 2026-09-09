"""章节库配置接口：以文件类型为维度维护章节（含同义词），并支持按章节预览命中。

所有接口需鉴权：管理员（X-Session-Token）可见/操作全部；普通用户（X-API-Key）
仅可见/操作自己拥有或共享的章节。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..routers import deps
from ..services import file_store
from ..services import sections as sec_store

router = APIRouter(prefix="/api/sections", tags=["sections"])


@router.get("", summary="查询章节库（可按文件类型过滤）")
async def list_sections(
    file_type_id: str | None = Query(None, description="按文件类型过滤；为空返回全部"),
    enabled_only: bool = Query(False, description="仅返回启用中的章节"),
    caller: dict = Depends(deps.get_caller),
):
    return {
        "sections": sec_store.list_sections(
            file_type_id, deps.scope_user_id(caller), enabled_only=enabled_only
        )
    }


@router.post("", summary="新建/更新章节")
async def save_section(payload: dict, caller: dict = Depends(deps.get_caller)):
    """新建或更新章节。

    预置章节（builtin=True）可改名称/同义词/停用，但不可删除。
    保存时做同文件类型内的同名/同义词冲突检测，以 warnings 返回（不阻断）。
    """
    try:
        record, warnings = sec_store.save_section(
            payload, user_id=caller.get("user_id")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"section": record, "warnings": warnings}


@router.delete("/{section_id}", summary="删除自定义章节")
async def delete_section(section_id: str, caller: dict = Depends(deps.get_caller)):
    owner = deps.scope_user_id(caller)
    if not sec_store.delete_section(section_id, owner):
        raise HTTPException(
            status_code=400, detail="章节不存在或为预置章节/非所属，不可删除"
        )
    return {"deleted": True}


@router.get("/parse", summary="解析文档识别出的章节标题")
async def parse_document_headings(
    file_id: str = Query(..., description="文件 id"),
    caller: dict = Depends(deps.get_caller),
):
    """返回该文档正文被识别为章节标题的行，供配置章节时快速录入同义词。"""
    rec = file_store.get(file_id, deps.scope_user_id(caller))
    if not rec:
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")
    headings = sec_store.parse_headings(rec.get("text") or "")
    return {
        "file_id": file_id,
        "filename": rec.get("filename"),
        "file_type": rec.get("file_type"),
        "headings": headings,
    }


@router.post("/preview", summary="预览章节命中情况")
async def preview_sections(payload: dict, caller: dict = Depends(deps.get_caller)):
    """预览一批章节在某文档上的匹配结果，用于验证同义词配置是否合理。

    入参：{"file_id": "...", "section_ids": [...]}（section_ids 为空则返回文档全部标题）
    返回：命中章节（含命中的标题与正文字数）与未命中章节 id。
    """
    file_id = str(payload.get("file_id") or "")
    if not file_id:
        raise HTTPException(status_code=400, detail="file_id 不能为空")
    rec = file_store.get(file_id, deps.scope_user_id(caller))
    if not rec:
        raise HTTPException(status_code=404, detail="文件不存在或已被清理")

    text = rec.get("text") or ""
    ids = [str(x) for x in (payload.get("section_ids") or []) if str(x).strip()]
    defs = sec_store.for_file_type(ids, rec.get("file_type")) if ids else []
    hits = sec_store.match_sections(text, defs)
    hit_ids = {h["section_id"] for h in hits}
    return {
        "file_id": file_id,
        "filename": rec.get("filename"),
        "file_type": rec.get("file_type"),
        "matched": [
            {
                "section_id": h["section_id"],
                "name": h["name"],
                "headings": h["headings"],
                "chars": len(h["body"]),
            }
            for h in hits
        ],
        "missed": [sid for sid in ids if sid not in hit_ids],
        "all_headings": sec_store.parse_headings(text),
    }
