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


class LLMContextOverflow(LLMError):
    """大模型返回 400 且提示上下文/输入超长（maximum context length / prompt is too long 等）。

    属于「结构性超长」而非瞬时限流/过载，不能用简单的退避重试消化——必须由调用方做
    「丢弃 KB 依据 / 截断送审正文 / 强制分片摘要」等降级处理后再试，否则整批静默失败。
    继承自 LLMError，故既有的 ``except llm_client.LLMError`` 仍能兜底捕获（退化为告警跳过），
    而需要主动降级的调用方可优先 ``except llm_client.LLMContextOverflow`` 做精准处理。
    """


# 全局并发闸：限制同时打到 LLM 提供方的在途请求数，避免「批次并发」放大触发限流(429/503)雪崩。
# 与审核引擎的批次并发解耦——批次可多路在飞计算 prompt，但落库到提供方的请求受此闸约束。
#
# 热更新（修复：旧实现首次调用即缓存 cap，运行时改 llm_max_concurrent 不生效需重启）：
# 记录构造时的 cap，每次取用时对比当前配置；变化即重建 semaphore。重建只影响「之后」
# 获取闸门的请求，已在闸内的在途请求自然排空，不会中断。收紧并发在少量在途请求跑完后即达成。
_LLM_SEM: asyncio.Semaphore | None = None
_LLM_SEM_CAP: int = 0


def _llm_semaphore() -> asyncio.Semaphore:
    global _LLM_SEM, _LLM_SEM_CAP
    cap = max(1, int(config.get("llm_max_concurrent", 3)))
    if _LLM_SEM is None or cap != _LLM_SEM_CAP:
        _LLM_SEM = asyncio.Semaphore(cap)
        _LLM_SEM_CAP = cap
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


# 上下文超长关键词：命中即判定为「输入超模型上下文窗口」（而非参数/鉴权错误）。
# 覆盖 OpenAI/vLLM/SiliconFlow/千问 等常见英文与中文报错文案，做大小写无关子串匹配。
_CONTEXT_OVERFLOW_KEYS = (
    "maximum context length",
    "context length",
    "prompt is too long",
    "prompt exceeds",
    "tokens exceed",
    "exceeds the maximum",
    "context window",
    "too many tokens",
    "input token",
    "sequence length",
    "token limit",
    # OpenAI 官方错误码与 vLLM 常见文案（务必覆盖，否则真实超长会被漏判为普通 400）
    "context_length_exceeded",
    "longer than the maximum",
    "maximum model length",
    # 中文关键词需足够具体：裸「超出」会误判「参数超出范围」等非超长 400，故限定为长度/上下文相关表述
    "超过最大",
    "上下文长度",
    "长度超出",
    "超出上下文",
    "超长",
)


def _is_context_overflow(body: str) -> bool:
    """从错误响应体识别「上下文/输入超长」语义（400 类）。"""
    b = (body or "").lower()
    return any(k in b for k in _CONTEXT_OVERFLOW_KEYS)


# CJK 字符区间（基本汉字 / 扩展A / 兼容汉字 / CJK 标点+日文假名 / 韩文音节 / 扩展B+）。
# 用于 estimate_tokens 的快速 CJK 计数。
_CJK_RE = re.compile(
    "[　-ヿ㐀-䶿一-鿿豈-﫿가-힯"
    "\U00020000-\U0002ffff]"
)


def estimate_tokens(text: str) -> int:
    """近似 token 计数（不依赖 tiktoken，避免引入重依赖）。

    中文/日文/韩文等 CJK 统一表意文字按 ~1.6 token 计，其余字符（英文/数字/标点/空白）
    按 ~0.3 token 计。对以中文招标/投标文件为主的场景，用于「发前 token 预算」判断足够准确，
    且偏差方向偏保守（多估），不会漏判超长。复杂 CJK 扩展区字符也按 CJK 计，避免低估。
    """
    if not text:
        return 0
    # 用正则一次性统计 CJK 字符数（C 层实现，远快于逐字符 Python 循环——
    # 高并发下逐字符遍历 6 万字 × 多文件会明显占用事件循环 CPU）。
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk * 1.6 + other * 0.3) + 1


def _base_url() -> str:
    """归一化 base_url，兼容三种用户填法：

    - ``https://host/v1/chat/completions``  （从文档里直接粘了完整地址）
    - ``https://host/v1``                    （只填到 /v1）
    - ``https://host``                       （只填 host）

    统一收敛为 ``https://host/v1``（不含末尾 ``/chat/completions``），
    再由 :func:`_endpoint` 补上 ``/chat/completions``，避免拼成
    ``/v1/chat/completions/v1/chat/completions`` 这类导致 404 的畸形地址。
    """
    base = (config.get("llm_base_url", "") or "").rstrip("/")
    # 用户可能直接粘了完整端点，先剥掉末尾的 /chat/completions
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
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
            # 但若属「上下文超长」语义，抛出可降级的 LLMContextOverflow，交由引擎做多级降级重试
            if _status == 400 and _is_context_overflow(body):
                raise LLMContextOverflow(f"大模型返回 400 上下文超长: {body}")
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
                if resp.status_code == 400 and _is_context_overflow(body):
                    raise LLMContextOverflow(f"大模型返回 400 上下文超长: {body[:400]}")
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
# 尾随逗号：, 后仅跟空白与闭合括号（JSON 不允许，但小参数模型高频产出）
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_trailing_commas(s: str) -> str:
    """移除对象/数组里的尾随逗号（"a":1,} → "a":1}）。反复替换以处理嵌套连续场景。"""
    prev = None
    while prev != s:
        prev = s
        s = _TRAILING_COMMA_RE.sub(r"\1", s)
    return s


def _complete_truncated(s: str) -> str | None:
    """对被截断的 JSON 做最小闭合修复：按栈补齐未闭合的字符串与括号。

    小参数模型在 max_tokens 截断时常输出「半截 JSON」（未闭合的字符串/数组/对象），
    直接解析必失败、整批结论作废。此处在扫描到文本结束仍有未闭合结构时，按栈顺序
    补上 "、}、]，尽量抢救出已生成的完整前缀（尾部残缺的最后一项由后续 json.loads
    容错或调用方按 rule_id 缺失补答处理）。返回补齐后的字符串；无可修复结构返回 None。
    """
    start = min(
        [p for p in (s.find("{"), s.find("[")) if p >= 0],
        default=-1,
    )
    if start < 0:
        return None
    stack: list[str] = []
    in_str = False
    esc = False
    # 记录最后一个「结构上安全」的截断点（不在字符串中、且刚闭合完一项）用于兜底
    for ch in s[start:]:
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
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
    repaired = s[start:]
    if in_str:
        repaired += '"'
    # 去掉可能悬空的尾随逗号后再补闭合括号
    repaired = _strip_trailing_commas(repaired.rstrip().rstrip(","))
    while stack:
        repaired += stack.pop()
    return repaired


def parse_json(text: str) -> Any:
    """从模型输出中稳健提取 JSON：优先代码块，其次首个平衡括号片段。

    容错分级（由严到宽，well-formed JSON 走第一档零开销）：
    1) 直接 json.loads；
    2) 括号平衡扫描，容忍前后多余说明文字；
    3) 去尾随逗号后重试（小参数模型高频缺陷）；
    4) 截断补齐：对未闭合的字符串/括号做最小闭合，抢救半截 JSON。
    """
    if not text:
        raise LLMError("模型输出为空")
    candidates: list[str] = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())

    def _try(s: str) -> tuple[bool, Any]:
        try:
            return True, json.loads(s)
        except json.JSONDecodeError:
            return False, None

    for cand in candidates:
        ok, val = _try(cand)
        if ok:
            return val
        # 3) 去尾随逗号后整体重试
        ok, val = _try(_strip_trailing_commas(cand))
        if ok:
            return val
        # 2) 括号平衡扫描，容忍前后多余说明文字
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
                        frag = cand[start : i + 1]
                        ok, val = _try(frag)
                        if ok:
                            return val
                        ok, val = _try(_strip_trailing_commas(frag))
                        if ok:
                            return val
                        break

    # 4) 截断补齐：对最宽候选（原始整段）做最小闭合修复后最后一搏
    for cand in candidates:
        repaired = _complete_truncated(cand)
        if repaired:
            ok, val = _try(repaired)
            if ok:
                logger.warning("模型输出疑似被截断，已按最小闭合修复后解析成功")
                return val

    # 5) detail 超长截断修复：findings 数组中 detail 字段疑似被截断（句子中间断开）时，
    #    强制截短 detail 至最后一个完整句子（句号/分号前），补全 JSON 闭合后重试。
    #    适用场景：模型在 detail 写超长导致 max_tokens 截断，前4步补齐失败时的最后抢救。
    for cand in candidates:
        # 寻找 "detail": "..." 疑似被截断的位置（引号未闭合或句子不完整）
        detail_pattern = r'"detail"\s*:\s*"([^"]{100,}?)(?=$|[^"\\]$)'
        match = re.search(detail_pattern, cand, re.DOTALL)
        if match:
            detail_content = match.group(1)
            # 查找最后一个完整句子标记（句号、分号、问号）
            last_sentence = max(
                detail_content.rfind('。'),
                detail_content.rfind('；'),
                detail_content.rfind('？'),
                detail_content.rfind('！'),
            )
            if last_sentence > 50:  # 至少保留50字的有效 detail
                truncated_detail = detail_content[:last_sentence + 1]
                # 重建 JSON：替换被截断的 detail，补全后续字段与闭合括号
                fixed = cand[:match.start(1)] + truncated_detail + '",'
                # 补全必要字段与闭合（简化版：假设 detail 后还缺 title/status/evidence 等）
                fixed += '"title":"(推理过程超长已截短)","status":"unknown","evidence":"","location":""}'
                # 尝试补全为 findings 数组与外层对象
                if '"findings"' in cand and fixed.count('{') > fixed.count('}'):
                    fixed += ']}'
                ok, val = _try(fixed)
                if ok:
                    logger.warning("检测到 detail 字段超长截断，已强制截短至最后完整句并补全 JSON")
                    return val

    raise LLMError(f"无法解析模型输出为 JSON: {text[:200]}")


async def test_connection() -> dict[str, Any]:
    """以一次最小对话请求探测连通性（兼容所有 OpenAI 协议实现，含讯飞星火）。

    早期实现打 ``/v1/models`` 列表端点，但讯飞星火等部分提供方不实现
    ``/models``，鉴权通过后会返回 404。改为直接发 ``/v1/chat/completions``，
    能同时验证地址路径、鉴权与模型是否存在。
    """
    model = (config.get("llm_model") or "").strip()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0.0,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            resp = await client.post(_endpoint(), json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"连接失败：{exc}"}
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        if resp.status_code in (401, 403):
            return {
                "ok": False,
                "message": f"鉴权失败(HTTP {resp.status_code})，请检查 API Key/APIPassword 是否正确：{body}",
            }
        if resp.status_code == 404:
            return {
                "ok": False,
                "message": "地址路径错误(HTTP 404)：base_url 应以 /v1 结尾，或直接填完整的 .../v1/chat/completions",
            }
        if resp.status_code == 400:
            return {
                "ok": False,
                "message": f"请求被拒(HTTP 400)，多为模型名称不存在或参数错误：{body}",
            }
        return {"ok": False, "message": f"HTTP {resp.status_code}: {body}"}
    # 解析成功响应，取一点内容回显
    try:
        data = resp.json()
        content = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    except Exception:
        return {"ok": True, "message": "连接正常（返回非标准 JSON，但 HTTP 200）"}
    detail: dict[str, Any] = {"model": model}
    if content:
        detail["sample"] = content[:60]
    return {"ok": True, "message": "连接正常", "detail": detail}
