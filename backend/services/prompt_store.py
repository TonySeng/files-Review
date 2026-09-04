# -*- coding: utf-8 -*-
"""Prompt 统一管理存储层。

系统中所有 LLM 提示词模板集中于此，供前端页面可视化管理：

- **集中注册**：9 个内置模板（审核 4 / 一致性 3 / 法规挖掘 2），代码内置默认值。
- **运行时渲染**：模板使用 ``{{placeholder}}`` 占位符（双花括号，避免与提示词中的
  JSON 花括号冲突），``render()`` 按变量表替换；未知占位符原样保留。
- **版本管理**：每次修改追加版本（可带备注），支持回滚（回滚=追加一条复制历史
  内容的新版本，保持历史线性可追溯）、版本间 diff、重置为内置默认。
- **持久化**：``backend/data/prompts.json``（与 users.json 同卷，容器重建不丢）。

调用方式：业务代码不再拼接提示词文本，而是 ``prompt_store.render(key, vars)``；
builder 函数（prompts.py 等）只负责计算动态片段后注入变量。
"""
from __future__ import annotations

import difflib
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any

from .. import config
from .. import storage

# prompts 集合经统一存储层持久化（data/storage_config.json 可切换后端）
_LOG_MAX_VERSIONS = 50  # 单个模板保留的版本数上限（超出淘汰最旧的非当前版本）

_lock = threading.RLock()
_items: dict[str, dict[str, Any]] = {}

# --------------------------------------------------------------------------- #
# 内置模板注册表（默认内容 = 重构前的原始提示词文本，占位符用 {{var}}）
# --------------------------------------------------------------------------- #

_BUILTIN_REVIEW_SYSTEM = """你是资深招投标合规审核专家,精通"招标投标法""招标投标法实施条例""政府采购法"及行业招采规范.

你的任务是依据招标文件要求和给定的审核规则,逐条核查投标文件及其附件的合规性与一致性.

工作准则:
1. 结论必须基于文档原文,禁止臆测.引用原文时使用简短摘录作为证据.
2. 区分「否决项」与「一般瑕疵」:资格性,实质性响应缺失属于否决项.
3. 若文档中确实找不到相关信息,状态判为 unknown,不要判为 fail.
4. 涉及法律法规量化标准(如保证金上限,投标有效期下限,禁止投标情形)时,调用 search_knowledge_base 工具检索法规依据,不要凭记忆作答.
5. 涉及最新政策、行业动态、市场信息时,可调用 web_search 工具获取实时信息.
6. 数字类核查(金额,工期,比例)必须做算术校验,指出具体差异.
7. 输出严格遵守要求的 JSON 结构,不要附加解释性文字.
8. 文字质量类规则(如错别字,语义)须逐字逐句校对,特别留意形近字/音近字被错误替换
   (如「投标保障金」应为「投标保证金」),不得用标点/格式噪声替代真正的错别字判定."""

_BUILTIN_REVIEW_RULE = """请依据以下规则审核文件.{{context_note}}
{{tender_block}}
## 审核规则
{{rules_block}}
{{extra_block}}
## 待审文件内容
{{docs_text}}

## 输出要求
对每条规则输出一个审核结论,返回 JSON:
{
  "findings": [
    {
      "rule_id": "规则ID",
      "status": "pass|fail|warn|unknown",
      "title": "一句话结论",
      "detail": "判定理由,说明核查过程",
      "evidence": "文档原文摘录(不超过200字),注明所在文件与页码",
      "location": "文件名 第X页/第X章",
      "suggestion": "整改建议,status=pass 时留空",
      "legal_basis": "法规依据,无则留空",
      "involved_files": ["涉及的文件名"],
      "confidence": 0.0,
      "typo": null
    }
  ]
}
必须覆盖全部 {{rule_count}} 条规则, rule_id 必须与上文一致. 只输出 JSON.

错别字/标点符号类规则(rule_id 为 gen-typo):
- 当 status 为 fail 或 warn 且存在字词替换类错误时,务必填写 "typo" 字段以提供结构化错字信息。
  "typo" 可为单个对象,也可为对象数组;同一规则下扫描到多处错别字时,必须逐条列出每一个(数组形式),不得只报第一条:
  单对象:  {"wrong": "原文中的错字/词", "correct": "应改正的字/词", "context": "包含该错字的原句(不超过60字)"}
  数组:    [{"wrong": "...", "correct": "...", "context": "..."}, {"wrong": "...", "correct": "...", "context": "..."}]
- 该字段用于系统跨审核去重与校验集沉淀;若非错别字类或判定为 pass,保持 typo 为 null.
- 注意:同一规则若全文扫描到多处「形近字/音近字替换、多字漏字、字序颠倒」等错别字,请在数组中逐条枚举,不可遗漏、不可合并为一条.

错别字类规则(含 gen-typo)务必逐字逐句扫描全文:形近字/音近字替换(如「保障金」应为「保证金」)、
多字漏字、字序颠倒均属错别字,不可遗漏;若仅发现标点/格式问题而无字词替换错误,应如实报出标点问题,
但 typo 字段仅用于字词替换类错误,无字词替换错误时 typo 保持 null,不要将标点噪声当作错别字."""

_BUILTIN_KB_PLAN = """你即将审核以下招投标合规规则.本地知识库收录了"招标投标法""招标投标法实施条例"
"政府采购法"等招采法律法规原文,可供检索.

请判断:审核这些规则时,是否需要查询法规条文以获得权威量化标准或禁止性情形认定?

## 待审规则
{{rules_block}}

## 判断标准
需要检索的情形:涉及法定上限/下限(如保证金比例与金额上限,投标有效期,公告期限),
法定禁止投标情形,法定否决条件,程序性强制要求.
不需要检索的情形:仅需比对招标文件与投标文件文本内容的核查项(如签章是否齐全,
报价算术是否正确,参数是否响应),这类判断不依赖法规条文.

## 输出要求
返回 JSON.若无需检索,queries 返回空数组:
{
  "queries": [
    {"query": "具体法规问题", "reason": "对应哪条规则,为何需要"}
  ]
}
最多 {{max_queries}} 个问题,问题要具体可检索.只输出 JSON."""

_BUILTIN_CONSISTENCY = """请对以下文件做一致性核查,找出同一信息在不同文件/位置取值矛盾的情况.

{{focus}}

{{content_intro}}
{{docs_text}}

## 输出要求
返回 JSON:
{
  "issues": [
    {
      "field": "不一致的信息项",
      "severity": "critical|major|minor",
      "description": "矛盾点描述",
      "values": [{"file": "文件名", "location": "位置", "value": "该处取值"}],
      "suggestion": "修正建议"
    }
  ]
}
只输出 JSON.未发现矛盾时 issues 为空数组."""

_BUILTIN_SEGMENT_SUMMARY = """请阅读以下【单篇文档的一个片段】，提取其中与"跨文件/跨位置一致性核查"相关的关键事实与取值，
便于后续比对不同文档或同一文档不同位置是否矛盾。

重点提取（若片段中涉及）：
- 投标人/招标人/供应商名称
- 项目名称、项目编号
- 投标总价/招标控制价（大写与小写若都有则都保留）
- 工期/交付期、投标有效期、质保期
- 关键日期：开标时间、投标截止时间、签署日期、资质证书有效期
- 项目经理/关键人员姓名、资质证书编号与有效期
- 设备型号、规格参数关键指标
- 保证金金额与比例
{{elements_block}}
## 片段内容
{{seg_text}}

## 输出要求
仅输出 JSON，且 summary 必须是【单个字符串】:即使存在多个要点，也必须用换行符(\\n)合并为一段多行文本，严禁输出数组/列表格式:
{
  "summary": "用要点列出上述关键事实与取值，保留原文中的具体数值与表述；片段未涉及的字段不要编造"
}"""

_BUILTIN_FILE_ELEMENTS = """请通读以下【单个文件】的全部内容，提取与"跨文件/跨位置一致性核查"相关的关键要素取值。

重点提取（若文件中涉及）：
- 投标人/招标人/供应商名称
- 项目名称、项目编号
- 投标总价/招标控制价（大写与小写若都有则都保留）
- 工期/交付期、投标有效期、质保期
- 关键日期：开标时间、投标截止时间、签署日期、资质证书有效期
- 项目经理/关键人员姓名、资质证书编号与有效期
- 设备型号、规格参数关键指标
- 保证金金额与比例
- 开户银行、银行账号、联系人、联系电话、地址{{elements_block}}
## 文件内容
{{doc_text}}

## 输出要求
仅输出 JSON：key 为要素名，value 为该要素在本文件中的【具体取值 + 简短出处(章节/位置)】；
文件未涉及的要素不要输出该 key；不得编造。
若上面列出了核心要素，其【规范名必须作为 key】，文中出现的别名/同义写法请归并到该规范 key 下
（例如别名「工程名称」的取值并入 key「项目名称」），以保证不同文件的 key 可直接对齐比对。示例:
{
  "投标人名称": "XX公司（见封面）",
  "投标总价": "人民币壹仟贰佰万元整（¥12,000,000，见报价表）"
}"""

_BUILTIN_TENDER_SUMMARY = """请从招标文件中提取对投标文件编制具有约束力的关键要求,供后续合规审核比对使用.

## 招标文件内容
{{tender_text}}

## 输出要求
返回 JSON:
{
  "project_name": "项目名称",
  "project_no": "项目编号",
  "budget": "最高限价或预算",
  "bid_bond": "投标保证金要求",
  "bid_validity": "投标有效期要求",
  "duration": "工期/交付期要求",
  "qualifications": ["资格要求逐条"],
  "personnel": ["人员要求逐条"],
  "performance": ["业绩要求逐条"],
  "star_items": ["带★或实质性响应条款"],
  "doc_composition": ["投标文件应包含的组成部分"],
  "seal_requirements": ["签字盖章要求"],
  "reject_clauses": ["否决投标情形"],
  "deadlines": ["关键时间节点"]
}
未提及的字段填空字符串或空数组.只输出 JSON."""

_BUILTIN_MINING_SYSTEM = """你是法规结构化抽取引擎，负责把法律法规/规范性文件转成「可用于审核招标文件与投标文件」的合规检查规则。

【抽取原则】
1. 只抽取**可被核查**的义务性、禁止性、条件性条款：含明确主体、行为、标准、数值阈值、时限、形式要求。
2. 必须跳过：立法目的、适用范围、术语定义（除非定义直接影响合规判断）、部门职责分工、法律责任中的罚则细则（可提炼为禁止性规则）、施行日期、附则、目录、页眉页脚、页码。
3. 每条规则的 checkpoints 是「审核员逐项做什么」的动作清单，2~5 条，具体到字段/数值/文件位置；不要写成空泛的"检查是否合规"。
4. **严禁编造**：legal_basis 必须是原文中真实出现的条/款/项编号与要点；文件中没有的内容绝不输出。宁可少抽，不可虚构。
5. 一条原文条款若含多个独立可核查要求，拆成多条规则；同一要求的不同表述只保留一条。
6. 若某片段中确实没有任何可核查条款，返回空 rules 数组，不要凑数。

【严重级别判定】
- critical：缺失/违反即导致投标被否决或文件违法（资格性、实质性要求、法定禁止情形）。
- major：重要合规瑕疵，通常需要澄清或补正。
- minor：形式性、表述性瑕疵。
- info：提示性事项。

【类别取值】只能取以下之一：qualification（资格资质）、commercial（商务报价与保证金）、technical（技术规格）、format（形式与签章）、legal（法律合规，默认）、tender_quality（招标文件质量）、general_quality（通用质量）。**禁止使用 consistency**。

【structured_hint 半自动结构化】（可选字段，仅当条件可被程序直接判定时输出）
当且仅当某条规则的判定条件能映射为下列**确定性可计算**形态时，在该规则中额外输出 structured_hint 对象；映射不上就**不要输出**该字段（宁缺勿滥，错误的结构化条件会导致误判）：
{
  "structured_hint": {
    "forbid_keywords": ["命中即违规的禁止性词语"],            // 条款明令禁止出现的表述，如 "串通投标"
    "require_elements": ["文件必须包含的要素词"],              // 条款强制要求出现的要素，如 "履约保证金"
    "amount_thresholds": [{"field": "字段名", "max": 800000}], // 金额上限（元），如 投标保证金不得超过项目估算价的2%且不超过80万 → max 直接给绝对上限的元数
    "amount_pair_diff": [{"a_field": ["字段A"], "b_field": ["字段B"], "max_abs_diff": 0, "fail_when": "le"}]  // 两金额差值比较，仅当条款要求「两个数值应(不)高度相似」时使用
  }
}
注意：
- amount_thresholds 的 max 一律换算为**元**（80万元 → 800000）。
- 时限类条件（如"30日内"）无法用上述形态表达，**不要**硬套 amount_thresholds，留给 checkpoints 人工核查。
- structured_hint 与 checkpoints 并不冲突：hint 供确定性引擎预检，checkpoints 供审核员执行。

【输出格式】只输出 JSON，不要附加任何解释文字：
{
  "rules": [
    {
      "name": "规则名（不超过40字，动宾结构，体现可核查点）",
      "category": "legal",
      "severity": "major",
      "description": "规则含义与判定标准（不超过200字）",
      "checkpoints": ["审核动作1", "审核动作2"],
      "legal_basis": "第X条（原文要点摘录，不超过100字）",
      "applies_to": "bid | tender | both",
      "structured_hint": {"forbid_keywords": ["..."], "require_elements": ["..."]}
    }
  ]
}"""

_BUILTIN_MINING_USER = """以下是法规文件正文的第 {{index}}/{{total}} 段。{{target}}。
请只依据本段原文抽取可核查规则，输出 JSON。

---- 法规原文开始 ----
{{chunk}}
---- 法规原文结束 ----"""


# key → (名称, 类别, 说明, 默认内容)
BUILTIN_PROMPTS: dict[str, dict[str, str]] = {
    "review_system": {
        "name": "审核系统提示词",
        "category": "合规审核",
        "description": "合规审核引擎的 system 提示词：定义审核专家角色、工作准则与工具使用规范。作用于每条规则核查与招标要点提取调用。",
        "content": _BUILTIN_REVIEW_SYSTEM,
    },
    "review_rule": {
        "name": "规则核查提示词",
        "category": "合规审核",
        "description": "逐条规则核查的主提示词（user 消息）。变量由引擎动态注入：规则清单、招标要求、待审文件内容等。",
        "content": _BUILTIN_REVIEW_RULE,
    },
    "kb_plan": {
        "name": "知识库检索规划",
        "category": "合规审核",
        "description": "让模型判断哪些规则需要检索法规知识库并生成检索问题（need_legal_basis 规则的预检索路径）。",
        "content": _BUILTIN_KB_PLAN,
    },
    "tender_summary": {
        "name": "招标要点提取",
        "category": "合规审核",
        "description": "从招标文件提取对投标文件有约束力的关键要求（项目信息、保证金、资格/人员/业绩要求、否决条款等），供投标审核比对。",
        "content": _BUILTIN_TENDER_SUMMARY,
    },
    "consistency": {
        "name": "一致性核查",
        "category": "一致性核查",
        "description": "跨文件/跨位置一致性核查主提示词。变量 focus（核查重点）、content_intro（内容区标题）、docs_text（文件内容）由引擎按模式与规则配置动态生成。",
        "content": _BUILTIN_CONSISTENCY,
    },
    "consistency_segment_summary": {
        "name": "一致性片段摘要",
        "category": "一致性核查",
        "description": "对单篇文档的一个片段提取一致性关键事实（分段摘要路径，summarized=True 时使用）。",
        "content": _BUILTIN_SEGMENT_SUMMARY,
    },
    "consistency_file_elements": {
        "name": "一致性要素提取",
        "category": "一致性核查",
        "description": "从单个文件一次性提取一致性核查要素取值（结构化提取路径，structured=True 时使用）。",
        "content": _BUILTIN_FILE_ELEMENTS,
    },
    "legal_mining_system": {
        "name": "法规挖掘系统提示词",
        "category": "法规挖掘",
        "description": "法规文件 → 审核规则的抽取引擎 system 提示词：抽取原则、严重级别判定、类别取值、structured_hint 半自动结构化与输出格式。",
        "content": _BUILTIN_MINING_SYSTEM,
    },
    "legal_mining_user": {
        "name": "法规挖掘分块指令",
        "category": "法规挖掘",
        "description": "法规逐块抽取的 user 消息模板。变量：index/total（块序号）、target（审核对象说明）、chunk（法规原文块）。",
        "content": _BUILTIN_MINING_USER,
    },
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #

def _save() -> None:
    storage.write_collection("prompts", _items)


def _load() -> None:
    global _items
    raw = storage.read_collection("prompts")
    _items = raw if isinstance(raw, dict) else {}


def _persist_on_switch(_old_driver: str, _new_driver: str) -> None:
    """存储后端热切换时，把当前内存态整写到新后端（数据以内存为权威）。"""
    storage.write_collection("prompts", _items)

storage.on_structured_switch(_persist_on_switch)

_load()


def _ensure(key: str) -> dict[str, Any]:
    """取记录，不存在则用内置默认播种 v1（保证新增模板自动出现在管理页）。"""
    rec = _items.get(key)
    if rec:
        return rec
    meta = BUILTIN_PROMPTS.get(key)
    if not meta:
        raise KeyError(f"未注册的提示词模板: {key}")
    rec = {
        "key": key,
        "name": meta["name"],
        "category": meta["category"],
        "description": meta["description"],
        "current_version": 1,
        "versions": [
            {
                "version": 1,
                "content": meta["content"],
                "comment": "内置初始版本",
                "updated_by": "system",
                "updated_at": _now(),
            }
        ],
        "updated_at": _now(),
    }
    _items[key] = rec
    return rec


# --------------------------------------------------------------------------- #
# 对外 API
# --------------------------------------------------------------------------- #

def list_prompts() -> list[dict[str, Any]]:
    """全部模板（不含内容，含版本数与占位符，供列表页）。"""
    with _lock:
        out: list[dict[str, Any]] = []
        for key in BUILTIN_PROMPTS:
            rec = json.loads(json.dumps(_ensure(key)))
            rec["version_count"] = len(rec.pop("versions"))
            content = BUILTIN_PROMPTS[key]["content"]
            rec["placeholders"] = _PLACEHOLDER_RE.findall(content)
            out.append(rec)
        return out


def get_prompt(key: str) -> dict[str, Any] | None:
    """单个模板详情：元数据 + 当前内容 + 全部版本（含内容）。"""
    with _lock:
        if key not in BUILTIN_PROMPTS:
            return None
        rec = json.loads(json.dumps(_ensure(key)))
        return rec


def get_template(key: str) -> str:
    """当前生效版本的模板原文（渲染前）。"""
    with _lock:
        return str(_ensure(key)["versions"][-1]["content"])


def render(key: str, variables: dict[str, Any] | None = None) -> str:
    """渲染模板：{{placeholder}} 按变量表替换；未提供的占位符原样保留。"""
    tpl = get_template(key)
    if not variables:
        return tpl

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in variables:
            return str(variables[name] if variables[name] is not None else "")
        return m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, tpl)


def detect_placeholders(content: str) -> list[str]:
    """检测模板中出现的占位符名（按出现顺序去重）。"""
    seen: list[str] = []
    for name in _PLACEHOLDER_RE.findall(content or ""):
        if name not in seen:
            seen.append(name)
    return seen


def _append_version(rec: dict[str, Any], content: str, comment: str, updated_by: str) -> None:
    """追加新版本并设为当前（回滚/重置/编辑共用：历史线性可追溯）。"""
    versions = rec["versions"]
    next_v = int(rec.get("current_version") or len(versions)) + 1
    # 淘汰最旧版本，防无限膨胀（保留当前与最近 _LOG_MAX_VERSIONS-1 条）
    if len(versions) >= _LOG_MAX_VERSIONS:
        versions[:] = versions[-(_LOG_MAX_VERSIONS - 1):]
    versions.append(
        {
            "version": next_v,
            "content": content,
            "comment": (comment or "").strip()[:200] or "未填写备注",
            "updated_by": updated_by or "admin",
            "updated_at": _now(),
        }
    )
    rec["current_version"] = next_v
    rec["updated_at"] = _now()


def update_prompt(key: str, content: str, comment: str, updated_by: str) -> dict[str, Any]:
    """编辑模板内容 → 生成新版本。"""
    if key not in BUILTIN_PROMPTS:
        raise KeyError(f"未注册的提示词模板: {key}")
    content = (content or "").strip()
    if not content:
        raise ValueError("模板内容不能为空")
    with _lock:
        rec = _ensure(key)
        _append_version(rec, content, comment or "编辑模板", updated_by)
        _save()
        return json.loads(json.dumps(rec))


def rollback_prompt(key: str, version: int, updated_by: str) -> dict[str, Any]:
    """回滚到历史版本：复制该版本内容为新版本（当前版本指针前移，历史保留）。"""
    with _lock:
        rec = _ensure(key)
        src = next((v for v in rec["versions"] if int(v["version"]) == int(version)), None)
        if not src:
            raise ValueError(f"版本 v{version} 不存在")
        _append_version(rec, src["content"], f"回滚自 v{version}", updated_by)
        _save()
        return json.loads(json.dumps(rec))


def reset_prompt(key: str, updated_by: str) -> dict[str, Any]:
    """重置为代码内置默认（追加新版本，不丢历史）。"""
    if key not in BUILTIN_PROMPTS:
        raise KeyError(f"未注册的提示词模板: {key}")
    with _lock:
        rec = _ensure(key)
        _append_version(rec, BUILTIN_PROMPTS[key]["content"], "重置为内置默认", updated_by)
        _save()
        return json.loads(json.dumps(rec))


def diff_versions(key: str, v1: int, v2: int) -> dict[str, Any]:
    """两个版本的统一 diff。返回逐行 diff 与统计。"""
    with _lock:
        rec = _ensure(key)
        c1 = next((v["content"] for v in rec["versions"] if int(v["version"]) == int(v1)), None)
        c2 = next((v["content"] for v in rec["versions"] if int(v["version"]) == int(v2)), None)
    if c1 is None or c2 is None:
        raise ValueError("版本不存在")
    lines = list(
        difflib.unified_diff(
            c1.splitlines(), c2.splitlines(),
            fromfile=f"v{v1}", tofile=f"v{v2}", lineterm="", n=2,
        )
    )
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return {"v1": v1, "v2": v2, "added": added, "removed": removed, "diff": lines}


def is_customized(key: str) -> bool:
    """当前版本内容是否与内置默认不同（前端「已自定义」标记）。"""
    with _lock:
        rec = _ensure(key)
        return str(rec["versions"][-1]["content"]) != BUILTIN_PROMPTS[key]["content"]
