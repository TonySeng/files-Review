"""千问 80B 客户端（vLLM / OpenAI 兼容协议）。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, AsyncIterator

import httpx

from .. import config
from . import call_audit

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


# 全局并发闸：限制同时打到 LLM 提供方的在途请求数，避免「批次并发」放大触发限流(429/503)雪崩。
# 与审核引擎的批次并发解耦——批次可多路在飞计算 prompt，但落库到提供方的请求受此闸约束。
_LLM_SEM: asyncio.Semaphore | None = None


def _llm_semaphore() -> asyncio.Semaphore:
    global _LLM_SEM
    if _LLM_SEM is None:
        cap = int(config.get("llm_max_concurrent", 3))
        _LLM_SEM = asyncio.Semaphore(max(1, cap))
    return _LLM_SEM


def _is_transient_status(code: int) -> bool:
    """429 限流 / 503 过载(尤其 System is too busy) / 其余 5xx 视为瞬时故障，可退避重试；
    其余 4xx（鉴权/参数/模型不存在等）不可重试，直接抛出。"""
    return code == 429 or code == 503 or (500 <= code < 600)


def _is_transient_llm_error(exc: Exception) -> bool:
    """判断 LLMError 是否由瞬时限流/过载/网络抖动引起（用以决定是否触发双倍无工具重试）。"""
    s = str(exc)
    keys = ("429", "503", "System is too busy", "ReadTimeout", "ConnectError",
            "ConnectTimeout", "timed out", "RemoteProtocolError", "too many request")
    return any(k.lower() in s.lower() for k in keys)


def _base_url() -> str:
    """归一化 base_url：硅基流动(SiliconFlow)等地址自带 /v1，千问/DeepSeek 不带。

    统一补齐且仅补一次，避免拼成 /v1/v1/chat/completions 导致 404。
    """
    base = (config.get("llm_base_url", "") or "").rstrip("/")
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def _endpoint() -> str:
    return _base_url() + "/chat/completions"


def _headers() -> dict[str, str]:
    key = config.get("llm_api_key") or "EMPTY"
    return {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}


async def _stream_attempt(
    payload: dict[str, Any], timeout: httpx.Timeout, deadline: float
) -> tuple[int, str, str]:
    """执行一次流式尝试，返回 (status, body_text, content)。

    - status >= 400 时 content 为空串，由上层走与非流式一致的状态码处理（瞬时退避/直接抛出）。
    - 读超时（timeout.read）只约束相邻两个 SSE 增量之间的空窗，长解码不再被「总时长」误杀；
      总墙钟超过 deadline 时抛 LLMError，交由上层按硬顶预算收口。
    """
    parts: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        async with client.stream("POST", _endpoint(), json=payload, headers=_headers()) as resp:
            status = resp.status_code
            if status >= 400:
                return status, (await resp.aread()).decode("utf-8", "replace"), ""
            async for line in resp.aiter_lines():
                if time.monotonic() > deadline:
                    raise LLMError("llm stream exceeded budget deadline")
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                for choice in obj.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
    return 200, "", "".join(parts)


async def chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """对话，返回 message 对象（可能含 tool_calls）。

    stream=True 时走 SSE 流式：读超时退化为「增量空窗超时」（默认 120s），
    总时长仅受硬顶预算约束——适配法规挖掘这类长解码调用，
    根治「非流式等完整 JSON 生成 > 读超时」被误判为连接失败的问题。
    """
    payload: dict[str, Any] = {
        "model": config.get("llm_model"),
        "messages": messages,
        "temperature": config.get("llm_temperature") if temperature is None else temperature,
        "max_tokens": config.get("llm_max_tokens") if max_tokens is None else max_tokens,
        "stream": bool(stream),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    _t0 = time.monotonic()
    _model = config.get("llm_model")
    _base = _base_url()
    _prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)

    req_timeout = float(timeout) if timeout is not None else float(config.get("llm_timeout", 600))
    if stream:
        # 流式：读超时 = 相邻 SSE 增量之间的空窗上限（默认 120s）；总时长由 _hard_ceil 管控
        idle = float(config.get("llm_stream_idle_timeout", 120))
        timeout = httpx.Timeout(idle, connect=20.0, write=60.0, pool=60.0)
    else:
        timeout = httpx.Timeout(req_timeout, connect=20.0)
    # 单逻辑调用总墙钟硬上限（Fix B 修订）：
    # 生效值 = min(req_timeout × max_attempts, llm_call_hard_ceil)。
    # 关键修正——上限必须覆盖"完整设计重试预算(req_timeout × 最大重试次数)"，
    # 否则像一致性比对(req_timeout=600)这类长超时调用会在第 1 次慢响应后，
    # 因"已用 + 单次上限 > 旧硬上限(600)"被剥夺重试机会，直接抛
    # "exceeded hard ceiling" 导致整段一致性核查失败。
    # llm_call_hard_ceil 仅作绝对上界，约束"配置本身过大"的病理场景
    # （如 req_timeout×max_attempts 高达数千秒时兜底）。
    # 另：llm_max_retries 5→3，根除早期"5×180≈900s 单调用挂死"；
    # 规则批=3×180=540s、一致性批=3×600=1800s 且后者受引擎层 consistency_timeout×2 收口。
    _call_start = time.monotonic()
    max_attempts = int(config.get("llm_max_retries", 3))
    _hard_ceil = min(req_timeout * max_attempts, float(config.get("llm_call_hard_ceil", 1800)))
    # 推理模型（如 deepseek-v4-flash）在复杂/多轮工具场景下会偶发：
    #   - 网络瞬时错误（连接中断/超时）
    #   - 返回空 choices 或"有 tool_calls 之外的轮次却空 content"
    # 这些并非参数/鉴权错误，重试通常即可恢复。故在调用层做有限次退避重试，
    # 避免单次抖动就让整个审核批次失败（曾表现为误导性的"模型输出为空"）。
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        # 预算耗尽：不再发起新的（可能耗时的）尝试，直接收口为错误，交上层退避/跳过。
        # 非流式用「已用时间 + 单次超时上限」判断，避免最后一个尝试又吃掉一个完整 req_timeout；
        # 流式的单次尝试时长不封顶（只受硬顶约束），故不预留 req_timeout。
        _reserve = 0.0 if stream else req_timeout
        if (time.monotonic() - _call_start) + _reserve > _hard_ceil:
            logger.warning(
                "大模型调用累计 %.0fs + 单次上限 %.0fs 将超硬上限 %.0fs，放弃重试",
                time.monotonic() - _call_start, req_timeout, _hard_ceil,
            )
            last_err = LLMError(f"llm call exceeded hard ceiling {_hard_ceil:.0f}s")
            break
        _status = 0
        _body = ""
        _stream_content = ""
        try:
            # 全局并发闸：限制同时打到提供方的在途请求数，压制宜并发放大触发的限流/过载雪崩
            async with _llm_semaphore():
                if stream:
                    _status, _body, _stream_content = await _stream_attempt(
                        payload, timeout, deadline=_call_start + _hard_ceil
                    )
                else:
                    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                        resp = await client.post(_endpoint(), json=payload, headers=_headers())
                        _status = resp.status_code
                        if _status >= 400:
                            _body = (resp.text or "")[:400]
        except httpx.HTTPError as exc:
            # 连接层异常（ConnectError/RemoteProtocolError/ReadTimeout 等）：多为瞬时
            # 网络抖动或服务端在长连接下偶发断连，重试通常可恢复。4xx 鉴权/参数错误
            # 已在下方单独抛出、不重试。
            last_err = LLMError(f"连接大模型失败({type(exc).__name__}): {exc}")
            if attempt + 1 < max_attempts:
                # 指数退避 + 随机抖动，避免重试风暴与对端共振
                backoff = min(2.0 * (2 ** attempt), 30.0) + random.uniform(0, 1.0)
                logger.warning(
                    "大模型连接失败(第%d/%d次, %s)，%.1fs 后重试: %s",
                    attempt + 1, max_attempts, type(exc).__name__, backoff, exc,
                )
                await asyncio.sleep(backoff)
            else:
                logger.warning(
                    "大模型连接失败(第%d/%d次, %s)，已达最大重试次数",
                    attempt + 1, max_attempts, type(exc).__name__,
                )
            continue
        if _status >= 400:
            body = _body
            _resp_headers = {} if stream else resp.headers
            if _is_transient_status(_status):
                # 限流(429)/过载(503)/5xx：瞬时故障，退避后重试；尊重 Retry-After。
                # 注意：旧实现把 >=400 一律直接抛出，导致 503 未退避即逃到上层触发「无工具整批重试」，
                # 在提供方过载时反成加倍施压的死亡螺旋。此处改为客户端内自行退避重试。
                last_err = LLMError(f"大模型返回 {_status}: {body}")
                if attempt + 1 < max_attempts:
                    retry_after = _resp_headers.get("Retry-After")
                    try:
                        ra = float(retry_after) if retry_after and str(retry_after).isdigit() else 0.0
                    except (TypeError, ValueError):
                        ra = 0.0
                    # 503 比普通 5xx 更可能持续过载，底座更大；上限 60s 防止无限等待
                    base = 5.0 if _status == 503 else 3.0
                    backoff = min(base * (2 ** attempt), 60.0)
                    if ra > backoff:
                        backoff = min(ra, 60.0)
                    backoff += random.uniform(0, 1.5)
                    logger.warning(
                        "大模型瞬时故障(第%d/%d次, HTTP %d)，%.1fs 后重试: %s",
                        attempt + 1, max_attempts, _status, backoff, body,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.warning(
                        "大模型瞬时故障(第%d/%d次, HTTP %d)，已达最大重试次数",
                        attempt + 1, max_attempts, _status,
                    )
                continue
            # 其余 4xx（鉴权/参数/模型不存在等）：不可重试，直接抛出
            raise LLMError(f"大模型返回 {_status}: {body}")
        if stream:
            # 流式成功路径：拼好的增量文本直接作为 content 返回（挖掘场景无需 tool_calls）
            if not _stream_content.strip():
                last_err = LLMError("大模型流式返回空内容")
                logger.warning("大模型流式返回空内容(第%d/%d次)，%.1fs 后重试", attempt + 1, max_attempts, 2.0 * attempt)
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(2.0 * attempt)
                continue
            call_audit.log_ai_call(
                kind="llm",
                model=_model,
                base_url=_base,
                prompt_chars=_prompt_chars,
                resp_chars=len(_stream_content),
                duration_ms=(time.monotonic() - _t0) * 1000,
                status="ok",
                retries=attempt,
            )
            return {"content": _stream_content}
        try:
            data = resp.json()
        except Exception:
            last_err = LLMError("大模型返回非 JSON 响应")
            logger.warning("大模型返回非 JSON(第%d/%d次)，%.1fs 后重试", attempt + 1, max_attempts, 2.0 * attempt)
            if attempt + 1 < max_attempts:
                await asyncio.sleep(2.0 * attempt)
            continue
        choices = data.get("choices") or []
        if not choices:
            last_err = LLMError("大模型返回空结果")
            logger.warning("大模型返回空 choices(第%d/%d次)，%.1fs 后重试", attempt + 1, max_attempts, 2.0 * attempt)
            if attempt + 1 < max_attempts:
                await asyncio.sleep(2.0 * attempt)
            continue
        msg = choices[0].get("message") or {}
        # 终轮（无工具调用）却返回空 content：推理模型偶发空响应，重试
        if not (msg.get("content") or "").strip() and not (msg.get("tool_calls") or []):
            last_err = LLMError("大模型返回空内容")
            logger.warning("大模型返回空内容(第%d/%d次)，%.1fs 后重试", attempt + 1, max_attempts, 2.0 * attempt)
            if attempt + 1 < max_attempts:
                await asyncio.sleep(2.0 * attempt)
            continue
        call_audit.log_ai_call(
            kind="llm",
            model=_model,
            base_url=_base,
            prompt_chars=_prompt_chars,
            resp_chars=len(msg.get("content") or ""),
            duration_ms=(time.monotonic() - _t0) * 1000,
            status="ok",
            retries=attempt,
        )
        return msg
    call_audit.log_ai_call(
        kind="llm",
        model=_model,
        base_url=_base,
        prompt_chars=_prompt_chars,
        resp_chars=0,
        duration_ms=(time.monotonic() - _t0) * 1000,
        status="error",
        error=str(last_err)[:300] if last_err else "unknown",
        retries=max_attempts - 1,
    )
    raise last_err or LLMError("大模型未返回有效响应")


async def chat_stream(
    messages: list[dict[str, Any]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """流式对话，逐段产出增量文本。"""
    payload = {
        "model": config.get("llm_model"),
        "messages": messages,
        "temperature": config.get("llm_temperature") if temperature is None else temperature,
        "max_tokens": config.get("llm_max_tokens") if max_tokens is None else max_tokens,
        "stream": True,
    }
    timeout = httpx.Timeout(float(config.get("llm_timeout", 600)), connect=20.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        async with client.stream(
            "POST", _endpoint(), json=payload, headers=_headers()
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise LLMError(f"大模型返回 {resp.status_code}: {body[:400]}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                for choice in obj.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str) -> Any:
    """从模型输出中稳健提取 JSON：优先代码块，其次首个平衡括号片段。"""
    if not text:
        raise LLMError("模型输出为空")
    candidates: list[str] = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())

    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
        # 括号平衡扫描，容忍前后多余说明文字
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cand.find(opener)
            if start < 0:
                continue
            depth, in_str, esc = 0, False, False
            for i in range(start, len(cand)):
                ch = cand[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cand[start : i + 1])
                        except json.JSONDecodeError:
                            break
    raise LLMError(f"无法解析模型输出为 JSON: {text[:200]}")


async def test_connection() -> dict[str, Any]:
    url = _base_url() + "/models"
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code >= 400:
            body = (resp.text or "")[:200]
            msg = f"HTTP {resp.status_code}"
            if body:
                msg += f": {body}"
            return {"ok": False, "message": msg}
        models = [m.get("id") for m in (resp.json().get("data") or [])]
        return {"ok": True, "message": "连接正常", "detail": {"models": models}}
    except httpx.HTTPError as exc:
        return {"ok": False, "message": str(exc)}
