# -*- coding: utf-8 -*-
"""临时自检：半自动 structured_hint 全链路（不调用 LLM）。

覆盖：
1. 挖掘提示词包含 structured_hint 规范；
2. _sanitize_structured 白名单校验；
3. coerce_rule 默认剥离模型直供 structured、保留 sanitized hint；
4. coerce_rule(allow_structured=True) 用户路径可激活 structured；
5. update_ruleset → resolve_rules 透传 structured；
6. _apply_structured_rules 对已转换规则产出确定性结论（含万元换算回归修复）；
7. promote_to_ruleset 转正保留 structured。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backend.services import legal_rules as lr  # noqa: E402
from backend.services import prompt_store  # noqa: E402
from backend.services import review_engine as re_eng  # noqa: E402
from backend.services import rules_store  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(label, cond, extra=""):
    RESULTS.append((bool(cond), label + (f" | {extra}" if extra else "")))
    print(("PASS " if cond else "FAIL ") + label + (f" | {extra}" if extra else ""))


# ---------------------------------------------------------------- 1) 提示词
# Prompt 已迁移至 prompt_store 统一管理；此处校验内置模板原文（不受运行时自定义影响）
_MINING_SYS = prompt_store.BUILTIN_PROMPTS["legal_mining_system"]["content"]
check("提示词含 structured_hint 规范", "structured_hint" in _MINING_SYS)
check("提示词要求金额换算为元", "换算为**元**" in _MINING_SYS)
check("提示词禁止时限硬套金额阈值", "不要**硬套" in _MINING_SYS or "不要**硬套 amount_thresholds" in _MINING_SYS or "硬套" in _MINING_SYS)

# ---------------------------------------------------------------- 2) 白名单校验
s = lr._sanitize_structured({"forbid_keywords": ["串通投标", " "], "amount_thresholds": [{"field": "投标保证金", "max": 800000}]})
check("合法 hint 归一化保留", isinstance(s, dict) and s.get("forbid_keywords") == ["串通投标"])
check("空白关键词被剔除", " " not in (s or {}).get("forbid_keywords", ["x"]))
check("amount_thresholds 保留", (s or {}).get("amount_thresholds") == [{"field": "投标保证金", "max": 800000.0}])
check("非法类型 → None", lr._sanitize_structured(["not", "a", "dict"]) is None)
check("全垃圾字段 → None", lr._sanitize_structured({"foo": "bar", "evil": {"hack": 1}}) is None)
check("None → None", lr._sanitize_structured(None) is None)

# ---------------------------------------------------------------- 3) coerce_rule 默认（模型路径）
RAW_LLM = {
    "name": "投标保证金不得超过80万元",
    "category": "commercial",
    "severity": "critical",
    "description": "投标保证金上限为项目估算价2%且不超过80万元",
    "checkpoints": ["核查投标保证金金额", "核查是否超过估算价2%"],
    "legal_basis": "第二十六条",
    "applies_to": "both",
    "structured_hint": {"amount_thresholds": [{"field": "投标保证金", "max": 800000}]},
    # 模型试图自行激活确定性引擎 —— 必须被剥离
    "structured": {"forbid_keywords": ["一切投标文件"]},
}
r1 = lr.coerce_rule(RAW_LLM, seq=1, prefix="lr-ab12", source_file="law.txt")
check("hint 保留", isinstance(r1, dict) and "structured_hint" in r1)
check("hint 白名单归一化", r1["structured_hint"].get("amount_thresholds") == [{"field": "投标保证金", "max": 800000.0}])
check("模型直供 structured 被剥离", "structured" not in r1, f"keys={sorted(r1.keys())}")

# 无 hint 的规则不产生空字段
r1b = lr.coerce_rule({k: v for k, v in RAW_LLM.items() if k not in ("structured_hint", "structured")}, seq=2, prefix="lr-ab12", source_file="law.txt")
check("无 hint 规则不含 structured_hint/structured", "structured_hint" not in r1b and "structured" not in r1b)

# ---------------------------------------------------------------- 4) coerce_rule 用户路径
RAW_USER = dict(RAW_LLM)
RAW_USER["structured"] = {"amount_thresholds": [{"field": "投标保证金", "max": 800000}]}
r2 = lr.coerce_rule(RAW_USER, seq=1, prefix="lr-ab12", source_file="law.txt", allow_structured=True)
check("用户路径 structured 保留", isinstance(r2, dict) and r2.get("structured", {}).get("amount_thresholds"))
check("用户路径 structured 与 hint 共存", "structured_hint" in r2 and "structured" in r2)
# 用户路径提交非法 structured → 剥离，不报错
RAW_BAD = dict(RAW_LLM)
RAW_BAD["structured"] = {"evil": True}
r2b = lr.coerce_rule(RAW_BAD, seq=1, prefix="lr-ab12", source_file="law.txt", allow_structured=True)
check("用户路径非法 structured 被白名单剥离", "structured" not in r2b)

# ---------------------------------------------------------------- 5) update_ruleset → resolve_rules 透传
RID = "lrs-test0001"
_rec = {
    "id": RID,
    "owner_id": None,
    "name": "测试集",
    "description": "",
    "rules": [r1],
    "stats": {"kept": 1},
    "version": 1,
    "status": "ready",
    "progress": 100.0,
    "is_shared": False,
    "created_at": "2026-09-03T00:00:00",
    "updated_at": "2026-09-03T00:00:00",
}
_orig_items, _orig_persist = lr._items, lr._persist
lr._items = {RID: json.loads(json.dumps(_rec))}
lr._persist = lambda rec: json.loads(json.dumps(rec))
try:
    # 用户「转确定性」：前端把 hint 复制为 structured 后 PATCH 全量规则
    user_rules = [dict(r1, structured=r1["structured_hint"])]
    upd = lr.update_ruleset(RID, user_id=None, rules=user_rules)
    check("update_ruleset 返回记录", upd is not None)
    check("版本递增", upd["version"] == 2, f"v={upd['version']}")
    ru = upd["rules"][0]
    check("转换后规则含 structured", ru.get("structured", {}).get("amount_thresholds") is not None)
    check("转换后 hint 仍在", "structured_hint" in ru)

    # 撤销：再 PATCH 不带 structured
    upd2 = lr.update_ruleset(RID, user_id=None, rules=[{k: v for k, v in ru.items() if k != "structured"}])
    check("撤销后 structured 消失", "structured" not in upd2["rules"][0])
    check("撤销后 hint 保留", "structured_hint" in upd2["rules"][0])

    # 再转一次，供 resolve/engine 用
    upd3 = lr.update_ruleset(RID, user_id=None, rules=[dict(ru)])
    resolved, missing = lr.resolve_rules([RID], None)
    check("resolve_rules 无 missing", missing == [])
    rr = resolved[0]
    check("resolve_rules 透传 structured", rr.get("structured", {}).get("amount_thresholds") is not None)
    check("resolve_rules id 加规则集前缀", rr["id"] == f"{RID}:lr-test00-001", rr["id"])

    # ---------------------------------------------------------------- 6) 确定性引擎
    def _run_engine(rule_list, doc_text):
        docs = [{"file_id": "f1", "file_type": "bid"}]
        return re_eng._apply_structured_rules(rule_list, docs, [doc_text])

    # 6a. 万元换算回归修复：100万元(=1,000,000) > 800,000 上限 → fail
    f = _run_engine(resolved, "投标文件\n投标保证金：100万元\n开户行：XX银行")
    key = rr["id"]
    check("100万元 > 80万上限 → 确定性 fail", key in f and f[key]["status"] == "fail", f.get(key, {}).get("detail", ""))
    check("结论标记 deterministic=True", f.get(key, {}).get("deterministic") is True)
    check("fail 详情含换算后金额", "1,000,000" in f[key]["detail"], f[key]["detail"])

    # 6b. 合规：50万元 ≤ 800,000 → amount_thresholds 不锁 pass（回落 LLM 综合判定）
    f2 = _run_engine(resolved, "投标文件\n投标保证金：50万元\n")
    check("50万元未超限 → 不产确定性结论(回落LLM)", key not in f2)

    # 6c. 字段未出现 → 跳过
    f3 = _run_engine(resolved, "投标文件\n无任何保证金表述\n")
    check("字段未出现 → 跳过", key not in f3)

    # 6d. forbid_keywords 转换后的规则
    rule_fk = dict(rr, structured={"forbid_keywords": ["串通投标"]})
    f4 = _run_engine([rule_fk], "投标文件\n经查存在串通投标行为")
    check("禁止关键词命中 → 确定性 fail", f4.get(key, {}).get("status") == "fail")

    # 6e. 纯 hint（未转换）不进确定性引擎
    rule_hint_only = {k: v for k, v in rr.items() if k != "structured"}
    f5 = _run_engine([rule_hint_only], "投标保证金：100万元")
    check("未转换(仅hint) → 确定性引擎不消费", key not in f5)

    # ---------------------------------------------------------------- 7) promote 转正保留 structured
    captured = {}
    _orig_save = rules_store.save_ruleset
    rules_store.save_ruleset = lambda payload, **kw: captured.update(payload) or dict(payload)
    try:
        lr.promote_to_ruleset(RID, user_id=None, name="转正测试")
    finally:
        rules_store.save_ruleset = _orig_save
    pro = (captured.get("rules") or [{}])[0]
    check("promote 转正保留 structured", pro.get("structured", {}).get("amount_thresholds") is not None)
    check("promote 依据并入 description", "第二十六条" in pro.get("description", ""))
finally:
    lr._items, lr._persist = _orig_items, _orig_persist

# ---------------------------------------------------------------- 汇总
fails = [m for okk, m in RESULTS if not okk]
print(f"\nTOTAL={len(RESULTS)} PASS={len(RESULTS) - len(fails)} FAIL={len(fails)}")
if fails:
    print("FAILED ITEMS:")
    for m in fails:
        print("  -", m)
    sys.exit(1)
print("ALL_PASS")
