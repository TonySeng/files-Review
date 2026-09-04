"""本地知识库客户端（LLM-Docqa，http://localhost:8000/api）。

实测契约：POST /api/qa/ask 返回 SSE 流，事件类型依次为
  sources -> delta(多次) -> stats -> done
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .. import config
from . import call_audit

logger = logging.getLogger(__name__)


class KBError(RuntimeError):
    pass


def _base() -> str:
    return config.get("kb_base_url", "").rstrip("/") + "/api"


async def list_knowledge_bases(api_key: str | None = None) -> list[dict[str, Any]]:
    api_key = api_key or config.get("kb_api_key")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            resp = await client.get(f"{_base()}/knowledge-bases", headers=headers)
        if resp.status_code >= 400:
            raise KBError(f"知识库返回 {resp.status_code}")
        data = resp.json()
    except httpx.HTTPError as exc:
        raise KBError(f"连接知识库失败: {exc}") from exc

    items = data if isinstance(data, list) else data.get("items", [])
    return [
        {
            "id": kb.get("id"),
            "name": kb.get("name"),
            "description": kb.get("description"),
            "document_count": kb.get("document_count", 0),
        }
        for kb in items
        if not kb.get("is_deleted")
    ]


async def ask(question: str, kb_id: str | None = None, api_key: str | None = None) -> dict[str, Any]:
    """向知识库提问，聚合 SSE 流为完整答案 + 引用来源。"""
    kb = kb_id or config.get("kb_id")
    if not kb:
        raise KBError("未配置知识库 ID")
    api_key = api_key or config.get("kb_api_key")
    headers = {"Accept": "text/event-stream", "Authorization": f"Bearer {api_key}"} if api_key else {"Accept": "text/event-stream"}

    _t0 = time.monotonic()
    _q_chars = len(question)
    _kb_base = _base()

    timeout = httpx.Timeout(float(config.get("kb_timeout", 300)), connect=15.0)
    max_attempts = int(config.get("kb_retry", 2))
    last_err: Exception | None = None

    for attempt in range(max_attempts):
        answer_parts: list[str] = []
        sources: list[dict[str, Any]] = []
        stats: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    f"{_kb_base}/qa/ask",
                    json={"kb_id": kb, "question": question},
                    headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise KBError(f"知识库返回 {resp.status_code}: {body[:200]}")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        etype = evt.get("type")
                        if etype == "delta":
                            answer_parts.append(
                                evt.get("content") or evt.get("delta") or evt.get("text") or ""
                            )
                        elif etype == "sources":
                            sources = evt.get("sources") or []
                        elif etype == "stats":
                            stats = {k: v for k, v in evt.items() if k != "type"}
                        elif etype == "error":
                            raise KBError(str(evt.get("message") or evt))
                        elif etype == "done":
                            break
            # 正常读完（含空答案）即返回，不重试；空答案由上层按"无结果"处理
            call_audit.log_ai_call(
                kind="kb",
                model="",
                base_url=_kb_base,
                prompt_chars=_q_chars,
                resp_chars=len("".join(answer_parts).strip()),
                duration_ms=(time.monotonic() - _t0) * 1000,
                status="ok",
                retries=attempt,
            )
            return {
                "question": question,
                "answer": "".join(answer_parts).strip(),
                "sources": [
                    {
                        "source_file": s.get("source_file"),
                        "page": s.get("page"),
                        # 知识库 chunk_size=512（按 token 计），留足空间避免截断片段原文
                        "text": (s.get("text") or "")[:1500],
                        "score": s.get("rerank_score") or s.get("rrf_score"),
                    }
                    for s in sources[:5]
                ],
                "stats": stats,
            }
        except (KBError, httpx.HTTPError) as exc:
            last_err = exc
            if attempt + 1 < max_attempts:
                # KB 服务偶发连接失效（如 ConnectionNotExistException）/ 网络抖动，
                # 重试通常即可恢复（实测重试一次即成功）。
                logger.warning(
                    "知识库检索失败(第%d/%d次, %s)，%.1fs 后重试",
                    attempt + 1, max_attempts, type(exc).__name__, 1.0 + attempt,
                )
                await asyncio.sleep(1.0 + attempt)
                continue
            if isinstance(exc, httpx.HTTPError):
                raise KBError(f"知识库请求失败: {exc}") from exc
            raise
    call_audit.log_ai_call(
        kind="kb",
        model="",
        base_url=_kb_base,
        prompt_chars=_q_chars,
        resp_chars=0,
        duration_ms=(time.monotonic() - _t0) * 1000,
        status="error",
        error=str(last_err)[:300] if last_err else "unknown",
        retries=max_attempts - 1,
    )
    raise last_err or KBError("知识库未返回有效响应")


async def test_connection() -> dict[str, Any]:
    try:
        kbs = await list_knowledge_bases()
        return {
            "ok": True,
            "message": f"连接正常，共 {len(kbs)} 个知识库",
            "detail": {"knowledge_bases": kbs},
        }
    except KBError as exc:
        return {"ok": False, "message": str(exc)}
