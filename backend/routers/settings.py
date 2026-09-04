"""服务地址配置与连通性测试接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import config
from ..models.schemas import ConfigUpdate, ConnectionTestRequest
from ..routers import deps
from ..services import kb_client, llm_client, ocr_client, web_search_client

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", summary="查询系统配置（密钥脱敏回显）")
async def get_settings(_: dict = Depends(deps.get_caller)):
    """读取当前配置（密钥脱敏为 "***"）。任意已登录用户可读（用于前端展示功能开关）；
    敏感写入操作限定管理员。"""
    return {"config": config.public_config(), "defaults": config.DEFAULTS}


@router.patch("", summary="更新系统配置（白名单字段，空值不覆盖）")
async def update_settings(
    payload: ConfigUpdate, _: dict = Depends(deps.require_admin)
):
    """修改服务配置（模型 / 知识库 / OCR 等）。仅管理员可写。"""
    patch = payload.model_dump(exclude_none=True)
    # 密钥字段：前端回显脱敏成 "***"，且“已配置则留空保持不变”。
    # 因此 None / 空串 / "***" 一律视为“不修改”，避免清空已存密钥。
    for key in ("llm_api_key", "web_search_api_key", "kb_api_key"):
        if patch.get(key) in (None, "", "***"):
            patch.pop(key, None)
    if patch:
        config.update_config(patch)
    return {"config": config.public_config()}


@router.post("/reset", summary="重置系统配置为默认值")
async def reset_settings(_: dict = Depends(deps.require_admin)):
    """重置为默认配置。仅管理员。"""
    config.reset_config()
    return {"config": config.public_config()}


@router.post("/test", summary="测试外部服务连通性（LLM/OCR/知识库）")
async def test_connection(
    req: ConnectionTestRequest, _: dict = Depends(deps.require_admin)
):
    """测试外部服务连通性。base_url / api_key 非空时临时生效，便于先测后存。

    密钥脱敏：前端回显会把已存密钥显示为 "***"。测试时若直接把 "***"（或空值）
    作为 api_key 下发，后端会把它当成真实密钥覆盖已存配置，导致 DeepSeek 等
    需鉴权服务返回 401。因此这里把 "***" / 空值视为“沿用已存配置”，只有拿到
    真正的非空密钥才临时覆盖。
    """
    # 各 target 对应的地址键与密钥键（web_search 无独立地址，密钥即其配置项）
    url_key = {"llm": "llm_base_url", "ocr": "ocr_base_url", "kb": "kb_base_url"}.get(req.target)
    secret_key = {
        "llm": "llm_api_key",
        "ocr": "ocr_api_key",
        "kb": "kb_api_key",
        "web_search": "web_search_api_key",
    }.get(req.target)

    MASKED = "***"
    overrides: dict[str, Any] = {}
    # 仅当传入“有效”（非空且非脱敏占位符）才临时覆盖，否则沿用已保存配置
    if req.base_url and req.base_url != MASKED and url_key:
        overrides[url_key] = req.base_url
    if req.api_key and req.api_key != MASKED and secret_key:
        overrides[secret_key] = req.api_key
    # 兼容旧前端：web_search 曾把 api key 放在 base_url 字段
    if (
        req.target == "web_search"
        and not req.api_key
        and req.base_url
        and req.base_url != MASKED
    ):
        overrides["web_search_api_key"] = req.base_url

    original = {k: config.get(k) for k in overrides}
    try:
        if overrides:
            config.update_config(overrides, persist=False)
        if req.target == "llm":
            return await llm_client.test_connection()
        if req.target == "ocr":
            return await ocr_client.test_connection()
        if req.target == "web_search":
            return await _test_web_search()
        return await kb_client.test_connection()
    except Exception as exc:  # noqa: BLE001 - 测试接口需返回失败原因而非 500
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if overrides:
            config.update_config(original, persist=False)


async def _test_web_search() -> dict:
    """测试联网搜索API配置。"""
    if not config.get("web_search_api_key"):
        raise HTTPException(status_code=400, detail="未配置API密钥")

    try:
        result = await web_search_client.search("测试查询", max_results=1)
        return {
            "ok": True,
            "message": f"连接成功，使用 {config.get('web_search_api')} API",
            "detail": {"result_count": len(result.get("results", []))},
        }
    except web_search_client.WebSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/knowledge-bases", summary="查询可用知识库列表")
async def list_knowledge_bases(_: dict = Depends(deps.require_admin)):
    """列出本地知识库，供前端下拉选择。

    知识库为可选外部依赖：当其服务不可用（未启动/网络不可达）时，返回空列表并标记
    kb_unavailable，避免前端首页加载时因 502 报错；KB 功能仅在该服务可用时生效。
    """
    try:
        return {"knowledge_bases": await kb_client.list_knowledge_bases(config.get("kb_api_key"))}
    except kb_client.KBError:
        return {"knowledge_bases": [], "kb_unavailable": True}


@router.get("/storage", summary="查看数据存储配置状态")
async def storage_status(_: dict = Depends(deps.require_admin)) -> dict:
    """当前生效的存储配置与后端状态（驱动/集合后端/文件目录/热加载/最近变更）。"""
    from ..storage import snapshot

    return snapshot()


@router.post("/storage/reload", summary="立即重新加载存储配置")
async def storage_reload(_: dict = Depends(deps.require_admin)) -> dict:
    """手动触发存储配置重载（不等热加载轮询）。返回是否有变更及最新状态。"""
    from ..storage import settings as storage_settings, snapshot

    changed = storage_settings.reload_if_changed()
    return {"changed": changed, **snapshot()}
