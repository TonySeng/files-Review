# -*- coding: utf-8 -*-
"""版本化与确定性执行支撑模块。

目标：把审核系统变成「稳定工业流水线」——同样的输入、同样的规则版本、同样的引擎版本、
同样的依赖数据快照，必然产生同样的输出。

本模块集中管理四类版本信息：
1. 引擎版本（engine_version）：审核服务/算法/依赖库版本，构建时注入（环境变量或文件），
   缺省回落到代码内置版本号。
2. 规则集版本（rule_set_version）：每套规则（内置模式集 / 自定义规则集）的唯一语义化版本，
   例如 RULE_SET_v2026.08.24。规则变更走发布流程，旧版本保留可回放。
3. 依赖数据快照版本（data_snapshot_version）：规则依赖的外部数据（黑名单/牌照库/法规条款库/
   敏感词库）做快照版本，审核时明确使用哪个版本，否则同一文件在不同时间可能因外部数据变化
   产生不同结果。当前系统主要外部依赖为 KB 知识库，故以 KB 的标识 + 基线日期作为快照版本。
4. 解析器版本（parser_version）：文件解析、OCR、文本提取环节的版本，固定解析器版本并
   对「解析结果快照」做哈希，审核基于快照而非原始文件流。

以及三类环境常量（统一编码/时区/语言），避免字符/日期判断因环境差异而波动：
- 编码：UTF-8
- 时区：Asia/Shanghai
- 语言：zh-CN

版本信息在每次审核时随结果一起存证（audit 表），支撑「按历史版本重跑」与一致性监控。
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

# --------------------------------------------------------------------------- #
# 环境常量（统一编码 / 时区 / 语言，避免环境差异导致的判断波动）
# --------------------------------------------------------------------------- #
ENV_ENCODING = "UTF-8"
ENV_TIMEZONE = "Asia/Shanghai"
ENV_LOCALE = "zh-CN"

# 引擎默认版本（构建未注入时回落）。语义化版本与镜像标签保持一致。
DEFAULT_ENGINE_VERSION = "v1.0.0"

# 数据快照默认基线（KB 知识库未提供独立版本标识时回落）。
# 实际部署若知识库有发布版本，应经环境变量 BCR_DATA_SNAPSHOT_VERSION 注入。
DEFAULT_DATA_SNAPSHOT_VERSION = "KB_BASELINE_v2026.08.01"


def _engine_version_from_env() -> str | None:
    """从环境变量读取引擎版本（容器镜像构建时注入 APP_VERSION）。"""
    v = os.getenv("BCR_ENGINE_VERSION") or os.getenv("APP_VERSION")
    return v.strip() if v and v.strip() else None


@lru_cache(maxsize=1)
def engine_version() -> str:
    """审核引擎版本（服务/算法/依赖库/模型接口层的整体版本）。"""
    from_env = _engine_version_from_env()
    if from_env:
        return from_env
    # 优先读取 pyproject 注入的包版本；读不到则回落默认
    try:
        return importlib.metadata.version("bidding-review-backend") or DEFAULT_ENGINE_VERSION
    except Exception:
        return DEFAULT_ENGINE_VERSION


def parser_version() -> str:
    """解析器版本：组合 PyMuPDF / python-docx / openpyxl / OCR 客户端 的版本。

    解析器版本差异（如 PyMuPDF 升级导致文本提取换行策略变化）会直接影响解析结果，
    故纳入版本指纹，确保「解析结果快照」可在同版本引擎下复现。
    """
    parts: list[str] = []
    for mod, label in (
        ("fitz", "pymupdf"),
        ("docx", "docx"),
        ("openpyxl", "openpyxl"),
        ("xlrd", "xlrd"),
    ):
        try:
            import importlib

            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", None) or "?"
        except Exception:
            ver = "na"
        parts.append(f"{label}-{ver}")
    # OCR 客户端版本（图聆云为外部服务，仅记调用端版本标签）
    parts.append("ocr-tuling")
    return "|".join(parts)


def data_snapshot_version() -> str:
    """依赖数据快照版本：规则依赖的外部数据（KB 知识库等）的版本标识。

    审核时明确使用哪个版本，否则同一文件在不同时间可能因外部数据变化产生不同结果。
    优先级：环境变量注入 > 配置项 > 默认基线。
    """
    from .. import config

    v = (
        os.getenv("BCR_DATA_SNAPSHOT_VERSION")
        or config.get("data_snapshot_version")
        or DEFAULT_DATA_SNAPSHOT_VERSION
    )
    return str(v)


def engine_build_time() -> str:
    """引擎构建时间（ISO，UTC）。容器构建时注入 BUILD_TIME，缺省为空串。"""
    return os.getenv("BCR_BUILD_TIME", "") or ""


def environment_profile() -> dict[str, str]:
    """环境常量快照，随审计记录存证，确保可重放时环境一致。"""
    return {
        "encoding": ENV_ENCODING,
        "timezone": ENV_TIMEZONE,
        "locale": ENV_LOCALE,
    }


# --------------------------------------------------------------------------- #
# 解析结果快照指纹
# --------------------------------------------------------------------------- #
def parsed_content_hash(
    text: str,
    *,
    parser_ver: str | None = None,
    encoding: str = ENV_ENCODING,
) -> str:
    """对「解析结果快照」做哈希。

    固定解析器版本 + 归一化文本（统一换行/空白），确保同一种子文件在同版本解析器下
    始终得到同一指纹，从而使审核基于「解析结果快照」而非易变的原始文件流。

    Args:
        text: 解析后的结构化文本内容（已拼接页码/表格标注）。
        parser_ver: 解析器版本；缺省时实时计算。
        encoding: 文本编码，统一 UTF-8。
    """
    pv = parser_ver or parser_version()
    # 归一化：统一行尾为 \n，折叠多余空行，strip 尾部空白——
    # 消除不同平台/解析器在空白处理上的微小差异带来的哈希漂移。
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    normalized = normalized.strip()
    base = f"{pv}||{encoding}||{normalized}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def file_content_hash(content: bytes) -> str:
    """文件内容指纹：SHA-256，确保每次审核的是同一份内容。"""
    return hashlib.sha256(content).hexdigest()


def rule_content_hash(rule: dict[str, Any]) -> str:
    """规则内容指纹：覆盖所有影响审核结论的定义字段，任一字段变更即指纹漂移。

    用于结论缓存 / 一致性缓存的键——避免「规则 ID 不变但定义已改」时仍命中旧缓存、
    返回陈旧结论。仅取语义相关字段，排除运行时/展示层字段（如 enabled、命中计数等）。
    """
    if not isinstance(rule, dict):
        return ""
    ce = rule.get("consistency_elements")
    if ce is None and isinstance(rule.get("structured"), dict):
        ce = rule["structured"].get("consistency_elements")
    payload = {
        "id": rule.get("id"),
        "name": rule.get("name"),
        "category": rule.get("category"),
        "severity": rule.get("severity"),
        "description": rule.get("description"),
        "checkpoints": rule.get("checkpoints"),
        "need_legal_basis": rule.get("need_legal_basis"),
        "deterministic": rule.get("deterministic"),
        "structured": rule.get("structured"),
        "consistency_elements": ce,
        "thresholds": rule.get("thresholds"),
        "focus": rule.get("focus"),
        # 关联文档类型/章节会改变送审正文范围，必须纳入指纹，
        # 否则「改了章节但缓存未失效」会返回按旧范围得出的结论。
        "doc_types": rule.get("doc_types"),
        "section_ids": rule.get("section_ids"),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def consistency_rule_token(rules: list[dict[str, Any]]) -> str:
    """一致性规则内容指纹聚合：取所有一致性类规则的内容指纹，排序后整体哈希。

    作为一致性缓存键的一部分，使「一致性规则的 checkpoints/description/要素 变更」
    即让摘要与要素提取缓存失效，避免 stale 摘要被复用。
    """
    from . import rules_store  # 延迟导入，避免模块级循环依赖

    fps = sorted(
        rule_content_hash(r) for r in (rules or []) if rules_store.is_consistency_rule(r)
    )
    return hashlib.sha256("|".join(fps).encode("utf-8")).hexdigest()[:24]


def doc_group_parsed_hash(docs: list[dict[str, Any]]) -> str:
    """整组文档的解析结果快照指纹：对各文档解析哈希按 (file_id, hash) 排序后整体哈希。

    用于存证「这批文件审核时的解析状态」，支撑按历史版本重跑时校验解析一致性。
    """
    items: list[tuple[str, str]] = []
    for d in docs:
        fid = str(d.get("file_id") or d.get("md5") or "")
        # 优先用已计算的 parsed_hash；否则现场计算
        ph = d.get("parsed_hash") or parsed_content_hash(d.get("text") or "")
        items.append((fid, ph))
    items.sort()
    base = "|".join(f"{fid}:{h}" for fid, h in items)
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# 规则集版本管理
# --------------------------------------------------------------------------- #
# 规则集版本映射：mode-* 合成集 / 自定义规则集 → 语义化版本号。
# 规则变更走发布流程，旧版本保留（不覆盖内置版本字符串，仅追加发布说明）。
# 版本号格式：RULE_SET_<模式>_vYYYY.MM.DD；自定义集以 id 哈希 + 发布日期标识。
_RULESET_BASELINE_DATE = "2026.08.24"


def rule_set_version_for(mode: str, content_hash: str = "") -> str:
    """内置模式集（bid/tender/general）的版本号。

    content_hash：规则内容指纹短码（若有），追加到版本号尾部，
    使「规则内容变更即版本漂移可检测」，达成需求 1.1「规则版本锁定」。
    """
    m = (mode or "general").lower()
    suffix = f"_{content_hash}" if content_hash else ""
    return f"RULE_SET_{m}_v{_RULESET_BASELINE_DATE}{suffix}"


def rule_set_version_for_custom(
    ruleset_id: str, published_at: str | None = None, content_hash: str = ""
) -> str:
    """自定义规则集版本号：以其 id 短哈希 + 发布日期 + 内容哈希标识，确保唯一且可追溯。

    Args:
        ruleset_id: 自定义规则集 id（如 rs-abcdef12）。
        published_at: 发布时间（ISO）；缺省使用规则集基线日期。
        content_hash: 规则内容指纹短码（若有），追加以检测内容漂移。
    """
    short = hashlib.sha256(ruleset_id.encode("utf-8")).hexdigest()[:8]
    date_tag = (published_at or _RULESET_BASELINE_DATE).replace("-", ".")[:10]
    suffix = f"_{content_hash}" if content_hash else ""
    return f"RULE_SET_CUSTOM_{short}_v{date_tag}{suffix}"


def rule_content_fingerprint(rules: list[dict[str, Any]]) -> str:
    """规则内容指纹：对规则组按 id 排序后取其 name/description/checkpoints/structured
    的稳定 JSON 做 SHA-256 短码。

    规则内容（含新增的 structured 可执行条件）一旦变更，指纹即变，从而驱动
    rule_set_version 漂移，使「同一文件在不同时间因规则变更而产生不同结果」可检测、
    可回放（需求 1.1 / 1.2）。
    """
    items = []
    for r in sorted(rules, key=lambda x: str(x.get("id") or "")):
        items.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "description": r.get("description"),
                "checkpoints": r.get("checkpoints") or [],
                "structured": r.get("structured") or None,
            }
        )
    blob = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def resolve_rule_set_version(
    *,
    mode: str | None,
    ruleset_id: str | None = None,
    rule_group_ids: list[str] | None = None,
    auto_match: bool = False,
    rules: list[dict[str, Any]] | None = None,
) -> str:
    """根据本次任务的规则来源解析规则集版本（含内容哈希，支持漂移检测）。

    优先级：
    1. 真实自定义规则集（非 mode-*）→ 自定义版本号（含发布日期 + 内容哈希）。
    2. 规则组（多选 + 自动匹配）→ 组内规则按其内容指纹联合版本。
    3. 内置模式集（mode-*）→ 对应模式版本号（含内容哈希）。
    """
    from ..services import rules_store

    # 1) 真实自定义规则集
    if ruleset_id and not str(ruleset_id).startswith("mode-"):
        rs = rules_store.get_ruleset(ruleset_id)
        published = (rs or {}).get("published_at")
        content = rule_content_fingerprint(rs.get("rules", []) if rs else [])
        return rule_set_version_for_custom(ruleset_id, published, content)

    # 2) 规则组：展开全部规则，取其内容指纹联合版本
    if rule_group_ids:
        from ..services import rule_groups

        expanded = rule_groups.expand_rule_groups(rule_group_ids)
        if expanded:
            content = rule_content_fingerprint(expanded)
            return f"RULE_GROUP_v{content}_{_RULESET_BASELINE_DATE}"

    # 3) 内置模式集：用传入 rules 或按 mode 取出全量规则计算内容哈希
    if rules is None:
        rules = rules_store.get_rules_by_mode((mode or "general").lower())
    content = rule_content_fingerprint(rules)
    return rule_set_version_for(mode or "general", content)


# --------------------------------------------------------------------------- #
# 完整版本指纹（用于审计存证与一致性比对）
# --------------------------------------------------------------------------- #
def build_version_manifest(
    *,
    docs: list[dict[str, Any]],
    mode: str | None,
    ruleset_id: str | None = None,
    rule_group_ids: list[str] | None = None,
    auto_match: bool = False,
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """组装一次审核的完整版本清单，随结果一起存证。

    包含：引擎版本、规则集版本（含内容哈希）、规则内容指纹、依赖数据快照版本、
    解析器版本、文件解析快照指纹、环境常量、时间（ISO，已按 Asia/Shanghai 标注）。
    """
    rule_set_version = resolve_rule_set_version(
        mode=mode,
        ruleset_id=ruleset_id,
        rule_group_ids=rule_group_ids,
        auto_match=auto_match,
        rules=rules,
    )
    content_hash = rule_content_fingerprint(rules or [])
    return {
        "engine_version": engine_version(),
        "engine_build_time": engine_build_time(),
        "rule_set_version": rule_set_version,
        "rule_content_hash": content_hash,
        "data_snapshot_version": data_snapshot_version(),
        "parser_version": parser_version(),
        "parsed_content_hash": doc_group_parsed_hash(docs),
        "environment": environment_profile(),
        "generated_at": _now_shanghai(),
    }


def _now_shanghai() -> str:
    """审核基准时间：统一按 Asia/Shanghai 生成，避免容器 UTC 时区导致日期错位。

    注意：本时间仅用于「审计存证的时间戳」，不用于任何判断逻辑（判断逻辑不依赖时间）。
    """
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(ENV_TIMEZONE)
        return datetime.now(tz).isoformat(timespec="seconds")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
