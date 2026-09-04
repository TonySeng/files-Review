# -*- coding: utf-8 -*-
"""审核规则批量导入：解析 CSV / XLSX / XLS，校验并归一化为规则对象，并生成导入模板。

导入模板字段结构（首行表头，支持中文或英文字段名）：
    name             规则名称        （必填）
    category         类别            （必填；支持 id 或中文标签，见 CATEGORY_MAP）
    severity         严重级别        （必填；支持 id 或中文标签，见 SEVERITY_MAP）
    description      规则说明/条件   （选填）
    checkpoints      审核要点        （必填；每行/每点一条，可用换行或「；」「;」分隔；多列“审核要点N”也会合并）
    need_legal_basis 需法规依据      （选填；是/否/true/false/1/0，默认否）
    enabled          是否启用        （选填；是/否/true/false/1/0，默认是）

校验与去噪：
- 名称空、以「#」「示例」「example」开头的行视为示例/注释行，跳过（不计入错误）。
- 类别/严重级别无法识别时给出明确错误并跳过该行。
- 审核要点缺失视为错误并跳过该行。
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

# ===================== 字段映射 =====================
CATEGORY_MAP: dict[str, str] = {
    "qualification": "qualification",
    "资格性": "qualification",
    "资格": "qualification",
    "commercial": "commercial",
    "商务": "commercial",
    "technical": "technical",
    "技术": "technical",
    "format": "format",
    "格式": "format",
    "consistency": "consistency",
    "一致性": "consistency",
    "legal": "legal",
    "法规": "legal",
    "tender_quality": "tender_quality",
    "招标文件质量": "tender_quality",
    "招标质量": "tender_quality",
    "general_quality": "general_quality",
    "通用质量": "general_quality",
    "通用": "general_quality",
    # 文字校对 / 错别字核查类规则（投标文件也常需）
    "general_text": "general_text",
    "通用文字": "general_text",
    "proper_noun": "proper_noun",
    "专有名词": "proper_noun",
    "punct_num": "punct_num",
    "标点数字": "punct_num",
    "word_usage": "word_usage",
    "语句用词": "word_usage",
    "format_spec": "format_spec",
    "格式规范": "format_spec",
}

SEVERITY_MAP: dict[str, str] = {
    "critical": "critical",
    "否决项": "critical",
    "严重": "critical",
    "major": "major",
    "重要": "major",
    "minor": "minor",
    "一般": "minor",
    "info": "info",
    "提示": "info",
}

# id -> 中文显示标签（导出 CSV/XLSX 时用中文，便于在 Excel 中直接编辑）
CATEGORY_LABEL_INV: dict[str, str] = {
    v: k for k, v in (
        ("资格性", "qualification"),
        ("商务", "commercial"),
        ("技术", "technical"),
        ("格式", "format"),
        ("一致性", "consistency"),
        ("法规", "legal"),
        ("招标文件质量", "tender_quality"),
        ("通用质量", "general_quality"),
        ("通用文字", "general_text"),
        ("专有名词", "proper_noun"),
        ("标点数字", "punct_num"),
        ("语句用词", "word_usage"),
        ("格式规范", "format_spec"),
    )
}
SEVERITY_LABEL_INV: dict[str, str] = {
    "critical": "否决项",
    "major": "重要",
    "minor": "一般",
    "info": "提示",
}

# 字段中文/英文别名 -> 标准内部键
FIELD_ALIASES: dict[str, str] = {
    "name": "name",
    "规则名称": "name",
    "category": "category",
    "类别": "category",
    "severity": "severity",
    "严重级别": "severity",
    "级别": "severity",
    "description": "description",
    "规则说明": "description",
    "规则说明/条件": "description",
    "说明": "description",
    "条件": "description",
    "checkpoints": "checkpoints",
    "审核要点": "checkpoints",
    "要点": "checkpoints",
    "need_legal_basis": "need_legal_basis",
    "需法规依据": "need_legal_basis",
    "法规依据": "need_legal_basis",
    "enabled": "enabled",
    "是否启用": "enabled",
    "启用": "enabled",
    "ruleset_name": "ruleset_name",
    "规则集名称": "ruleset_name",
}

TEMPLATE_FIELDS: list[tuple[str, str, str, str]] = [
    ("name", "规则名称", "是", "规则的中文名称，将展示在规则清单中"),
    ("category", "类别", "是", "资格性/商务/技术/格式/一致性/法规/招标文件质量/通用质量（或对应英文 id）"),
    ("severity", "严重级别", "是", "否决项/重要/一般/提示（或 critical/major/minor/info）"),
    ("description", "规则说明/条件", "否", "规则的审核目的与判定条件说明"),
    ("checkpoints", "审核要点", "是", "逐条核查点，每行/每点一条；可用换行或「；」分隔；可拆分多列「审核要点1/2…」"),
    ("need_legal_basis", "需法规依据", "否", "是/否（或 true/false/1/0），默认否；为是时审核优先检索法规知识库"),
    ("enabled", "是否启用", "否", "是/否（或 true/false/1/0），默认是"),
]


def _clean_key(k: Any) -> str:
    return str(k).strip().lstrip("\ufeff").replace(" ", " ").strip()


def _decode_text(data: bytes) -> str:
    """文本类文件（CSV/TXT）解码：优先 UTF-8（含 BOM），失败回退 GBK/GB18030。

    中文版 Excel 导出的 CSV 默认以 ANSI(GBK) 编码保存，直接用 UTF-8 解码会
    得到乱码表头，导致字段别名匹配失败、整行被静默跳过。故需做编码容错。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _is_example_row(name: str) -> bool:
    s = name.strip().lower()
    if not s:
        return True
    if s.startswith("#"):
        return True
    if s.startswith("示例") or s.startswith("（示例") or s.startswith("(示例"):
        return True
    if s.startswith("example") or s.startswith("sample"):
        return True
    return False


def _parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("是", "true", "1", "yes", "y", "启用", "需要", "需"):
        return True
    if s in ("否", "false", "0", "no", "n", "停用", "不需要", "无需"):
        return False
    return default


def _split_checkpoints(v: Any) -> list[str]:
    if v is None:
        return []
    # 多列「审核要点N」以 \n 拼接传入
    text = str(v)
    parts = re.split(r"[；;]", text)
    return [p.strip() for p in parts if p.strip()]


def _map_category(v: Any) -> str | None:
    if v is None:
        return None
    return CATEGORY_MAP.get(str(v).strip().lower())


def _map_severity(v: Any) -> str | None:
    if v is None:
        return None
    return SEVERITY_MAP.get(str(v).strip().lower())


def _normalize_record(row: dict[str, Any], line_no: int) -> tuple[dict[str, Any] | None, str | None]:
    """归一化一行为规则对象；返回 (rule, error)。示例行返回 (None, None)。"""
    name = (row.get("name") or "").strip()
    if _is_example_row(name):
        return None, None

    if not name:
        return None, f"第 {line_no} 行：缺少规则名称"

    category = _map_category(row.get("category"))
    if not category:
        return None, f"第 {line_no} 行「{name}」：类别无效（应为 {', '.join(sorted(set(CATEGORY_MAP.values())))} 或对应中文）"

    severity = _map_severity(row.get("severity")) or "major"

    checkpoints = _split_checkpoints(row.get("checkpoints"))
    if not checkpoints:
        return None, f"第 {line_no} 行「{name}」：缺少审核要点"

    rule = {
        "id": f"import-{abs(hash(name + category)) % 10**8:08d}",
        "name": name,
        "category": category,
        "severity": severity,
        "description": (row.get("description") or "").strip(),
        "checkpoints": checkpoints,
        "need_legal_basis": _parse_bool(row.get("need_legal_basis"), False),
        "enabled": _parse_bool(row.get("enabled"), True),
        "builtin": False,
        "ruleset_name": (row.get("ruleset_name") or "").strip(),
    }
    return rule, None


def _rows_to_dicts(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    norm_headers = [_clean_key(h) for h in headers]
    out: list[dict[str, Any]] = []
    for r in rows:
        d: dict[str, Any] = {}
        for i, h in enumerate(norm_headers):
            key = FIELD_ALIASES.get(h)
            if not key:
                continue
            val = r[i] if i < len(r) else ""
            if key == "checkpoints":
                # 合并所有“审核要点”相关列
                d.setdefault("checkpoints", "")
                extra = str(val or "")
                d["checkpoints"] = (d["checkpoints"] + "\n" + extra).strip() if d["checkpoints"] else extra
            else:
                if key not in d or not d[key]:
                    d[key] = val
        out.append(d)
    return out


def parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.reader(io.StringIO(text))
    lines = [r for r in reader if any(c.strip() for c in r)]
    if not lines:
        return []
    headers = [_clean_key(h) for h in lines[0]]
    rows = lines[1:]
    return _rows_to_dicts(headers, rows)


def parse_xlsx_bytes(data: bytes) -> list[dict[str, Any]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = None
    try:
        ws = wb["规则"]
    except KeyError:
        ws = wb.active
    matrix = [list(row) for row in ws.iter_rows(values_only=True)]
    matrix = [[("" if c is None else c) for c in row] for row in matrix if any(c != "" and c is not None for c in row)]
    if not matrix:
        return []
    headers = [_clean_key(h) for h in matrix[0]]
    rows = matrix[1:]
    return _rows_to_dicts(headers, rows)


def parse_xls_bytes(data: bytes) -> list[dict[str, Any]]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    sh = book.sheet_by_index(0)
    matrix: list[list[Any]] = []
    for r in range(sh.nrows):
        matrix.append([sh.cell_value(r, c) for c in range(sh.ncols)])
    matrix = [[("" if c is None else c) for c in row] for row in matrix if any(str(c).strip() for c in row)]
    if not matrix:
        return []
    headers = [_clean_key(h) for h in matrix[0]]
    rows = matrix[1:]
    return _rows_to_dicts(headers, rows)


def parse_rulesets_json(data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """解析导出的「规则集 JSON」（全量保真格式），返回 (规则集列表, 错误列表)。

    兼容三种输入形态：
      - {"schema": "...", "rulesets": [ {...}, ... ]}
      - [ {...规则集...}, ... ]            （规则集数组）
      - {...单个规则集...}                 （单个规则集对象）

    每个规则集至少需含 name 与非空 rules 数组；单条规则至少需含 name 与 checkpoints。
    返回的规则集对象保留原始 id / builtin / mode / structured 等字段，供 save_ruleset 直接落盘。
    """
    import json as _json

    try:
        obj = _json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        raise ValueError(f"JSON 解析失败：{exc}")

    if isinstance(obj, dict) and "rulesets" in obj:
        raw_sets = obj["rulesets"]
    elif isinstance(obj, list):
        raw_sets = obj
    elif isinstance(obj, dict):
        raw_sets = [obj]
    else:
        raise ValueError("JSON 顶层应为 规则集数组 或 含 rulesets 字段的对象")

    if not isinstance(raw_sets, list):
        raise ValueError("规则集数据应为数组")

    sets: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, rs in enumerate(raw_sets, start=1):
        if not isinstance(rs, dict):
            errors.append(f"第 {idx} 个规则集不是合法对象，已跳过")
            continue
        name = (rs.get("name") or "").strip()
        rules = rs.get("rules")
        if not name:
            errors.append(f"第 {idx} 个规则集缺少 name，已跳过")
            continue
        if not isinstance(rules, list) or not rules:
            errors.append(f"规则集「{name}」缺少 rules 数组，已跳过")
            continue
        # 校验每条规则，剔除无效的
        clean_rules: list[dict[str, Any]] = []
        for j, r in enumerate(rules, start=1):
            if not isinstance(r, dict):
                errors.append(f"规则集「{name}」第 {j} 条不是合法对象，已跳过")
                continue
            rname = (r.get("name") or "").strip()
            cps = r.get("checkpoints") or []
            if isinstance(cps, str):
                cps = [c for c in cps.replace("；", ";").split(";") if c.strip()]
            if not rname:
                errors.append(f"规则集「{name}」第 {j} 条缺少 name，已跳过")
                continue
            if not cps:
                errors.append(f"规则集「{name}」规则「{rname}」缺少审核要点，已跳过")
                continue
            clean_rules.append(r)
        if not clean_rules:
            errors.append(f"规则集「{name}」无有效规则，已跳过")
            continue
        sets.append(
            {
                "id": rs.get("id"),
                "name": name,
                "description": rs.get("description", ""),
                "builtin": bool(rs.get("builtin")),
                "mode": rs.get("mode"),
                "rules": clean_rules,
            }
        )
    return sets, errors


def parse_rules_file(filename: str, data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """解析上传文件，返回 (有效规则列表, 错误说明列表)。"""
    ext = Path(filename).suffix.lower()
    try:
        if ext in (".xlsx",):
            raw_rows = parse_xlsx_bytes(data)
        elif ext in (".xls",):
            raw_rows = parse_xls_bytes(data)
        else:  # csv / txt
            text = _decode_text(data)
            raw_rows = parse_csv(text)
    except Exception as exc:  # 解析失败（损坏/格式不符）
        raise ValueError(f"文件解析失败：{exc}")

    rules: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, row in enumerate(raw_rows, start=2):  # 第 1 行为表头
        rule, err = _normalize_record(row, idx)
        if err:
            errors.append(err)
        elif rule:
            rules.append(rule)
    return rules, errors


# ===================== 模板生成 =====================
_TEMPLATE_HEADER = ["规则名称", "类别", "严重级别", "规则说明/条件", "审核要点", "需法规依据", "是否启用"]
_TEMPLATE_EXAMPLE = [
    "示例：投标保证金金额上限",
    "商务",
    "重要",
    "核查投标保证金是否超过法定上限",
    "金额是否超过项目估算价的 2% 且最高不超过 80 万元；是否从投标人基本账户转出",
    "是",
    "是",
]


def build_template_csv() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_TEMPLATE_HEADER)
    w.writerow(_TEMPLATE_EXAMPLE)
    return ("\ufeff" + buf.getvalue()).encode("utf-8-sig")


def build_template_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "规则"
    ws.append(_TEMPLATE_HEADER)
    ws.append(_TEMPLATE_EXAMPLE)

    spec = wb.create_sheet("字段说明")
    spec.append(["字段", "说明", "必填", "可取值 / 规范"])
    for f_name, f_cn, required, desc in TEMPLATE_FIELDS:
        spec.append([f_cn, desc, required, ""])
    # 列宽
    for col in ("A", "B", "C", "D"):
        spec.column_dimensions[col].width = 26 if col != "D" else 60

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ===================== 规则集导出（CSV / XLSX）=====================
# 列结构与导入模板完全一致，因此导出的文件可被 POST /api/rulesets/import 原样回导。
# 额外增加「规则集名称」列，便于一次导出多个规则集后在回导时按名称分组建集。
_EXPORT_HEADER = ["规则集名称", "规则名称", "类别", "严重级别", "规则说明/条件", "审核要点", "需法规依据", "是否启用"]


def _rule_to_row(set_name: str, rule: dict[str, Any]) -> list[Any]:
    cps = rule.get("checkpoints") or []
    if isinstance(cps, str):
        cps = [c for c in cps.replace("；", ";").split(";") if c.strip()]
    return [
        set_name,
        rule.get("name", ""),
        CATEGORY_LABEL_INV.get(rule.get("category"), rule.get("category", "")),
        SEVERITY_LABEL_INV.get(rule.get("severity"), rule.get("severity", "")),
        rule.get("description", ""),
        "；".join(str(c) for c in cps),
        "是" if rule.get("need_legal_basis") else "否",
        "是" if rule.get("enabled", True) else "否",
    ]


def export_rules_csv(sets: list[dict[str, Any]]) -> bytes:
    """将规则集列表导出为 CSV（含 BOM，便于 Excel 直接打开编辑）。

    列顺序与 import 模板一致，且每行带「规则集名称」，可被 /import 回导。
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_EXPORT_HEADER)
    for rs in sets:
        name = (rs.get("name") or "").strip()
        for r in rs.get("rules", []) or []:
            if not isinstance(r, dict):
                continue
            w.writerow(_rule_to_row(name, r))
    # utf-8-sig 已含 BOM，供 Excel 正确识别中文编码；不要再手动叠加 \ufeff
    return buf.getvalue().encode("utf-8-sig")


def export_rules_xlsx(sets: list[dict[str, Any]]) -> bytes:
    """将规则集列表导出为 XLSX（「规则」数据表 + 「字段说明」辅助表）。

    与 import 模板列结构一致，可被 /import 回导。
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "规则"
    ws.append(_EXPORT_HEADER)
    for rs in sets:
        name = (rs.get("name") or "").strip()
        for r in rs.get("rules", []) or []:
            if not isinstance(r, dict):
                continue
            ws.append(_rule_to_row(name, r))

    # 字段说明辅助表，方便用户在 Excel 中对照编辑
    spec = wb.create_sheet("字段说明")
    spec.append(["字段", "说明", "必填", "可取值 / 规范"])
    for f_name, f_cn, required, desc in TEMPLATE_FIELDS:
        spec.append([f_cn, desc, required, ""])
    for col in ("A", "B", "C", "D"):
        spec.column_dimensions[col].width = 26 if col != "D" else 60

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
