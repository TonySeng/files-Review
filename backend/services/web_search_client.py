"""联网搜索客户端，支持多个搜索API提供商。"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .. import config

logger = logging.getLogger(__name__)


class WebSearchError(RuntimeError):
    pass


async def search(query: str, max_results: int | None = None) -> dict[str, Any]:
    """执行联网搜索。

    Args:
        query: 搜索查询
        max_results: 最大结果数（None表示使用配置的默认值）

    Returns:
        {
            "query": str,
            "results": [{"title": str, "url": str, "snippet": str}, ...]
        }
    """
    api_type = config.get("web_search_api", "tavily")
    api_key = config.get("web_search_api_key", "")
    if not api_key:
        raise WebSearchError("未配置联网搜索API密钥")

    max_results = max_results or config.get("web_search_max_results", 5)
    timeout = config.get("web_search_timeout", 30)

    try:
        if api_type == "tavily":
            return await _search_tavily(query, api_key, max_results, timeout)
        elif api_type == "bing":
            return await _search_bing(query, api_key, max_results, timeout)
        elif api_type == "serper":
            return await _search_serper(query, api_key, max_results, timeout)
        else:
            raise WebSearchError(f"不支持的搜索API类型: {api_type}")
    except httpx.HTTPError as exc:
        raise WebSearchError(f"搜索请求失败: {exc}") from exc


async def _search_tavily(
    query: str, api_key: str, max_results: int, timeout: int
) -> dict[str, Any]:
    """Tavily Search API: https://docs.tavily.com/docs/tavily-api/rest_api"""
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
        )

    if resp.status_code >= 400:
        error_msg = f"Tavily API 返回 {resp.status_code}"
        try:
            error_data = resp.json()
            error_msg = error_data.get("error", error_msg)
        except Exception:
            pass
        raise WebSearchError(error_msg)

    data = resp.json()
    results = []
    for item in data.get("results", [])[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
        })

    return {"query": query, "results": results}


async def _search_bing(
    query: str, api_key: str, max_results: int, timeout: int
) -> dict[str, Any]:
    """Bing Web Search API: https://learn.microsoft.com/en-us/bing/search-apis/bing-web-search/overview"""
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            headers={"Ocp-Apim-Subscription-Key": api_key},
            params={"q": query, "count": max_results, "mkt": "zh-CN"},
        )

    if resp.status_code >= 400:
        error_msg = f"Bing API 返回 {resp.status_code}"
        try:
            error_data = resp.json()
            error_msg = error_data.get("error", {}).get("message", error_msg)
        except Exception:
            pass
        raise WebSearchError(error_msg)

    data = resp.json()
    results = []
    for item in data.get("webPages", {}).get("value", [])[:max_results]:
        results.append({
            "title": item.get("name", ""),
            "url": item.get("url", ""),
            "snippet": item.get("snippet", ""),
        })

    return {"query": query, "results": results}


async def _search_serper(
    query: str, api_key: str, max_results: int, timeout: int
) -> dict[str, Any]:
    """Serper.dev Google Search API: https://serper.dev/"""
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results, "gl": "cn", "hl": "zh-cn"},
        )

    if resp.status_code >= 400:
        error_msg = f"Serper API 返回 {resp.status_code}"
        try:
            error_data = resp.json()
            error_msg = error_data.get("message", error_msg)
        except Exception:
            pass
        raise WebSearchError(error_msg)

    data = resp.json()
    results = []
    for item in data.get("organic", [])[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })

    return {"query": query, "results": results}
