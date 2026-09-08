"""审核提示词模板.

模板正文已迁移至 prompt_store 统一管理（后台页面可编辑/版本化/测试），
本模块只负责计算动态片段（规则清单、要素块等）并调用 prompt_store.render()。
SYSTEM 常量保留为内置默认的静态引用（兼容既有引用方），运行时请用 system_prompt()。
"""
from __future__ import annotations

from typing import Any

from . import prompt_store

SYSTEM = prompt_store.BUILTIN_PROMPTS["review_system"]["content"]


def system_prompt() -> str:
    """审核 system 提示词（当前生效版本，支持后台自定义）。"""
    return prompt_store.render("review_system")

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "检索本地招采法律法规知识库,获取权威条文依据."
                "适用于需要法规量化标准或禁止性情形认定的场景."
                "不要用于查询投标文件自身内容."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "具体的法规问题,例如「投标保证金金额上限是多少」",
                    },
                    "reason": {
                        "type": "string",
                        "description": "说明为何需要检索该法规",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索最新政策、行业动态、技术标准、市场信息等实时内容."
                "适用于需要查询最新政策文件、行业规范更新、技术发展趋势等场景."
                "不要用于查询基础法规条文(使用 search_knowledge_base)或文档自身内容."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词,例如「2024年政府采购最新政策」",
                    },
                    "reason": {
                        "type": "string",
                        "description": "说明为何需要联网搜索",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def build_rule_prompt(
    rules: list[dict[str, Any]],
    docs_text: str,
    tender_summary: str,
    extra_instruction: str = "",
    mode: str = "bid",
) -> str:
    rule_lines: list[str] = []
    for idx, rule in enumerate(rules, 1):
        points = "\n".join(f"     - {p}" for p in rule.get("checkpoints", []))
        rule_lines.append(
            f"{idx}. [{rule['id']}] {rule['name']}(类别={rule.get('category')},"
            f"严重级别={rule.get('severity')})\n"
            f"   说明:{rule.get('description', '')}\n"
            f"   审核要点:\n{points}"
        )
    rules_block = "\n\n".join(rule_lines)

    # 投标审核:关注招标要求响应性
    if mode == "bid":
        tender_block = (
            f"\n## 招标文件关键要求（仅作背景理解，当前规则的具体审核对象与判定标准以上文「审核规则」为准，"
            f"不得把其中的人员要求、资格要求、业绩要求、否决投标情形等摘要标签直接作为本规则的结论）\n{tender_summary}\n"
            if tender_summary.strip() else ""
        )
        context_note = "对每条规则核查投标文件是否符合招标要求与合规规范."
    # 招标审核:关注合规性与完备性
    elif mode == "tender":
        tender_block = ""
        context_note = "对每条规则核查招标文件自身的合规性,完整性和法律适当性."
    # 通用审核:独立核查
    else:
        tender_block = ""
        context_note = "对每条规则独立核查文件合规性."

    extra_block = (
        f"\n## 补充审核要求\n{extra_instruction}\n" if extra_instruction.strip() else ""
    )

    return prompt_store.render(
        "review_rule",
        {
            "context_note": context_note,
            "tender_block": tender_block,
            "rules_block": rules_block,
            "extra_block": extra_block,
            "docs_text": docs_text,
            "rule_count": len(rules),
        },
    )


def build_kb_plan_prompt(rules: list[dict[str, Any]], max_queries: int = 6) -> str:
    """让模型自主判断哪些规则需要检索法规知识库."""
    rule_lines = "\n".join(
        f"- [{r['id']}] {r['name']}:{r.get('description', '')}\n"
        f"  要点:{';'.join(r.get('checkpoints', []))}"
        for r in rules
    )
    return prompt_store.render(
        "kb_plan",
        {"rules_block": rule_lines, "max_queries": max_queries},
    )


def format_elements_block(elements: list[Any] | None, title: str) -> str:
    """把一致性要素清单渲染成提示词片段。

    要素既可以是规范名字符串（旧格式），也可以是
    {"name", "synonyms", "note"} 结构（规则显式声明的新格式）。
    """
    if not elements:
        return ""
    lines: list[str] = []
    for e in elements:
        if isinstance(e, dict):
            name = str(e.get("name") or "").strip()
            if not name:
                continue
            syns = [str(s).strip() for s in (e.get("synonyms") or []) if str(s).strip()]
            syns = [s for s in syns if s != name]
            note = str(e.get("note") or "").strip()
            line = f"- {name}"
            if syns:
                line += f"（别名：{'、'.join(syns[:8])}）"
            if note:
                line += f"｜比对要求：{note}"
            lines.append(line)
        else:
            name = str(e).strip()
            if name:
                lines.append(f"- {name}")
    if not lines:
        return ""
    return f"\n\n## {title}\n" + "\n".join(lines) + "\n"


def build_consistency_prompt(
    docs_text: str,
    mode: str = "bid",
    multi_doc: bool = True,
    summarized: bool = False,
    elements: list[Any] | None = None,
    structured: bool = False,
    focus_points: list[str] | None = None,
) -> str:
    # 招标审核:无需跨文件一致性(招标文件通常是单包)
    if mode == "tender":
        focus = """招标文件内部一致性核查,重点:
- 项目名称,编号在不同章节是否一致
- 投标保证金金额与比例是否匹配项目预算/最高限价
- 开标时间,投标截止时间,答疑时间的逻辑顺序
- 资格条件在正文与评分细则中的表述是否矛盾
- 技术参数要求在不同章节是否冲突"""
    # 投标审核:招标要求与投标响应的一致性
    else:
        focus = """投标文件与招标文件的交叉一致性核查,重点:
- 投标人名称,项目名称与编号在所有文件中是否一致
- 投标总价(含大小写)在不同位置是否一致
- 工期与交付期,投标有效期,质保期在各文件中是否一致
- 项目经理与关键人员姓名,联系方式,资质证书编号与有效期
- 设备型号与规格参数在技术响应与报价表中是否匹配
- 日期逻辑(签署日期不得晚于开标日期,证书不得过期)"""

    # 单文件场景：不存在"跨文件"，强制收敛为「文件内部不同位置/章节」的取值一致性，
    # 避免模型臆测其它文件而产生虚假的跨文件矛盾。
    if not multi_doc:
        focus = (
            "（仅上传了单一文件，不存在跨文件比对，请仅聚焦该文件内部不同章节/位置之间"
            "同一信息的取值一致性，不要臆测或引用其它文件）\n" + focus
        )

    # 一致性类规则若声明了 checkpoints，则以规则配置为准（真正的「可配置」路径）；
    # 未配置时才回退到上面的内置默认关注点。
    if focus_points:
        fp_items = "\n".join(
            f"- {p}" for p in focus_points if str(p).strip()
        )
        if fp_items:
            focus = (
                "以下为本任务【一致性规则显式配置】的核查要点，请逐条核对"
                "（规则之外的通用一致性问题可一并指出）：\n" + fp_items
            )

    # 若本任务一致性规则声明了需核查的具体要素（如 项目名称、项目编号、交货日志、
    # 报价金额 等），将这些要素注入重点核对清单，要求模型逐项比对其在不同文件/位置是否一致。
    focus += format_elements_block(
        elements,
        "本任务一致性规则声明的核心要素（务必逐项核对这些要素在不同文件/位置是否一致）",
    )

    content_intro = (
        "## 各文件提取的一致性要素取值（以下为各文件一次性提取的要素取值 JSON，"
        "已按文件分段标注；请逐项比对同一要素在不同文件中的取值是否矛盾）"
        if structured
        else (
            "## 文件内容（以下为各文档经分段摘要后的关键事实，用于跨文件一致性比对；"
            "每段标注了来源文档与段落，请据此比对不同文档/位置是否矛盾）"
            if summarized
            else "## 文件内容"
        )
    )

    return prompt_store.render(
        "consistency",
        {"focus": focus, "content_intro": content_intro, "docs_text": docs_text},
    )


def build_segment_summary_prompt(
    seg_text: str, mode: str = "bid", elements: list[Any] | None = None
) -> str:
    """对单篇文档的一个片段做一致性关键事实摘要提取，供后续跨文件/文档内一致性比对。

    elements: 本任务一致性规则声明的重点核查要素（如 项目名称、项目编号、交货日志）。
    提供时模型会优先提取这些要素的具体取值，便于后续做要素级一致性校验。
    """
    elements_block = format_elements_block(
        elements,
        "本任务一致性规则特别关注的要素（请务必从片段中尽量提取其具体取值；"
        "若片段涉及，未涉及不要编造）",
    )
    return prompt_store.render(
        "consistency_segment_summary",
        {"elements_block": elements_block, "seg_text": seg_text},
    )


def build_file_elements_prompt(
    doc_text: str, mode: str = "bid", elements: list[Any] | None = None
) -> str:
    """从单个文件一次性提取一致性核查所需要素取值，返回紧凑结构化 JSON。

    与 build_segment_summary_prompt（逐段多次调用、串行、产出冗长自由文本）不同，
    本提示词要求模型用【单次调用】通读整篇文件并直接产出结构化要素取值，
    便于一致性阶段「每文件并行提取 → 紧凑比对」，从根本上消除逐段串行摘要的慢/超时问题。

    elements 支持两种格式：规范名字符串列表（旧）或
    {"name", "synonyms", "note"} 结构（一致性规则显式声明的新格式）。
    """
    elements_block = format_elements_block(
        elements,
        "本任务一致性规则特别关注的要素（务必逐项提取其在本文件中的具体取值；"
        "未涉及则该 key 不要出现，严禁编造）",
    )
    return prompt_store.render(
        "consistency_file_elements",
        {"elements_block": elements_block, "doc_text": doc_text},
    )


def build_tender_summary_prompt(tender_text: str) -> str:
    return prompt_store.render("tender_summary", {"tender_text": tender_text})
