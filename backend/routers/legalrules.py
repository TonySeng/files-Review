# -*- coding: utf-8 -*-
"""法规文件 → 临时审核规则集。

用户不选用既有规则组/规则集，而是上传法律法规文件，由模型解析全文并自动抽取
一组「临时审核规则」，再拿这组规则去审核目标文档。

接口约定：生成是**异步**的（法规全文动辄数十万字，分块抽取耗时较长），
``POST /generate`` 立即返回规则集记录（status=pending/mining），
前端轮询 ``GET /{id}`` 读取 status / progress / rules。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from .. import config
from ..models.schemas import (
    LegalRuleGenerateRequest,
    LegalRulePromoteRequest,
    LegalRuleUpdateRequest,
)
from ..routers import deps
from ..services import file_store, legal_rules

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/legal-rules", tags=["legal-rules"])


def _assert_owner(rec: dict, caller: dict) -> None:
    """普通用户只能操作自己拥有的记录；管理员可操作全部。"""
    if caller["role"] == "admin":
        return
    if rec.get("owner_id") != caller["user_id"]:
        raise HTTPException(status_code=404, detail="规则集不存在")


@router.post("/preview", summary="法规解析预检：返回切分块数/字符数预估")
async def preview_split(
    req: LegalRuleGenerateRequest, caller: dict = Depends(deps.get_caller)
):
    """预检：不调用模型，只统计切分结果，便于前端提示成本与可行性。"""
    budget = int(config.get("legal_chunk_chars", 12000))
    total_chars = 0
    chunks = 0
    modes: set[str] = set()
    files: list[dict] = []
    source_meta: list[dict] = []
    scope = deps.scope_user_id(caller)
    for fid in req.file_ids:
        rec = file_store.get(fid, scope)
        if not rec:
            raise HTTPException(status_code=404, detail=f"文件不存在或已过期: {fid}")
        text = (rec.get("text") or "").strip()
        if not text:
            raise HTTPException(
                status_code=400, detail=f"文件未提取到可解析文本: {rec.get('filename')}"
            )
        segs, how = legal_rules.split_legal_text(text)
        packed = legal_rules.pack_chunks(segs, budget)
        total_chars += len(text)
        chunks += len(packed)
        modes.add(how)
        meta = legal_rules._extract_doc_meta(text)
        if meta:
            source_meta.append(
                {"file_id": fid, "filename": rec.get("filename") or fid, **meta}
            )
        files.append(
            {
                "file_id": fid,
                "filename": rec.get("filename"),
                "char_count": len(text),
                "segments": len(segs),
                "chunks": len(packed),
                "split_mode": how,
            }
        )
    max_source = int(config.get("legal_max_source_chars", 600000))
    return {
        "files": files,
        "total_chars": total_chars,
        "chunks": chunks,
        "split_mode": "/".join(sorted(modes)),
        "chunk_chars": budget,
        "max_source_chars": max_source,
        "truncated": total_chars > max_source,
        "concurrency": int(config.get("legal_mining_concurrency", 3)),
        "max_rules": int(req.max_rules or config.get("legal_max_rules", 30)),
        "llm_model": config.get("llm_model"),
        "source_meta": source_meta,
    }


@router.post("/generate", summary="异步生成法规临时规则集（LLM 分块抽取）")
async def generate(req: LegalRuleGenerateRequest, caller: dict = Depends(deps.get_caller)):
    """创建并启动一个临时规则集生成任务，立即返回。"""
    is_shared = bool(req.is_shared) and caller["role"] == "admin"
    try:
        rec = await legal_rules.create_ruleset(
            name=req.name,
            file_ids=req.file_ids,
            mode=req.mode,
            max_rules=req.max_rules,
            description=req.description,
            user_id=caller["user_id"],
            is_shared=is_shared,
            reuse=req.reuse,
            scope_user_id=deps.scope_user_id(caller),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建法规规则集失败")
        raise HTTPException(status_code=500, detail=f"创建失败: {exc}") from exc
    return rec


@router.get("", summary="查询法规规则集列表（概要，含 rule_count）")
async def list_rulesets(caller: dict = Depends(deps.get_caller)):
    """列出临时规则集（管理员看全部，普通用户看自己的 + 系统共享）。"""
    return {"rulesets": legal_rules.list_sets(deps.scope_user_id(caller))}


@router.get("/{ruleset_id}", summary="查询法规规则集详情（含规则明细与过程日志）")
async def get_ruleset(ruleset_id: str, caller: dict = Depends(deps.get_caller)):
    """获取规则集详情（含规则、进度、统计、告警）。生成过程中轮询此接口。"""
    rec = legal_rules.get_set(ruleset_id, deps.scope_user_id(caller))
    if not rec:
        raise HTTPException(status_code=404, detail="规则集不存在")
    return rec


@router.patch("/{ruleset_id}", summary="更新法规规则集（名称/规则编辑/启停）")
async def update_ruleset(
    ruleset_id: str, req: LegalRuleUpdateRequest, caller: dict = Depends(deps.get_caller)
):
    """编辑临时规则集（生成完成后可人工修订）。仅属主或管理员可操作。"""
    rec = legal_rules.get_set(ruleset_id, deps.scope_user_id(caller))
    if not rec:
        raise HTTPException(status_code=404, detail="规则集不存在")
    _assert_owner(rec, caller)
    if rec.get("status") == "mining":
        raise HTTPException(status_code=409, detail="规则正在生成中，请等待完成后再编辑")
    # 传 scope_user_id 而非 caller["user_id"]：管理员为 None（无属主限制），
    # 普通用户为自身 id（只能改自己的）；越权校验已由 _assert_owner 完成。
    _uid = deps.scope_user_id(caller)
    updated = legal_rules.update_ruleset(
        ruleset_id,
        user_id=_uid,
        name=req.name,
        description=req.description,
        rules=req.rules,
        is_shared=req.is_shared if caller["role"] == "admin" else None,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="规则集不存在")
    return updated


@router.delete("/{ruleset_id}", summary="删除法规规则集")
async def delete_ruleset(ruleset_id: str, caller: dict = Depends(deps.get_caller)):
    """删除临时规则集。已用于审核任务的规则集删除后不影响历史任务（任务已存快照）。"""
    rec = legal_rules.get_set(ruleset_id, deps.scope_user_id(caller))
    if not rec:
        raise HTTPException(status_code=404, detail="规则集不存在")
    _assert_owner(rec, caller)
    if not legal_rules.delete_set(ruleset_id, deps.scope_user_id(caller)):
        raise HTTPException(status_code=404, detail="规则集不存在")
    return {"deleted": True, "id": ruleset_id}


@router.post("/{ruleset_id}/promote", summary="把临时规则集固化为正式自定义规则集")
async def promote_ruleset(
    ruleset_id: str, req: LegalRulePromoteRequest, caller: dict = Depends(deps.get_caller)
):
    """「转正」：把临时规则集固化为正式自定义规则集，可在规则管理中长期复用。"""
    rec = legal_rules.get_set(ruleset_id, deps.scope_user_id(caller))
    if not rec:
        raise HTTPException(status_code=404, detail="规则集不存在")
    _assert_owner(rec, caller)
    if rec.get("status") != "ready":
        raise HTTPException(status_code=409, detail="规则集尚未生成完成")
    try:
        saved = legal_rules.promote_to_ruleset(
            ruleset_id, user_id=deps.scope_user_id(caller), name=req.name
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("规则集转正失败")
        raise HTTPException(status_code=500, detail=f"转正失败: {exc}") from exc
    if not saved:
        raise HTTPException(status_code=400, detail="规则集为空或不可用")
    return saved
