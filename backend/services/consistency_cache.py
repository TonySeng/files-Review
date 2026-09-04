"""一致性切片摘要缓存（一致性提效 ①）：对相同文档复用其分段摘要结果，跳过重复的 LLM 调用。

缓存键 = 文档文本哈希 + 审核模式 + 重点核查要素 + 温度。任一输入变化即不命中，
避免缓存掩盖材料或要素变更。缓存文件落在挂载卷 DATA_DIR 内（与 rules_store 同策略），
容器重建不丢。读取/写入均容错降级，绝不因缓存异常中断审核。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

from . import rules_store

logger = logging.getLogger(__name__)

_CACHE_FILE = rules_store.DATA_DIR / "consistency_cache.json"
_MAX_ENTRIES = 500

_lock = threading.Lock()
_mem: dict[str, str] = {}


def _load() -> None:
    """惰性加载磁盘缓存到内存（仅一次）。失败降级为空缓存，绝不抛异常。"""
    global _mem
    if _mem:
        return
    try:
        if _CACHE_FILE.exists():
            _mem = json.loads(_CACHE_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 缓存损坏不应中断审核
        logger.warning("一致性摘要缓存读取失败(降级为不命中): %s", exc)
        _mem = {}


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _element_token(e: Any) -> str:
    """把要素条目归一为缓存键片段，兼容字符串与 {name, synonyms, note} 结构。"""
    if isinstance(e, dict):
        name = str(e.get("name") or "").strip()
        syns = sorted({str(s).strip() for s in (e.get("synonyms") or []) if str(s).strip()})
        note = str(e.get("note") or "").strip()
        return f"{name}#{','.join(syns)}#{note}"
    return str(e).strip()


def make_key(
    doc_text: str,
    *,
    mode: str,
    elements: list[Any],
    temperature: float | None,
    kind: str = "summary",
    doc_md5: str | None = None,
    rule_token: str | None = None,
) -> str:
    """生成稳定的文档摘要缓存键（确定性：相同输入 → 相同 key）。

    temperature==0.0 视为确定性（输出可复现），其余按实际温度区分，
    避免确定性/非确定性结论串用。kind 区分不同用途（summary=分段摘要，
    elements=结构化要素提取），避免两类缓存相互串用。
    doc_md5：纳入文件 MD5，避免「文件改了但解析文本哈希未变」时命中旧缓存；
    rule_token：纳入一致性规则内容指纹聚合，使「一致性规则定义已改」即让缓存失效。
    """
    eles = "|".join(sorted(_element_token(e) for e in (elements or [])))
    temp = "det0" if temperature == 0.0 else f"t{temperature}"
    return _sha(
        _sha(doc_text or ""), doc_md5 or "", mode, eles, temp, kind, rule_token or ""
    )


def get(key: str) -> str | None:
    """命中返回缓存的文档摘要文本，未命中返回 None。"""
    if not key:
        return None
    with _lock:
        _load()
        return _mem.get(key)


def put(key: str, summary: str) -> None:
    """写入并落盘；超出容量上限时按插入顺序淘汰最旧。落盘失败仅内存生效。"""
    if not key:
        return
    with _lock:
        _load()
        _mem[key] = summary
        while len(_mem) > _MAX_ENTRIES:
            _mem.pop(next(iter(_mem)))
        try:
            _CACHE_FILE.write_text(json.dumps(_mem, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("一致性摘要缓存落盘失败(仅内存生效): %s", exc)
