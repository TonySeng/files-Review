"""批次审核结论缓存（方案 C 核心）：对相同输入的批次复用历史 LLM 结论，降低重复/相似任务的增量耗时。

缓存键覆盖所有影响结论的输入（文档指纹、规则集、模式、KB/联网开关、确定性模式、
招标摘要、KB 预检索依据、额外指令）。任一输入变化即不命中，避免缓存掩盖材料或依据变更。

缓存文件落在挂载卷 DATA_DIR 内（与 rules_store 同策略），容器重建不丢。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

from . import rules_store

logger = logging.getLogger(__name__)

_CACHE_FILE = rules_store.DATA_DIR / "findings_cache.json"
_MAX_ENTRIES = 500

_lock = threading.Lock()
_mem: dict[str, list[dict[str, Any]]] = {}


def _load() -> None:
    """惰性加载磁盘缓存到内存（仅一次）。失败降级为空缓存，绝不抛异常。"""
    global _mem
    if _mem:
        return
    try:
        if _CACHE_FILE.exists():
            _mem = json.loads(_CACHE_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 缓存损坏不应中断审核
        logger.warning("审核结论缓存读取失败(降级为不命中): %s", exc)
        _mem = {}


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def make_key(
    doc_hashes: list[str],
    rule_ids: list[str],
    *,
    mode: str,
    kb_enabled: bool,
    web_search_enabled: bool,
    det_mode: bool,
    tender_summary: str,
    global_kb_context: str,
    extra_instruction: str,
    doc_md5s: list[str] | None = None,
    rule_fps: list[str] | None = None,
) -> str:
    """生成稳定的批次缓存键（确定性：相同输入 → 相同 key）。

    文档指纹额外纳入文件 MD5（doc_md5s），避免「文件改了但解析文本哈希未变」（如仅图片/
    排版变更）时仍命中旧结论；规则指纹额外纳入每条规则的内容哈希（rule_fps），避免
    「规则 ID 不变但定义（checkpoints/描述/要素/severity）已改」时返回陈旧结论。
    """
    return _sha(
        _sha(*doc_hashes),
        _sha(*(doc_md5s or [])),
        _sha(*rule_ids),
        _sha(*(rule_fps or [])),
        mode,
        "kb" if kb_enabled else "nokb",
        "ws" if web_search_enabled else "nows",
        "det" if det_mode else "nondet",
        _sha(tender_summary or ""),
        _sha(global_kb_context or ""),
        _sha(extra_instruction or ""),
    )


def get(key: str) -> list[dict[str, Any]] | None:
    """命中返回缓存的批次结论（仅 LLM 产出，不含 structured 锁定结论），未命中返回 None。"""
    if not key:
        return None
    with _lock:
        _load()
        return _mem.get(key)


def put(key: str, findings: list[dict[str, Any]]) -> None:
    """写入并落盘；超出容量上限时按插入顺序淘汰最旧。落盘失败仅内存生效。"""
    if not key:
        return
    with _lock:
        _load()
        _mem[key] = findings
        while len(_mem) > _MAX_ENTRIES:
            _mem.pop(next(iter(_mem)))
        try:
            _CACHE_FILE.write_text(json.dumps(_mem, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("审核结论缓存落盘失败(仅内存生效): %s", exc)
