"""验证「跨文件一致性核查」已改造为规则驱动、核心要素可随规则集/规则组复用。

覆盖：
1. 要素库结构完整
2. 一致性格聚合：显式声明优先、文本推断回退、同名合并
3. 三种模式（bid / tender / general）的门控行为
4. 自定义规则保存时 consistency_elements 不被 _normalize_structured 丢弃
5. 提示词：规则要点覆盖内置默认、要素块渲染别名与比对要求
6. 缓存键对 dict 要素安全且随要素变化
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from services import consistency_cache, prompts, rules_store  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} {extra}")
        FAILED.append(name)


# ---------- 1. 要素库 ----------
print("\n1. 一致性要素库")
lib = rules_store.list_consistency_library()
check("要素库非空", len(lib) >= 20, f"(实际 {len(lib)})")
check(
    "每条要素都有 name/synonyms/group",
    all(e.get("name") and e.get("synonyms") and e.get("group") for e in lib),
)
_probe = lib[0]
_probe["synonyms"].append("MUTATED")
check(
    "修改返回值不污染模块常量",
    "MUTATED" not in rules_store.list_consistency_library()[0]["synonyms"],
)

# ---------- 2. 规格聚合 ----------
print("\n2. 一致性格聚合")
bid_spec = rules_store.collect_consistency_spec(rules_store.get_rules_by_mode("bid"))
check("bid 模式识别到一致性规则", "cons-cross" in bid_spec["rule_ids"], f"(实际 {bid_spec['rule_ids']})")
check("bid 模式使用规则显式声明的要素", bid_spec["explicit"] is True)
ele_names = [e["name"] for e in bid_spec["elements"]]
check("要素含投标人名称/报价金额/签署日期", {"投标人名称", "报价金额", "签署日期"} <= set(ele_names), f"(实际 {ele_names})")
check("核查要点来自规则 checkpoints", len(bid_spec["focus_points"]) >= 8, f"(实际 {len(bid_spec['focus_points'])})")

tender_spec = rules_store.collect_consistency_spec(rules_store.get_rules_by_mode("tender"))
check("tender 模式识别到 tender-consistency", "tender-consistency" in tender_spec["rule_ids"], f"(实际 {tender_spec['rule_ids']})")
t_names = [e["name"] for e in tender_spec["elements"]]
check("tender 要素含最高限价相关（报价金额）", "报价金额" in t_names, f"(实际 {t_names})")

gen_spec = rules_store.collect_consistency_spec(rules_store.get_rules_by_mode("general"))
check("general 模式无一致性规则（auto 下不执行）", gen_spec["rule_ids"] == [], f"(实际 {gen_spec['rule_ids']})")

# 旧式规则回退：category=consistency 但无 structured
legacy = [
    {
        "id": "cons-legacy",
        "name": "旧式一致性规则",
        "category": "consistency",
        "checkpoints": ["投标总价与报价表是否一致", "工期是否一致"],
    }
]
legacy_spec = rules_store.collect_consistency_spec(legacy)
check("旧式规则走文本推断", legacy_spec["explicit"] is False)
check("推断出报价金额与工期", {"报价金额", "工期"} <= {e["name"] for e in legacy_spec["elements"]},
      f"(实际 {[e['name'] for e in legacy_spec['elements']]})")

# 要素合并：两条规则声明同一要素
merged = rules_store.collect_consistency_spec(
    [
        {"id": "cons-a", "name": "A", "category": "consistency",
         "structured": {"consistency_elements": [{"name": "项目名称", "synonyms": ["标的名称"]}]}},
        {"id": "cons-b", "name": "B", "category": "consistency",
         "structured": {"consistency_elements": [{"name": "项目名称", "synonyms": ["工程名称"], "note": "须完全一致"}]}},
    ]
)
proj = [e for e in merged["elements"] if e["name"] == "项目名称"]
check("同名要素合并为一条", len(proj) == 1, f"(实际 {len(proj)})")
check("合并后同义词取并集", {"标的名称", "工程名称"} <= set(proj[0]["synonyms"]) if proj else False,
      f"(实际 {proj[0]['synonyms'] if proj else None})")

# ---------- 3. 自定义规则持久化 ----------
print("\n3. 自定义规则保存不丢要素")
normalized = rules_store._normalize_rule(
    {
        "id": "cons-custom",
        "name": "自定义一致性规则",
        "category": "consistency",
        "structured": {
            "consistency_elements": [
                {"name": "项目经理", "synonyms": ["项目负责人"], "note": "须与社保一致"},
                "证书编号",  # 简写：纯名称，同义词回查要素库
                {"name": "自定义要素", "synonyms": ["别名A"]},
            ]
        },
    }
)
saved = normalized.get("structured") or {}
check("structured 未被丢弃", bool(saved), f"(实际 {saved})")
s_names = [e["name"] for e in saved.get("consistency_elements", [])]
check("保留 3 条要素", len(s_names) == 3, f"(实际 {s_names})")
cert = next((e for e in saved.get("consistency_elements", []) if e["name"] == "证书编号"), None)
check("纯名称简写回查要素库补齐同义词", cert is not None and len(cert["synonyms"]) > 1, f"(实际 {cert})")
custom = next((e for e in saved.get("consistency_elements", []) if e["name"] == "自定义要素"), None)
check("要素库外的自定义要素保留", custom is not None and custom["synonyms"] == ["自定义要素", "别名A"], f"(实际 {custom})")

# ---------- 4. 提示词 ----------
print("\n4. 提示词渲染")
elements = [{"name": "报价金额", "synonyms": ["投标总价", "投标报价"], "note": "大小写须一致"}]
p_default = prompts.build_consistency_prompt("docs", mode="bid", elements=elements)
check("无 focus_points 时保留内置默认关注点", "投标文件与招标文件的交叉一致性核查" in p_default)
check("要素块渲染别名", "别名：投标总价、投标报价" in p_default)
check("要素块渲染比对要求", "比对要求：大小写须一致" in p_default)

p_rule = prompts.build_consistency_prompt(
    "docs", mode="bid", elements=elements, focus_points=["报价金额在各处出现时是否一致"]
)
check("有 focus_points 时覆盖内置默认", "投标文件与招标文件的交叉一致性核查" not in p_rule)
check("规则要点进入提示词", "报价金额在各处出现时是否一致" in p_rule)
check("规则要点时仍渲染要素块", "本任务一致性规则声明的核心要素" in p_rule)

p_single = prompts.build_consistency_prompt("docs", mode="bid", multi_doc=False, elements=elements)
check("单文件场景保留收敛提示", "不存在跨文件比对" in p_single)

p_file = prompts.build_file_elements_prompt("doc", elements=elements)
check("单文件要素提取提示词含规范名归并说明", "规范名必须作为 key" in p_file)

# ---------- 5. 缓存键 ----------
print("\n5. 缓存键")
k1 = consistency_cache.make_key("text", mode="bid", elements=elements, temperature=0.0, kind="elements")
k2 = consistency_cache.make_key("text", mode="bid", elements=elements, temperature=0.0, kind="elements")
check("dict 要素可生成缓存键且稳定", k1 == k2 and bool(k1))
k3 = consistency_cache.make_key(
    "text", mode="bid",
    elements=[{"name": "报价金额", "synonyms": ["投标总价"], "note": "大小写须一致"}],
    temperature=0.0, kind="elements",
)
check("要素变化（同义词减少）缓存键变化", k1 != k3)
k4 = consistency_cache.make_key("text", mode="bid", elements=["报价金额"], temperature=0.0, kind="elements")
check("字符串要素仍兼容", bool(k4))

print("\n" + ("全部通过" if not FAILED else f"失败 {len(FAILED)} 项: {FAILED}"))
sys.exit(1 if FAILED else 0)
