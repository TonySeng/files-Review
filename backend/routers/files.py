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
    owner_id = caller.get("user_id")

    async def handle(idx: int, upload_file: UploadFile):
        role = role_list[idx] if idx < len(role_list) else "bid"
        ftype = type_list[idx] if idx < len(type_list) else None
        try:
            content = await upload_file.read()
            record = await file_store.save_and_parse(
                upload_file.filename or "unnamed", content, role, ftype,
                owner_id=owner_id,
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
    return {"files": file_store.list_files(deps.scope_user_id(caller))}


@router.patch("/{file_id}/role", summary="修改文件角色（招标/投标/附件）")
async def update_role(file_id: str, payload: dict, caller: dict = Depends(deps.get_caller)):
    try:
        return file_store.set_role(file_id, payload.get("role", ""), deps.scope_user_id(caller))
    except file_store.StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{file_id}/type", summary="修改文件类型（触发重新解析）")
async def update_type(
    file_id: str, payload: dict, caller: dict = Depends(deps.get_caller)
):
    """手动指定文件类型（系统不自动识别）。返回更新后的文件元信息。"""
    try:
        return file_store.set_file_type(
            file_id, payload.get("file_type") or None, deps.scope_user_id(caller)
        )
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

    # 过短锚点防护：evidence 只填了「无」这类占位词时，单字/短串会在全文
    # 随机命中无关位置（如正文里的「无」字），直接拒绝检索而不是给错误定位。
    cand_list = [str(c).strip() for c in (req.candidates or []) if str(c).strip()]
    if len(snippet) < 4 and not any(len(c) >= 4 for c in cand_list):
        return {
            "snippet": snippet,
            "matches": [],
            "message": "定位锚点过短（少于 4 字），已拒绝检索以避免错误定位",
        }

    scope = deps.scope_user_id(caller)
    if req.file_ids:
        try:
            docs = file_store.get_many(req.file_ids, scope)
        except file_store.StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    else:
        # 未指定文件时在当前用户可见的已上传文件中查找
        docs = [
            d for d in (file_store.get(f["file_id"], scope) for f in file_store.list_files(scope))
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


@router.get("/{file_id}/preview", summary="原文分页预览：返回指定页文本与高亮区间")
async def preview_page(
    file_id: str,
    page: int | None = None,
    start: int | None = None,
    end: int | None = None,
    snippet: str | None = None,
    caller: dict = Depends(deps.get_caller),
):
    """原文预览 + 页码跳转 + 内容高亮（与原文定位共用同一份提取文本，保证对齐）。

    定位方式（按优先级）：
    - `start`（配合可选 `end`）：原文定位返回的绝对字符下标，自动换算所在页并
      计算页内高亮区间；
    - `page`：直接跳转到指定页（PDF 页码与定位描述中的「第N页」一致）；
    - 两者都提供 `snippet` 时，在该页内二次定位（处理模糊匹配跨页边界的情况）。

    无页标记的文档（docx/txt/xlsx）视为单页「全文」。
    """
    record = file_store.get(file_id, deps.scope_user_id(caller))
    if not record:
        raise HTTPException(status_code=404, detail="文件不存在")
    text = record.get("text") or ""
    if not text.strip():
        raise HTTPException(status_code=404, detail="该文件未提取到文本内容，无法预览")

    pages = text_locator.split_pages(text)

    # —— 选页：start 优先，其次 page，默认第 1 页 ——
    target: dict | None = None
    if start is not None and start >= 0:
        target = text_locator.page_for_offset(pages, start)
    elif page is not None:
        target = next((p for p in pages if p["page"] == page), None)
        if target is None:
            raise HTTPException(
                status_code=400,
                detail=f"页码超出范围：文档共 {pages[-1]['page']} 页",
            )
    target = target or pages[0]

    page_text = text[target["start"] : target["end"]]

    # —— 计算页内高亮区间 ——
    highlights: list[dict[str, int]] = []
    match_meta: dict[str, object] = {}
    if start is not None and target["start"] <= start < target["end"]:
        # 绝对下标 → 页内下标；end 缺省时用 snippet 长度或单字符兜底
        h_start = start - target["start"]
        if end is not None and end > start:
            h_end = min(end, target["end"]) - target["start"]
        elif snippet:
            h_end = min(h_start + len(snippet.strip()), len(page_text))
        else:
            h_end = h_start + 1
        highlights.append({"start": h_start, "end": h_end})
        match_meta = {"match_type": "located", "source": "offset"}
    elif snippet and snippet.strip():
        # 页内二次定位（同一套三级匹配，保证与全文定位结果一致）
        hit = text_locator.locate(page_text, snippet, context_chars=0)
        if hit:
            highlights.append({"start": hit["start"], "end": hit["end"]})
            match_meta = {
                "match_type": hit["match_type"],
                "confidence": hit["confidence"],
                "source": "page_locate",
            }

    return {
        "file_id": file_id,
        "filename": record["filename"],
        "ext": record.get("ext"),
        "page": target["page"],
        "page_label": target["label"],
        "page_count": pages[-1]["page"],
        "is_paged": pages[-1]["page"] > 1 or pages[0]["label"] != "全文",
        "page_text": page_text,
        "highlights": highlights,
        **match_meta,
    }


@router.get("/{file_id}/text", summary="获取文件提取后的纯文本内容")
async def get_text(file_id: str, limit: int = 20000, caller: dict = Depends(deps.get_caller)):
    record = file_store.get(file_id, deps.scope_user_id(caller))
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


@router.get("/{file_id}/sections", summary="获取解析阶段产出的章节结构")
async def get_sections(file_id: str, caller: dict = Depends(deps.get_caller)):
    """返回解析阶段按 document-split 方式拆分的章节结构（不返回章节全文）。

    每个章节含：title（可读章节名，""=标题前的前置内容）、raw（原始标题行）、
    level（标题层级 1-3）、page（所在页码）、char_start/char_end（原文偏移，
    可直接用于原文定位/预览高亮）、char_len（章节体长度）、preview（前 120 字预览）、
    source（style=Word 样式标题 / worksheet=Excel 工作表 / regex=正则识别 /
    preamble=前置内容）。审核引擎按规则关联章节直接复用该结构做精确送审。
    """
    record = file_store.get(file_id, deps.scope_user_id(caller))
    if not record:
        raise HTTPException(status_code=404, detail="文件不存在")
    sections = record.get("sections") or []
    out = []
    for s in sections:
        body = str(s.get("body") or "")
        out.append(
            {
                "title": s.get("title") or "",
                "raw": s.get("raw") or "",
                "level": s.get("level", 0),
                "page": s.get("page", 1),
                "char_start": s.get("char_start", 0),
                "char_end": s.get("char_end", 0),
                "char_len": len(body),
                "preview": body[:120],
                "source": s.get("source") or "",
            }
        )
    return {
        "file_id": file_id,
        "filename": record.get("filename"),
        "ext": record.get("ext"),
        "section_count": record.get("section_count", 0),
        "sections": out,
    }


@router.delete("/{file_id}", summary="删除已上传文件")
async def delete_file(file_id: str, caller: dict = Depends(deps.get_caller)):
    if not file_store.delete(file_id, deps.scope_user_id(caller)):
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"deleted": True}


@router.get("/{file_id}", summary="下载文件原始内容（支持中文文件名）")
async def download_file(file_id: str, caller: dict = Depends(deps.get_caller)):
    """下载原始上传文件（字节）。供历史任务详情页「下载」使用。

    文件字节持久化在数据卷 uploads 子目录，容器重启不会丢失；若记录存在但
    磁盘字节已缺失（极端情况），返回 404 提示不可用而非抛出。
    """
    record = file_store.get(file_id, deps.scope_user_id(caller))
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
