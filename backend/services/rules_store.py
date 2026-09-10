# -*- coding: utf-8 -*-
"""合规审核规则存储：内置规则集 + 用户自定义规则集（本地 JSON 持久化）。

规则分为三类：
- 投标文件合规规则（BID_RULES）：17条
- 招标文件合规规则（TENDER_RULES）：12条
- 通用审核规则（GENERAL_RULES）：8条

审核模式与规则关系：
- 投标模式 = BID_RULES + GENERAL_RULES
- 招标模式 = TENDER_RULES + GENERAL_RULES
- 通用模式 = GENERAL_RULES
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

# 自定义规则集持久化目录：必须落在 Docker 挂载卷 /app/backend/data 内，
# 否则容器重建会被重置为镜像初始（空）状态，导致「重启即丢失」。
# 与 file_types.json / rule_groups.json 同目录策略保持一致。
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
from .. import storage  # noqa: E402  # 统一存储层

# custom_rulesets 集合经统一存储层持久化（data/storage_config.json 可切换后端）
_lock = threading.Lock()

# ===================== 一致性要素库 =====================
# 跨文件一致性核查的「核心要素」在此集中声明，供规则引用与前端下拉选用。
# 一致性类规则可在 structured.consistency_elements 中显式声明本规则要核查的要素；
# 未显式声明时，引擎会拿规则文本回查本库做同义词推断（向后兼容旧规则）。
#
# 单条结构：
#   name     : 要素规范名（引擎注入提示词、结果展示均用此名）
#   synonyms : 同义词/别名，引擎据此在文档中定位该要素的各种写法
#   group    : 前端下拉分组
#   note     : 该要素的默认比对要求（可为空）
CONSISTENCY_ELEMENT_LIBRARY: list[dict[str, Any]] = [
    # ---- 基础标识 ----
    {"name": "项目名称", "group": "基础标识", "synonyms": ["项目名称", "工程名称", "标的名称"], "note": ""},
    {"name": "项目编号", "group": "基础标识", "synonyms": ["项目编号", "标段编号", "招标编号", "项目代号", "采购编号"], "note": ""},
    {"name": "投标人名称", "group": "基础标识", "synonyms": ["投标人名称", "供应商名称", "投标单位", "承包人名称"], "note": "须与营业执照、公章、投标函完全一致"},
    {"name": "招标人名称", "group": "基础标识", "synonyms": ["招标人名称", "采购人名称", "发包人"], "note": ""},
    {"name": "统一社会信用代码", "group": "基础标识", "synonyms": ["统一社会信用代码", "信用代码", "纳税人识别号"], "note": ""},
    # ---- 金额 ----
    {"name": "报价金额", "group": "金额", "synonyms": ["报价金额", "投标总价", "投标报价", "投标金额", "最高限价", "招标控制价", "合同金额", "中标金额"], "note": "大小写金额须一致，且不得超过最高限价"},
    {"name": "投标保证金", "group": "金额", "synonyms": ["投标保证金", "保函金额", "担保金额", "保证金金额"], "note": ""},
    # ---- 时间 ----
    {"name": "工期", "group": "时间", "synonyms": ["工期", "交货期", "交付期", "供货期", "竣工日期"], "note": ""},
    {"name": "质保期", "group": "时间", "synonyms": ["质保期", "保修期", "质量保证期"], "note": ""},
    {"name": "投标有效期", "group": "时间", "synonyms": ["投标有效期", "投标保函有效期"], "note": ""},
    {"name": "开标时间", "group": "时间", "synonyms": ["开标时间", "开标日期"], "note": ""},
    {"name": "投标截止时间", "group": "时间", "synonyms": ["投标截止时间", "截标时间", "投标截止日期"], "note": ""},
    {"name": "签署日期", "group": "时间", "synonyms": ["签署日期", "签章日期", "落款日期", "签订日期"], "note": "签署日期不得晚于开标日期"},
    # ---- 人员与资质 ----
    {"name": "项目经理", "group": "人员与资质", "synonyms": ["项目经理", "项目负责人", "建造师"], "note": ""},
    {"name": "证书编号", "group": "人员与资质", "synonyms": ["证书编号", "资质证书编号", "注册编号"], "note": "证书须在有效期内"},
    # ---- 银行与账户 ----
    {"name": "开户银行", "group": "银行与账户", "synonyms": ["开户银行", "基本账户", "开户行"], "note": ""},
    {"name": "银行账号", "group": "银行与账户", "synonyms": ["银行账号", "投标保证金账户", "收款账号", "账户账号"], "note": ""},
    # ---- 联系信息 ----
    {"name": "地址", "group": "联系信息", "synonyms": ["注册地址", "住所地", "公司地址", "通讯地址"], "note": ""},
    {"name": "联系人", "group": "联系信息", "synonyms": ["联系人", "授权代表", "被授权人"], "note": ""},
    {"name": "联系电话", "group": "联系信息", "synonyms": ["联系电话", "联系方式", "手机号"], "note": ""},
    # ---- 其他 ----
    {"name": "交货日志", "group": "其他", "synonyms": ["交货日志", "供货记录", "交付日志", "到货记录", "发货记录"], "note": ""},
]


def list_consistency_library() -> list[dict[str, Any]]:
    """返回内置一致性要素库（副本，防止调用方改动污染模块常量）。"""
    return [dict(e, synonyms=list(e.get("synonyms") or [])) for e in CONSISTENCY_ELEMENT_LIBRARY]


def get_library_element(name: str) -> dict[str, Any] | None:
    """按规范名查要素库条目；命中同义词也返回对应规范条目。"""
    target = str(name or "").strip()
    if not target:
        return None
    for e in CONSISTENCY_ELEMENT_LIBRARY:
        if e["name"] == target:
            return e
    for e in CONSISTENCY_ELEMENT_LIBRARY:
        if target in (e.get("synonyms") or []):
            return e
    return None


def is_consistency_rule(rule: Any) -> bool:
    """判定一条规则是否属于「一致性核查」类。

    命中任一条件即为一致性规则：
    - category == "consistency"（推荐，规则编辑界面选「一致性」类别即生效）
    - id 以 "cons-" 开头
    - name 含「一致」且该规则未显式归入其它类别

    第三条保留是为了兼容历史规则，但加了类别保护：像「语义通顺性与逻辑一致性」
    这类已归类为 general_quality 的文字质量规则不会被误判为一致性规则。
    """
    if not isinstance(rule, dict):
        return False
    cat = str(rule.get("category") or "")
    if cat == "consistency":
        return True
    if str(rule.get("id") or "").startswith("cons-"):
        return True
    # 未显式归类（空）或仍为规则编辑器的默认类别 qualification 时，才按名称推断
    return "一致" in str(rule.get("name") or "") and cat in ("", "qualification")


def _infer_elements_from_text(text: str) -> list[dict[str, Any]]:
    """按要素库同义词回查规则文本，推断其关注的一致性要素（用于未显式声明的旧规则）。"""
    if not text:
        return []
    out: list[dict[str, Any]] = []
    for e in CONSISTENCY_ELEMENT_LIBRARY:
        syns = list(e.get("synonyms") or []) or [e["name"]]
        if any(syn in text for syn in syns):
            out.append(
                {
                    "name": e["name"],
                    "synonyms": list(e.get("synonyms") or []),
                    "note": str(e.get("note") or ""),
                }
            )
    return out


def collect_consistency_spec(rules: list[dict[str, Any]] | None) -> dict[str, Any]:
    """从任务规则中聚合出一致性核查规格（要素 + 核查要点 + 来源规则）。

    要素优先级：规则 structured.consistency_elements 显式声明 > 规则文本同义词推断。
    返回结构::
        {
          "rule_ids": [...], "rule_names": [...],
          "elements": [{"name", "synonyms", "note"}],
          "focus_points": [...],   # 来自一致性规则的 checkpoints
          "explicit": bool,        # 是否存在显式要素声明
        }
    """
    rule_ids: list[str] = []
    rule_names: list[str] = []
    elements: list[dict[str, Any]] = []
    focus_points: list[str] = []
    explicit = False
    for r in rules or []:
        if not is_consistency_rule(r):
            continue
        rid = str(r.get("id") or "")
        if rid:
            rule_ids.append(rid)
        rname = str(r.get("name") or "")
        if rname:
            rule_names.append(rname)
        declared = _normalize_consistency_elements(
            (r.get("structured") or {}).get("consistency_elements")
        )
        if declared:
            explicit = True
            elements.extend(declared)
        else:
            blob = " ".join(
                [
                    rname,
                    str(r.get("description") or ""),
                    " ".join(str(c) for c in (r.get("checkpoints") or [])),
                    " ".join(str(c) for c in (r.get("criteria") or [])),
                ]
            )
            elements.extend(_infer_elements_from_text(blob))
        for c in r.get("checkpoints") or []:
            c_str = str(c).strip()
            if c_str and c_str not in focus_points:
                focus_points.append(c_str)
    return {
        "rule_ids": rule_ids,
        "rule_names": rule_names,
        "elements": _merge_elements(elements),
        "focus_points": focus_points,
        "explicit": explicit,
    }


# ===================== 投标文件合规规则 =====================
BID_RULES: list[dict[str, Any]] = [
    # ---- 资格性审查（否决项为主）----
    {
        "id": "qual-license", "name": "营业执照与经营范围",
        "category": "qualification", "severity": "critical", "need_legal_basis": False,
        "description": "投标人须提供有效营业执照，经营范围覆盖招标标的。",
        "checkpoints": [
            "是否提供营业执照副本复印件并加盖公章",
            "营业执照是否在有效期内、是否通过年检",
            "经营范围是否覆盖本次招标采购标的",
            "投标人名称与营业执照、公章、投标函是否完全一致",
        ],
    },
    {
        "id": "qual-cert", "name": "资质证书与等级要求",
        "category": "qualification", "severity": "critical", "need_legal_basis": False,
        "description": "核查招标文件要求的行业资质、等级、认证是否齐备且有效。",
        "checkpoints": [
            "招标文件要求的资质证书是否全部提供",
            "资质等级是否达到最低要求",
            "证书是否在有效期内",
            "认证证书（如 ISO9001、CMMI、等级保护测评）是否有效",
        ],
    },
    {
        "id": "qual-personnel", "name": "项目人员配置与资格",
        "category": "qualification", "severity": "major", "need_legal_basis": False,
        "description": "项目经理及关键岗位人员的资格、社保、无兼职承诺。",
        "checkpoints": [
            "项目经理资格证书是否满足要求",
            "关键岗位人员数量、职称是否满足要求",
            "是否提供社保缴纳证明且单位与投标人一致",
            "拟派人员是否存在同时担任其他在建项目负责人的情形",
        ],
    },
    {
        "id": "qual-performance", "name": "业绩与案例证明",
        "category": "qualification", "severity": "major", "need_legal_basis": False,
        "description": "同类项目业绩的数量、金额、时间区间及证明材料。",
        "checkpoints": [
            "业绩数量是否达到要求",
            "单个业绩合同金额是否达到门槛",
            "业绩时间是否在招标文件限定区间内",
            "是否提供合同关键页或验收证明作为佐证",
        ],
    },
    {
        "id": "qual-credit", "name": "信用与禁止投标情形",
        "category": "qualification", "severity": "critical", "need_legal_basis": True,
        "description": "失信被执行人、重大税收违法、政府采购严重违法失信等禁止性情形。",
        "checkpoints": [
            "是否提供信用中国等平台查询记录",
            "是否被列入失信被执行人或重大税收违法名单",
            "是否提供无重大违法记录声明",
            "是否存在法律法规规定的禁止参加投标情形",
        ],
        # 注意：本规则【不要】配 structured 的 forbid_keywords / require_elements。
        # 二者都是「字面子串命中即锁定 fail」，而这类禁止性/资格性表述在合规文件中
        # 恰恰以否定式出现（如「投标人不得被列入失信被执行人名单」「不存在不良行为记录」），
        # 字面命中会 100% 误判为不合规，且确定性结论 LLM 无法推翻（confidence=1.0）。
        # 同样的道理适用于「无重大违法记录声明」等要素——「无行贿犯罪记录承诺函」等等效
        # 表述拿不到字面匹配就会被判缺失。此类规则交给 LLM 做语义判断，不结构化。
    },
    {
        "id": "qual-consortium", "name": "联合体投标合规性",
        "category": "qualification", "severity": "major", "need_legal_basis": True,
        "description": "联合体协议、牵头人授权、成员资质与禁止重复投标。",
        "checkpoints": [
            "招标文件是否允许联合体投标",
            "是否提供联合体协议并明确牵头人与分工",
            "联合体成员是否另行单独投标或参加其他联合体",
        ],
    },
    # ---- 商务偏离 ----
    {
        "id": "biz-bidbond", "name": "投标保证金",
        "category": "commercial", "severity": "critical", "need_legal_basis": True,
        "description": "投标保证金的金额、形式、递交时间与有效期。",
        "checkpoints": [
            "金额是否符合招标文件要求且不超过项目估算价 2%、最高 80 万元",
            "形式是否符合要求（转账、银行保函、保兑支票等）",
            "保证金有效期是否与投标有效期一致",
            "是否从投标人基本账户转出",
        ],
    },
    {
        "id": "biz-price", "name": "报价完整性与算术准确性",
        "category": "commercial", "severity": "critical", "need_legal_basis": False,
        "description": "报价表填报完整、单价与总价一致、大小写金额一致、不超最高限价。",
        "checkpoints": [
            "开标一览表与明细报价表金额是否一致",
            "单价乘数量之和是否等于合计金额（算术校核）",
            "金额大写与小写是否一致",
            "是否超过最高投标限价或低于成本价",
            "是否存在选择性报价或缺项漏项",
            "税率与含税口径是否符合要求",
        ],
        # 同 qual-credit：本规则【不要】配 structured。
        # 「选择性报价」「缺项漏项」「低于成本价」在合规文件中常以「不存在选择性报价」
        # 这类否定式声明出现，字面命中即 fail 属纯误判；「税率」等要素也并非所有报价表
        # 都会出现，require_elements 缺失即 fail 同样会误杀。算术校核本身需要模型读表计算，
        # 结构化条件表达不了，故整条规则回落 LLM。
    },
    {
        "id": "biz-validity", "name": "投标有效期与工期承诺",
        "category": "commercial", "severity": "major", "need_legal_basis": False,
        "description": "投标有效期、交付工期、质保期是否满足招标要求。",
        "checkpoints": [
            "投标有效期是否不短于招标文件要求",
            "交付/实施工期是否满足要求",
            "质保期与售后服务承诺是否达标",
        ],
    },
    {
        "id": "biz-payment", "name": "付款条件与合同条款响应",
        "category": "commercial", "severity": "major", "need_legal_basis": False,
        "description": "付款方式、违约责任等实质性条款是否接受。",
        "checkpoints": [
            "付款条件是否与招标文件一致，有无实质性偏离",
            "是否对合同主要条款提出保留或修改",
            "违约责任与验收条款是否接受",
        ],
    },
    # ---- 技术响应 ----
    {
        "id": "tech-response", "name": "技术参数响应与偏离",
        "category": "technical", "severity": "critical", "need_legal_basis": False,
        "description": "逐项核对技术规格响应，识别负偏离与带星号条款。",
        "checkpoints": [
            "是否提供技术参数偏离表并逐项响应",
            "带★或标注为实质性要求的条款是否全部满足",
            "是否存在未响应或负偏离的关键参数",
            "响应描述是否有产品资料等佐证",
        ],
    },
    {
        "id": "tech-plan", "name": "实施方案与服务承诺",
        "category": "technical", "severity": "major", "need_legal_basis": False,
        "description": "实施方案、进度计划、质量保障与培训运维方案的完整性。",
        "checkpoints": [
            "是否包含招标文件要求的全部方案章节",
            "进度计划是否与承诺工期匹配",
            "是否提供质量保障、风险应对与培训运维方案",
        ],
    },
    # ---- 格式与签章 ----
    {
        "id": "fmt-seal", "name": "签字盖章与法人授权",
        "category": "format", "severity": "critical", "need_legal_basis": False,
        "description": "投标函等关键文件的签字、盖章与授权委托书。",
        "checkpoints": [
            "投标函是否由法定代表人或授权代表签署并盖单位公章",
            "非法定代表人签署时是否提供授权委托书",
            "要求逐页盖章或骑缝章的部分是否满足",
            "公章名称与投标人名称是否一致",
        ],
    },
    {
        "id": "fmt-template", "name": "格式规范与编制要求",
        "category": "format", "severity": "minor", "need_legal_basis": False,
        "description": "按招标文件规定格式编制，目录页码齐全，装订与份数符合要求。",
        "checkpoints": [
            "是否使用招标文件提供的格式模板",
            "目录、页码、章节编号是否完整对应",
            "正副本份数、装订与密封要求是否满足",
            "是否按要求区分商务标与技术标（暗标要求是否遵守）",
        ],
    },
    {
        "id": "fmt-completeness", "name": "必备文件清单完整性",
        "category": "format", "severity": "critical", "need_legal_basis": False,
        "description": "对照招标文件投标文件组成清单，逐项核查是否缺件。",
        "checkpoints": [
            "招标文件列明的投标文件组成部分是否齐全",
            "各类声明、承诺函、附件是否遗漏",
            "是否存在应填未填的空白表格",
        ],
    },
    # ---- 一致性 ----
    {
        "id": "cons-cross", "name": "跨文件信息一致性",
        "category": "consistency", "severity": "major", "need_legal_basis": False,
        "description": "同一信息在正文、附件、报价表、证明材料间是否矛盾。",
        "checkpoints": [
            "投标人名称、统一社会信用代码在各文件中是否一致",
            "项目名称、标段编号是否一致",
            "报价金额在各处出现时是否一致",
            "项目经理及团队人员姓名、证书编号是否一致",
            "工期、质保期、投标有效期在各处表述是否一致",
            "日期逻辑是否合理（签署日期不晚于开标日期，证书未过期）",
        ],
        # 一致性核心要素：声明本规则要跨文件比对的要素。可在规则编辑界面增删，
        # 未声明时引擎会按规则文本回查要素库做同义词推断。
        "structured": {
            "consistency_elements": [
                {"name": "投标人名称", "synonyms": ["投标人名称", "供应商名称", "投标单位"], "note": "须与营业执照、公章、投标函完全一致"},
                {"name": "统一社会信用代码", "synonyms": ["统一社会信用代码", "信用代码"], "note": ""},
                {"name": "项目名称", "synonyms": ["项目名称", "工程名称", "标的名称"], "note": ""},
                {"name": "项目编号", "synonyms": ["项目编号", "标段编号", "招标编号"], "note": ""},
                {"name": "报价金额", "synonyms": ["报价金额", "投标总价", "投标报价"], "note": "大小写金额须一致，且不得超过最高限价"},
                {"name": "项目经理", "synonyms": ["项目经理", "项目负责人", "建造师"], "note": ""},
                {"name": "证书编号", "synonyms": ["证书编号", "资质证书编号", "注册编号"], "note": "证书须在有效期内"},
                {"name": "工期", "synonyms": ["工期", "交货期", "交付期"], "note": ""},
                {"name": "质保期", "synonyms": ["质保期", "保修期"], "note": ""},
                {"name": "投标有效期", "synonyms": ["投标有效期", "投标保函有效期"], "note": ""},
                {"name": "签署日期", "synonyms": ["签署日期", "签章日期", "落款日期"], "note": "签署日期不得晚于开标日期"},
            ],
        },
    },
    {
        "id": "cons-tender-match", "name": "与招标文件要求的对应性",
        "category": "consistency", "severity": "critical", "need_legal_basis": False,
        "description": "以招标文件为基准，核查投标文件的逐项响应关系。",
        "checkpoints": [
            "招标文件的实质性要求是否被逐项响应",
            "投标文件引用的招标条款编号是否准确",
            "是否存在答非所问或套用其他项目内容（他项目名称残留）",
        ],
        "structured": {
            "consistency_elements": [
                {"name": "项目名称", "synonyms": ["项目名称", "工程名称"], "note": "警惕他项目名称残留"},
                {"name": "项目编号", "synonyms": ["项目编号", "标段编号"], "note": ""},
                {"name": "投标人名称", "synonyms": ["投标人名称", "供应商名称"], "note": ""},
                {"name": "报价金额", "synonyms": ["报价金额", "投标总价"], "note": ""},
            ],
        },
    },
]

# ===================== 招标文件合规规则 =====================
TENDER_RULES: list[dict[str, Any]] = [
    {
        "id": "tender-completeness", "name": "招标文件组成完整性",
        "category": "tender_quality", "severity": "critical", "need_legal_basis": True,
        "description": "核查招标文件是否包含法定必备内容（招标公告、投标人须知、评标办法、合同条款等）。",
        "checkpoints": [
            "是否包含招标公告或投标邀请书",
            "是否包含投标人须知及前附表",
            "是否包含评标办法与标准",
            "是否包含技术规格书或需求说明",
            "是否包含合同主要条款或合同草案",
            "是否包含投标文件格式与清单",
        ],
    },
    {
        "id": "tender-scope", "name": "招标范围与需求清晰性",
        "category": "tender_quality", "severity": "major", "need_legal_basis": False,
        "description": "招标标的范围、数量、技术需求、服务内容描述应清晰完整，无歧义。",
        "checkpoints": [
            "招标标的名称、数量、规格是否明确",
            "技术需求描述是否清晰无歧义",
            "服务内容、交付物、验收标准是否完整",
            "是否存在前后矛盾或模糊表述",
        ],
    },
    {
        "id": "tender-qualification", "name": "资格条件合法性",
        "category": "tender_quality", "severity": "critical", "need_legal_basis": True,
        "description": "资格条件不得违法限制或排斥潜在投标人，不得以不合理条件限制竞争。",
        "checkpoints": [
            "是否设置与招标项目实际需要无关的资格条件",
            "是否对不同投标人实行差别或歧视待遇",
            "是否限定特定行政区域或特定行业的业绩",
            "注册资本、资产规模等要求是否过高且无合理依据",
        ],
    },
    {
        "id": "tender-tech-spec", "name": "技术规格公平性",
        "category": "tender_quality", "severity": "critical", "need_legal_basis": True,
        "description": "技术规格不得指向特定品牌、专利或供应商，不得设置排他性条款。",
        "checkpoints": [
            "是否指定特定品牌、商标、专利或供应商",
            "技术参数组合是否明显指向某一特定产品",
            "是否要求提供特定厂商的授权或认证",
            "是否允许同等产品或明确等效条款",
        ],
    },
    {
        "id": "tender-evaluation", "name": "评标办法量化与公平性",
        "category": "tender_quality", "severity": "major", "need_legal_basis": True,
        "description": "评标标准应量化、明确，评分规则公平合理。",
        "checkpoints": [
            "评标方法是否明确（综合评分法、最低评标价法等）",
            "评分因素和权重是否明确且合理",
            "主观评分项是否有明确的评分档次和标准",
            "是否存在倾向性或歧视性评分设置",
        ],
    },
    {
        "id": "tender-bidbond", "name": "投标保证金要求合规性",
        "category": "tender_quality", "severity": "critical", "need_legal_basis": True,
        "description": "投标保证金金额不得超过项目估算价的2%且最高不超过80万元。",
        "checkpoints": [
            "投标保证金金额是否符合法定上限",
            "保证金形式要求是否合法（不得限定仅接受现金）",
            "保证金退还时间与条件是否明确",
        ],
    },
    {
        "id": "tender-timeline", "name": "时间安排合理性",
        "category": "tender_quality", "severity": "major", "need_legal_basis": True,
        "description": "招标文件发售至投标截止时间应满足法定最短期限。",
        "checkpoints": [
            "公开招标时间是否不少于20日",
            "邀请招标时间是否满足要求",
            "开标时间、地点是否明确",
            "答疑、踏勘等关键节点时间是否合理",
        ],
    },
    {
        "id": "tender-price-limit", "name": "最高限价与成本合理性",
        "category": "tender_quality", "severity": "major", "need_legal_basis": False,
        "description": "设置最高限价时应有合理依据，不得明显低于成本或脱离市场行情。",
        "checkpoints": [
            "是否设置最高投标限价",
            "最高限价是否明显偏离市场价格",
            "是否公开限价编制依据",
        ],
    },
    {
        "id": "tender-payment", "name": "付款条件合理性",
        "category": "tender_quality", "severity": "major", "need_legal_basis": False,
        "description": "付款条件应合理，不得无合理依据拖延付款或设置过高门槛。",
        "checkpoints": [
            "付款比例、节点是否合理",
            "是否存在无合理理由延期付款的条款",
            "质保金比例与退还期限是否符合规定",
        ],
    },
    {
        "id": "tender-contract-terms", "name": "合同条款公平性",
        "category": "tender_quality", "severity": "major", "need_legal_basis": False,
        "description": "合同主要条款应公平合理，不得单方面加重供应商责任或免除采购人义务。",
        "checkpoints": [
            "违约责任是否对等",
            "风险分担是否合理",
            "是否存在单方面解除权或变更权条款",
        ],
    },
    {
        "id": "tender-clarification", "name": "答疑与澄清程序规范性",
        "category": "tender_quality", "severity": "minor", "need_legal_basis": True,
        "description": "答疑、澄清、修改招标文件应按规定程序进行并通知所有投标人。",
        "checkpoints": [
            "是否明确质疑答复时限与渠道",
            "澄清修改文件是否及时通知所有购买招标文件的单位",
            "实质性修改是否延长投标截止时间",
        ],
    },
    {
        "id": "tender-consistency", "name": "招标文件内部一致性",
        "category": "consistency", "severity": "major", "need_legal_basis": False,
        "description": "招标文件各章节间、表格与正文间应无矛盾或冲突。",
        "checkpoints": [
            "前附表与正文条款是否一致",
            "技术规格与评分标准是否对应",
            "投标文件格式要求是否前后一致",
            "同一信息在多处出现时是否一致",
        ],
        "structured": {
            "consistency_elements": [
                {"name": "项目名称", "synonyms": ["项目名称", "工程名称", "标的名称"], "note": ""},
                {"name": "项目编号", "synonyms": ["项目编号", "标段编号", "招标编号"], "note": ""},
                {"name": "招标人名称", "synonyms": ["招标人名称", "采购人名称", "发包人"], "note": ""},
                {"name": "报价金额", "synonyms": ["最高限价", "招标控制价", "项目预算", "预算金额"], "note": "前附表与正文的控制价须一致"},
                {"name": "投标保证金", "synonyms": ["投标保证金", "保证金金额"], "note": "金额与比例须与项目预算/最高限价匹配"},
                {"name": "开标时间", "synonyms": ["开标时间", "开标日期"], "note": ""},
                {"name": "投标截止时间", "synonyms": ["投标截止时间", "截标时间"], "note": "须晚于开标准备期且满足法定最短期限"},
                {"name": "工期", "synonyms": ["工期", "交货期", "交付期", "竣工日期"], "note": ""},
                {"name": "质保期", "synonyms": ["质保期", "保修期"], "note": ""},
            ],
        },
    },
]

# ===================== 通用审核规则 =====================
# 通用质量规则：适用于任意文档（合同/方案/报告/制度/公文/技术文档等），不含招投标业务口径。
# 每条 description 末尾统一给出【判定】以约束 pass/fail 边界，降低模型随机波动与误报：
#   - fail = 确凿的、可逐字取证的问题；warn = 疑似需人工复核；
#   - pass = 未发现该类问题；unknown = 原文不足以判断。
GENERAL_RULES: list[dict[str, Any]] = [
    {
        "id": "gen-typo", "name": "错别字与标点符号规范",
        "category": "general_quality", "severity": "minor", "need_legal_basis": False,
        "description": (
            "逐字逐句扫描文档，核查错别字（形近字/音近字替换、多字漏字、字序颠倒）"
            "与标点符号规范。错别字指实际用词错误（如形近字被误写为另一字），须与标点/格式问题区分。"
            "【判定】仅当能逐字指出「错字→正字」时判 fail 并在 typo 字段登记；"
            "OCR扫描件的词内异常空格（如'总公 司''投 标人'）不算错别字，归为 warn 或 pass；"
            "仅标点/空格问题判 warn；未发现字词错误判 pass。专有名词（公司/人名/产品名）不同写法"
            "属不同主体，不得判为错别字。"
        ),
        "checkpoints": [
            "逐句扫描，重点识别形近字、音近字被错误替换（如「帐号/账号」「以/已」「的/地/得」）",
            "识别多字、漏字或字序颠倒导致的表意错误",
            "标点符号使用是否规范（中英文符号混用、标点缺失、多余空格等）→ 归为 warn，不计入错别字",
            "OCR扫描件的词内空格噪声（如'总公 司'）不算错别字，不得填入 typo 字段",
            "专业术语拼写/用词是否正确",
            "专有名词不同写法属不同主体、不得判错别字；未识别到错别字时判 pass，不要把标点噪声当作错别字",
        ],
    },
    {
        "id": "gen-semantics", "name": "语义通顺性与逻辑一致性",
        "category": "general_quality", "severity": "minor", "need_legal_basis": False,
        "description": (
            "检查语句是否通顺、表述是否清晰、逻辑是否自洽。"
            "【判定】发现语句不通/表意矛盾并能摘录原句才判 warn（表述类问题一般不判 fail，"
            "除非直接导致条款无法执行）；行文通顺无矛盾判 pass。"
        ),
        "checkpoints": [
            "是否存在语句不通、表意不明（须摘录原句）",
            "是否存在逻辑矛盾或前后冲突（须并列摘录冲突的两处）",
            "是否存在主谓不一致、成分残缺",
            "无法确定是否矛盾（如需外部背景）时判 unknown，不臆测",
        ],
    },
    {
        "id": "gen-terminology", "name": "专业术语与表述规范",
        "category": "general_quality", "severity": "minor", "need_legal_basis": False,
        "description": (
            "专业术语使用规范统一，同一概念表述前后一致，缩写首次出现应注明全称。"
            "【判定】同一概念出现两种及以上混用写法、且能并列摘录时判 warn；统一规范判 pass。"
        ),
        "checkpoints": [
            "专业术语使用是否规范",
            "同一概念的表述是否统一（须并列摘录两种不同写法及各自出处）",
            "缩写是否在首次出现时注明全称",
        ],
    },
    {
        "id": "gen-format-number", "name": "数字、日期、金额格式统一性",
        "category": "general_quality", "severity": "minor", "need_legal_basis": False,
        "description": (
            "数字、日期、金额的表示方式应统一规范。"
            "【判定】同一类量值出现不一致格式且能并列摘录时判 warn；金额大小写不一致或"
            "同一金额多处取值不同属实质问题，判 fail 并写出算式。格式统一判 pass。"
        ),
        "checkpoints": [
            "数字使用是否统一（阿拉伯数字与中文数字混用）",
            "日期格式是否统一（YYYY-MM-DD 或其他格式）",
            "金额单位是否统一（元、万元、人民币）；同一金额大小写是否一致（须核对并写出换算）",
            "百分比、小数点位数是否一致",
        ],
    },
    {
        "id": "gen-table", "name": "表格完整性与格式规范",
        "category": "general_quality", "severity": "minor", "need_legal_basis": False,
        "description": (
            "表格结构完整、数据齐全、格式统一。"
            "【判定】表格缺表头/表名或存在应填未填的空缺项且能定位时判 warn；结构完整判 pass。"
            "无法从文本还原表格结构（如复杂合并单元格）时判 unknown，不臆测缺项。"
        ),
        "checkpoints": [
            "表格是否有表头、表号、表名",
            "表格中是否存在应填未填的空白单元格或缺项（须指出具体行/列）",
            "表格格式是否统一（对齐方式、边框样式）",
        ],
    },
    {
        "id": "gen-toc", "name": "文档结构完整性（目录、页码、编号）",
        "category": "general_quality", "severity": "minor", "need_legal_basis": False,
        "description": (
            "目录、页码、章节编号应完整准确、连贯无跳号。"
            "【判定】章节编号跳号/重号、目录与正文标题不符且能指出具体处时判 warn；连贯一致判 pass。"
        ),
        "checkpoints": [
            "目录标题与正文章节标题是否对应",
            "章节编号是否连贯无遗漏/无重复（须指出跳号或重号的具体编号）",
            "页码是否连续且与目录一致（若文本未保留页码信息则该项判 unknown）",
        ],
    },
    {
        "id": "gen-reference", "name": "引用与出处准确性",
        "category": "general_quality", "severity": "minor", "need_legal_basis": False,
        "description": (
            "引用法规、标准、文献时应准确标注出处，正文交叉引用（如「见第X章」「如表N」）应指向存在的对象。"
            "【判定】交叉引用指向不存在的章节/表号，或引用编号明显残缺时判 warn；"
            "引用内容的真伪需外部核实的，判 unknown，不得凭记忆断言错误。"
        ),
        "checkpoints": [
            "正文交叉引用（见第X章/如表N/附件M）指向的对象在文档中是否存在",
            "引用的法律法规名称、条款编号、技术标准编号与版本格式是否完整（真伪需外部核实的判 unknown）",
            "图表、数据来源是否标注",
        ],
    },
    {
        "id": "gen-attachment", "name": "附件完整性与引用一致性",
        "category": "general_quality", "severity": "minor", "need_legal_basis": False,
        "description": (
            "正文引用的附件应齐全，附件编号与正文一致。"
            "【判定】正文提及的附件编号在附件清单/内容中找不到对应项时判 warn；一致齐全判 pass。"
            "仅上传单一文件、无从判断附件是否随附时判 unknown。"
        ),
        "checkpoints": [
            "正文提及的附件编号/名称是否都能在文档或附件清单中找到对应项",
            "附件编号、名称与正文引用是否一致（须并列摘录不一致处）",
            "附件内容是否与正文描述匹配",
        ],
    },
    {
        "id": "gen-completeness", "name": "关键要素完整性（标题、日期、签署/落款）",
        "category": "general_quality", "severity": "major", "need_legal_basis": False,
        "description": (
            "通用文档应具备基本要素：标题、成文/签署日期、责任主体（落款单位/人）及必要的签章位。"
            "【判定】缺少标题或落款主体等关键要素且能确认缺失时判 fail；缺日期/签章位判 warn；"
            "要素齐全判 pass。是否需要签章取决于文档性质，无法确定时判 unknown，不强加要求。"
        ),
        "checkpoints": [
            "是否有明确的文档标题",
            "是否有成文日期/签署日期",
            "是否有责任主体（落款单位或个人）",
            "正式文件是否具备签署/盖章位（文档性质不要求签章的判 pass 或 unknown，不误报）",
        ],
    },
    {
        "id": "gen-date-logic", "name": "日期与期限逻辑合理性",
        "category": "general_quality", "severity": "major", "need_legal_basis": False,
        "description": (
            "文档中的日期先后关系与期限应符合逻辑。"
            "【判定】出现确凿的日期逻辑错误（如结束早于开始、签署晚于生效、有效期已过）且能取证、写出比较时判 fail；"
            "日期表述含糊需确认判 warn；未涉及日期或不足以判断判 pass/unknown。不得引入「当前系统时间」作为判据。"
        ),
        "checkpoints": [
            "起止日期是否满足开始≤结束（须写出两日期与比较）",
            "签署/生效/截止等日期的先后关系是否符合逻辑",
            "有效期、期限描述是否与相关日期自洽（不以运行时刻为判据，除非文档给出基准日）",
        ],
    },
]

BUILTIN_RULESET_ID = "builtin-default"


def _builtin_ruleset() -> dict[str, Any]:
    """内置规则集（投标+通用），保持向后兼容。"""
    return {
        "id": BUILTIN_RULESET_ID,
        "name": "标准文档合规审核规则集",
        "description": f"文档合规审核（{len(BID_RULES)}条）+ 通用审核（{len(GENERAL_RULES)}条）",
        "builtin": True,
        "mode": "bid",
        "rules": [dict(r, enabled=True, builtin=True) for r in (BID_RULES + GENERAL_RULES)],
    }


def get_rules_by_mode(mode: str) -> list[dict[str, Any]]:
    """根据审核模式返回适用规则。

    Args:
        mode: "bid" | "tender" | "general"

    Returns:
        投标模式 = BID_RULES + GENERAL_RULES
        招标模式 = TENDER_RULES + GENERAL_RULES
        通用模式 = GENERAL_RULES
    """
    if mode == "bid":
        return [dict(r, enabled=True) for r in (BID_RULES + GENERAL_RULES)]
    elif mode == "tender":
        return [dict(r, enabled=True) for r in (TENDER_RULES + GENERAL_RULES)]
    elif mode == "general":
        return [dict(r, enabled=True) for r in GENERAL_RULES]
    else:
        # 未知模式降级为通用
        return [dict(r, enabled=True) for r in GENERAL_RULES]


def _load_custom() -> list[dict[str, Any]]:
    data = storage.read_collection("custom_rulesets")
    if not isinstance(data, list):
        return []
    if not isinstance(data, list):
        return []
    # 迁移：存量自定义规则集归属为「系统共享」（owner=system, is_shared=True），
    # 使既有全局规则对全体用户可见；用户自建规则集则带自身 owner_id。
    changed = False
    for rs in data:
        if not isinstance(rs, dict):
            continue
        if "owner_id" not in rs:
            rs["owner_id"] = "system"
            rs["is_shared"] = True
            changed = True
    if changed:
        _save_custom(data)
    return data


def _accessible(rs: dict[str, Any], user_id: str | None) -> bool:
    """作用域判定：管理员（user_id=None）可见全部；普通用户仅可见自己拥有或共享的规则集。"""
    if user_id is None:
        return True
    return rs.get("owner_id") == user_id or bool(rs.get("is_shared"))


def _save_custom(items: list[dict[str, Any]]) -> None:
    try:
        storage.write_collection("custom_rulesets", items)
    except Exception:  # noqa: BLE001
        pass


def list_rulesets(user_id: str | None = None) -> list[dict[str, Any]]:
    """返回规则集列表。user_id=None（管理员）返回全部；否则仅返回该用户拥有或共享的。"""
    base = [_builtin_ruleset()]
    custom = [rs for rs in _load_custom() if _accessible(rs, user_id)]
    return base + custom


def get_ruleset(ruleset_id: str | None, user_id: str | None = None) -> dict[str, Any] | None:
    if not ruleset_id or ruleset_id == BUILTIN_RULESET_ID:
        return _builtin_ruleset()
    for rs in _load_custom():
        if rs["id"] == ruleset_id:
            return rs if _accessible(rs, user_id) else None
    return None


def save_ruleset(
    payload: dict[str, Any],
    user_id: str | None = None,
    is_shared: bool | None = None,
) -> dict[str, Any]:
    """新建或更新自定义规则集；内置规则集会被复制为新集合。

    user_id=None（管理员/系统）创建的规则集归属「系统共享」；普通用户创建归属自己
    （owner_id=user_id, is_shared=False）。更新已有规则集时保留其原有归属。
    """
    with _lock:
        items = _load_custom()
        rid = payload.get("id")
        if not rid or rid == BUILTIN_RULESET_ID:
            rid = f"rs-{uuid.uuid4().hex[:8]}"
        existing = next((rs for rs in items if rs["id"] == rid), None)
        if existing:
            owner_id = existing.get("owner_id", "system")
            shared = existing.get("is_shared", True)
        else:
            owner_id = "system" if user_id is None else user_id
            shared = True if user_id is None else bool(is_shared)
        record = {
            "id": rid,
            "name": payload.get("name") or "未命名规则集",
            "description": payload.get("description", ""),
            "builtin": False,
            "owner_id": owner_id,
            "is_shared": shared,
            "rules": [_normalize_rule(r) for r in payload.get("rules", [])],
        }
        for i, rs in enumerate(items):
            if rs["id"] == rid:
                items[i] = record
                break
        else:
            items.append(record)
        _save_custom(items)
        return record


def delete_ruleset(ruleset_id: str, user_id: str | None = None) -> bool:
    if ruleset_id == BUILTIN_RULESET_ID:
        return False
    with _lock:
        items = _load_custom()
        target = next((rs for rs in items if rs["id"] == ruleset_id), None)
        if not target:
            return False
        # 仅管理员或属主可删除
        if user_id is not None and target.get("owner_id") != user_id:
            return False
        remaining = [rs for rs in items if rs["id"] != ruleset_id]
        if len(remaining) == len(items):
            return False
        _save_custom(remaining)
        return True


def _normalize_rule(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id") or f"rule-{uuid.uuid4().hex[:6]}",
        "name": raw.get("name") or "未命名规则",
        "category": raw.get("category") or "qualification",
        "severity": raw.get("severity") or "major",
        "description": raw.get("description", ""),
        "checkpoints": [c for c in (raw.get("checkpoints") or []) if str(c).strip()],
        "need_legal_basis": bool(raw.get("need_legal_basis")),
        "enabled": raw.get("enabled", True),
        "builtin": bool(raw.get("builtin")),
        # 关联文档类型：仅对匹配文件类型的文件执行本规则审核；缺省/空=适用于全部文件
        "doc_types": [str(x).strip() for x in (raw.get("doc_types") or []) if str(x).strip()],
        # 关联章节：仅对命中章节的正文执行本规则审核；缺省/空=不裁剪（沿用全量审核）。
        # 章节以文件类型为维度，审核时按文档自身 file_type 与 section_ids 取交集。
        "section_ids": [
            str(x).strip() for x in (raw.get("section_ids") or []) if str(x).strip()
        ],
        # 结构化可执行条件（需求 1.2）：可计算条件，由确定性规则引擎直接判定，
        # LLM 仅作辅助证据摘要，不决定 fail/pass。
        "structured": _normalize_structured(raw.get("structured")),
    }


def _normalize_consistency_elements(raw: Any) -> list[dict[str, Any]]:
    """归一化一致性要素声明，兼容三种写法：

    1. 对象列表：[{"name": "项目名称", "synonyms": [...], "note": "..."}]
    2. 名称列表：["项目名称", "项目编号"]（同义词与默认要求回查要素库补齐）
    3. 换行文本："项目名称\\n项目编号"

    统一输出 [{"name", "synonyms", "note"}]，同名要素合并（后者补齐前者的空字段）。
    """
    items: list[dict[str, Any]] = []
    if isinstance(raw, str):
        raw = [s for s in raw.replace("，", "\n").replace(",", "\n").split("\n")]
    if not isinstance(raw, (list, tuple)):
        return items
    for entry in raw:
        name = ""
        synonyms: list[str] = []
        note = ""
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            syn_raw = entry.get("synonyms")
            if isinstance(syn_raw, str):
                syn_raw = [s for s in syn_raw.replace("，", ",").split(",")]
            synonyms = [str(s).strip() for s in (syn_raw or []) if str(s).strip()]
            note = str(entry.get("note") or "").strip()
        elif isinstance(entry, str):
            name = entry.strip()
        if not name:
            continue
        # 未显式给同义词时，回查要素库补齐（含按同义词命中的规范名）
        lib = get_library_element(name)
        if lib:
            name = lib["name"]
            if not synonyms:
                synonyms = list(lib.get("synonyms") or [])
            if not note:
                note = str(lib.get("note") or "")
        if name not in synonyms:
            synonyms.insert(0, name)
        items.append({"name": name, "synonyms": synonyms, "note": note})
    return _merge_elements(items)


def _merge_elements(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 name 合并要素条目：同义词并集去重，note 取首个非空。"""
    merged: dict[str, dict[str, Any]] = {}
    for it in items:
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        cur = merged.get(name)
        if not cur:
            merged[name] = {
                "name": name,
                "synonyms": list(it.get("synonyms") or []),
                "note": str(it.get("note") or ""),
            }
            continue
        for s in it.get("synonyms") or []:
            if s not in cur["synonyms"]:
                cur["synonyms"].append(s)
        if not cur["note"] and it.get("note"):
            cur["note"] = str(it["note"])
    return list(merged.values())


def _opt_float(val: Any) -> float | None:
    """宽松转 float：None/空串/非法值一律返回 None，不抛异常、不静默当 0。

    用于「阈值类」可选字段——若沿用 float(x or 0) 的写法，会把「未配置」与
    「阈值就是 0」混为一谈，导致相对阈值（max_ratio）被 0 顶掉。
    """
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _normalize_structured(raw: Any) -> dict[str, Any] | None:
    """校验并归一化 structured 可执行条件字段。

    支持六类条件：
    - forbid_keywords: 黑名单关键词列表（命中即 fail）
    - require_elements: 必含要素列表（缺失即 fail）
    - regex_patterns: [{pattern, must_match: bool}]（正则匹配判定）
    - amount_thresholds: [{field, max}]（金额上限判定）
    - amount_pair_diff: [{a_field, b_field, max_abs_diff|max_ratio, ratio_base, fail_when}]
      （两金额差值比较；max_abs_diff 为绝对阈值「元」，max_ratio 为相对阈值比例，
      如 0.1 表示「差值 < 基准值的 10%」，ratio_base 取 "a"|"b" 指定分母）
    - consistency_elements: 一致性核查核心要素 [{name, synonyms, note}]
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    ce = _normalize_consistency_elements(raw.get("consistency_elements"))
    if ce:
        out["consistency_elements"] = ce
    fk = raw.get("forbid_keywords")
    if isinstance(fk, (list, tuple)):
        out["forbid_keywords"] = [str(x).strip() for x in fk if str(x).strip()]
    re_elems = raw.get("require_elements")
    if isinstance(re_elems, (list, tuple)):
        out["require_elements"] = [str(x).strip() for x in re_elems if str(x).strip()]
    pats = raw.get("regex_patterns")
    if isinstance(pats, (list, tuple)):
        norm_pats = []
        for p in pats:
            if isinstance(p, dict) and p.get("pattern"):
                norm_pats.append(
                    {
                        "pattern": str(p["pattern"]),
                        "must_match": bool(p.get("must_match", True)),
                    }
                )
        if norm_pats:
            out["regex_patterns"] = norm_pats
    amts = raw.get("amount_thresholds")
    if isinstance(amts, (list, tuple)):
        norm_amts = []
        for a in amts:
            if isinstance(a, dict) and a.get("field"):
                try:
                    norm_amts.append(
                        {"field": str(a["field"]), "max": float(a.get("max", 0))}
                    )
                except (TypeError, ValueError):
                    continue
        if norm_amts:
            out["amount_thresholds"] = norm_amts
    apd = raw.get("amount_pair_diff")
    if isinstance(apd, (list, tuple)):
        norm_apd = []
        for p in apd:
            if not isinstance(p, dict):
                continue
            a_fields = [str(x).strip() for x in (p.get("a_field") or p.get("a") or []) if str(x).strip()]
            b_fields = [str(x).strip() for x in (p.get("b_field") or p.get("b") or []) if str(x).strip()]
            if not a_fields or not b_fields:
                continue
            # 阈值二选一：max_abs_diff（绝对，元）或 max_ratio（相对，比例）。
            # 相对阈值用于「差值小于暂估价的 10%」这类随基准值浮动的规则 —— 这类规则
            # 无法用固定金额表达，此前只能回落 LLM，导致同一份文件反复出现
            # 「文本算对、结构化字段填错」的自相矛盾结论（降级为待人工复核）。
            thr = _opt_float(p.get("max_abs_diff"))
            ratio = _opt_float(p.get("max_ratio"))
            if thr is None and ratio is None:
                continue
            base = str(p.get("ratio_base", "a") or "a").strip().lower()
            if base not in ("a", "b"):
                base = "a"
            entry: dict[str, Any] = {
                "a_field": a_fields,
                "b_field": b_fields,
                "fail_when": str(p.get("fail_when", "le")).lower(),
            }
            if thr is not None:
                entry["max_abs_diff"] = thr
            if ratio is not None:
                entry["max_ratio"] = ratio
                entry["ratio_base"] = base
            norm_apd.append(entry)
        if norm_apd:
            out["amount_pair_diff"] = norm_apd
    return out if out else None


def select_rules(
    ruleset_id: str | None,
    rule_ids: list[str] | None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    rs = get_ruleset(ruleset_id, user_id)
    rules = [r for r in (rs.get("rules", []) if rs else []) if r.get("enabled", True)]
    if rule_ids:
        wanted = set(rule_ids)
        rules = [r for r in rules if r["id"] in wanted]
    return rules


def get_rules_by_ids(
    rule_ids: list[str] | None, user_id: str | None = None
) -> list[dict[str, Any]]:
    """跨内置模式与自定义规则集，按 id 解析规则对象（用于规则组展开）。

    返回顺序与入参 rule_ids 保持一致；找不到的 id 静默跳过。
    user_id 给定时仅从「该用户拥有或共享」的规则集中解析。
    """
    if not rule_ids:
        return []
    pool: dict[str, dict[str, Any]] = {}
    # 内置三模式规则（共享，任何用户可见）
    for mode in ("bid", "tender", "general"):
        for r in get_rules_by_mode(mode):
            pool[r["id"]] = r
    # 作用域内的自定义规则集
    for rs in list_rulesets(user_id):
        if rs.get("builtin"):
            continue
        for r in rs.get("rules", []):
            rid = r.get("id")
            if rid:
                pool[rid] = r
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rid in rule_ids:
        if rid in pool and rid not in seen:
            seen.add(rid)
            out.append(pool[rid])
    return out
