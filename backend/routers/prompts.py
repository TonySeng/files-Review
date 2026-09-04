# -*- coding: utf-8 -*-
"""Prompt 统一管理接口（管理员）。

系统中所有 LLM 提示词模板的集中管理入口：列表 / 详情 / 编辑（生成新版本）/
版本历史 / 回滚 / 版本对比 / 重置为内置默认 / 测试验证（变量渲染 + 可选真实
调用大模型）。模板运行时由 ``services.prompt_store`` 渲染，编辑保存后下一次
审核 / 一致性核查 / 法规挖掘即使用新版本，无需重启。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..models.schemas import PromptRollbackRequest, PromptTestRequest, PromptUpdateRequest
from ..routers import deps
from ..services import llm_client, prompt_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.get("", summary="查询全部 Prompt 模板元数据（管理员）")
async def list_prompts(_: dict = Depends(deps.require_admin)):
    """列出全部提示词模板（元数据 + 当前版本号 + 占位符列表，不含正文）。"""
    items = prompt_store.list_prompts()
    for it in items:
        it["customized"] = prompt_store.is_customized(it["key"])
    return {"prompts": items}


@router.get("/{key}", summary="查询 Prompt 模板详情（含全部历史版本）")
async def get_prompt(key: str, _: dict = Depends(deps.require_admin)):
    """获取模板详情：元数据 + 当前内容 + 全部版本历史（含内容与变更备注）。"""
    rec = prompt_store.get_prompt(key)
    if not rec:
        raise HTTPException(status_code=404, detail="提示词模板不存在")
    rec["customized"] = prompt_store.is_customized(key)
    rec["placeholders"] = prompt_store.detect_placeholders(
        rec["versions"][-1]["content"]
    )
    return rec


@router.put("/{key}", summary="更新 Prompt 模板内容（自动生成新版本）")
async def update_prompt(
    key: str, req: PromptUpdateRequest, caller: dict = Depends(deps.require_admin)
):
    """编辑模板内容：保存为新版本并立即生效（历史版本保留可回滚）。"""
    try:
        rec = prompt_store.update_prompt(
            key, req.content, req.comment, updated_by=caller.get("username") or "admin"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return rec


@router.post("/{key}/rollback", summary="回滚 Prompt 到指定历史版本")
async def rollback_prompt(
    key: str, req: PromptRollbackRequest, caller: dict = Depends(deps.require_admin)
):
    """回滚到指定历史版本：复制该版本内容为新版本（历史保持线性可追溯）。"""
    try:
        rec = prompt_store.rollback_prompt(
            key, req.version, updated_by=caller.get("username") or "admin"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return rec


@router.post("/{key}/reset", summary="重置 Prompt 为内置默认版本")
async def reset_prompt(key: str, caller: dict = Depends(deps.require_admin)):
    """重置为代码内置默认模板（追加新版本，不丢失编辑历史）。"""
    try:
        rec = prompt_store.reset_prompt(
            key, updated_by=caller.get("username") or "admin"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return rec


@router.get("/{key}/diff", summary="对比 Prompt 两个版本的文本差异")
async def diff_prompt(
    key: str, v1: int, v2: int, _: dict = Depends(deps.require_admin)
):
    """对比两个版本的差异（统一 diff 格式，含增/删行数统计）。"""
    try:
        return prompt_store.diff_versions(key, v1, v2)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{key}/test", summary="测试验证 Prompt：变量渲染 + 可选真实 LLM 调用")
async def test_prompt(key: str, req: PromptTestRequest, _: dict = Depends(deps.require_admin)):
    """测试验证：按变量渲染模板返回最终提示词；``send_to_llm=true`` 时把渲染
    结果作为 user 消息真实调用一次大模型，返回模型回复供发布前验证效果。
    """
    rec = prompt_store.get_prompt(key)
    if not rec:
        raise HTTPException(status_code=404, detail="提示词模板不存在")
    rendered = prompt_store.render(key, req.variables)
    missing = [p for p in prompt_store.detect_placeholders(rendered)]
    result: dict = {
        "rendered": rendered,
        "unresolved_placeholders": missing,
        "chars": len(rendered),
    }
    if req.send_to_llm:
        try:
            msg = await llm_client.chat(
                [{"role": "user", "content": rendered}],
                temperature=0.0,
                timeout=120,
            )
            result["llm_reply"] = str(msg.get("content") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("提示词测试调用大模型失败: %s", exc)
            result["llm_error"] = str(exc)
    return result
