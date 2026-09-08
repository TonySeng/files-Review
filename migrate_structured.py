#!/usr/bin/env python3
"""结构化条件（structured）体检与迁移脚本 — 2026-09-08

背景
----
带 `structured` 的规则由「确定性引擎」锁定结论：一旦命中即 fail，LLM 无法推翻，
confidence 固定 1.0。因此这类条件配错就是**稳定的误判/漏判**，比 LLM 偶发抖动危害大得多。

本轮体检发现三类问题：
 1. 相对阈值被写成绝对阈值（如「差值小于暂估价的 10%」被配成 max_abs_diff=1000 元）→ 漏判
 2. 金额字段名把「中标候选人」这类非金额实体放进 b_field → 抽到序号/人名，判定失真
 3. forbid_keywords / require_elements 用于「否定式合规声明」类规则 → 100% 误判
    （文件写「投标人不得被列入失信被执行人名单」反而被判不合规；
     写「不存在选择性报价」同样被判命中）

用法
----
    python migrate_structured.py            # 体检 + 预演，不落库
    python migrate_structured.py --apply    # 体检 + 写入

容器内执行（脚本需在 /app 下，规则库经统一存储层持久化）：
    docker cp migrate_structured.py <backend容器>:/app/
    docker exec <backend容器> python /app/migrate_structured.py --apply

设计：按**规则语义**（名称/描述关键词）匹配，不依赖固定 rule_id，故 131 上规则 id 不同也能命中；
幂等——已修复的规则再次运行只会报告「无需变更」。
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")

from backend.services import rules_store  # noqa: E402

APPLY = "--apply" in sys.argv

# --------------------------------------------------------------------------- #
# 需要修复的规则模板
# --------------------------------------------------------------------------- #

# 中标价 vs 暂估价「异常接近」类规则：阈值必须随暂估价浮动 → 用相对阈值
PROVISIONAL_RULE = {
    "name": "中标价与暂估价异常接近（疑似泄露暂估价）",
    "description": (
        "【业务意图】暂估价属于招标控制性价格信息。正常竞争下中标价应明显低于暂估价；"
        "若中标价与暂估价\"完全一致\"或\"差异过小（＜暂估价的 10%）\"，说明投标人可能提前"
        "获知暂估价、报价缺乏竞争性，属异常接近，判定为不合规。\n"
        "【判定口径】A = 项目暂估价（仅采信明确标注为「暂估价」的金额，不得用「概算价」"
        "「预算价」「控制价」「最高限价」替代；无「暂估价」字样则无法判定）；"
        "B = 最终中标候选人（第一中标候选人 / 预中标人）的投标价（中标价）；"
        "两值统一换算为「元」后比较 R = |A − B| ÷ A（以暂估价 A 为分母，取绝对值）；"
        "本规则只做数值判定，不因文件中「暂估价/概算价」术语混用而改判。\n"
        "【结论映射（唯一，不得并存）】R = 0 或 R ＜ 10% → fail；R ≥ 10% → pass；"
        "A 或 B 任一缺失 / 无法定位 / 多值冲突 → unknown（禁止猜测，禁止用其他价格顶替）。"
    ),
    "checkpoints": [
        "从评标报告/中标候选人公示中定位「项目暂估价」金额 A 与「最终中标候选人（第一中标候选人/预中标人）投标价（中标价）」B，"
        "摘录原文并在 detail 中写明 A=…元、B=…元。二者任一缺失或无法定位 → 直接判 unknown 并在 detail 说明缺哪一项；"
        "严禁用概算价、预算价、控制价、最高限价等替代暂估价。",
        "计算差异率 R = |A − B| ÷ A（以暂估价 A 为分母，取绝对值），在 detail 中列出完整算式与百分比结果（保留两位小数），并与 10% 阈值比较。",
        "按唯一映射出结论：R = 0 或 R ＜ 10% → fail；R ≥ 10% → pass。R ≥ 10% 时一律判 pass，"
        "不得因报告中出现其他价格差（如初始报价与中标价的差额）、或「暂估价/概算价」术语不一致，而改判为 fail 或 unknown。",
    ],
    "structured": {
        "amount_pair_diff": [
            {
                "a_field": ["暂估价", "项目暂估价"],
                "b_field": ["中标价", "中标金额", "投标价", "预中标价"],
                "max_ratio": 0.1,
                "ratio_base": "a",
                "fail_when": "lt",
            }
        ]
    },
}


def _is_provisional_rule(rule: dict) -> bool:
    """识别「中标价 vs 暂估价接近度」类规则（不依赖 rule_id）。"""
    blob = f"{rule.get('name') or ''} {rule.get('description') or ''}"
    return "暂估价" in blob and ("相似度" in blob or "10%" in blob or "接近" in blob)


def _needs_fix(rule: dict) -> list[str]:
    """返回该规则存在的问题清单（空 = 无需变更）。"""
    problems: list[str] = []
    st = rule.get("structured") or {}
    apd = st.get("amount_pair_diff") or []
    for p in apd:
        if p.get("max_ratio") is None and p.get("max_abs_diff") is not None:
            problems.append(
                f"相对阈值被写成绝对阈值 max_abs_diff={p['max_abs_diff']} 元"
                "（规则语义是「差值 < 暂估价的 10%」，阈值须随基准浮动）"
            )
        for f in (p.get("b_field") or []) + (p.get("a_field") or []):
            if f in ("中标候选人", "投标人", "供应商", "候选人"):
                problems.append(f"金额字段名「{f}」不是金额实体，会抽到序号/名称导致判定失真")
        if p.get("fail_when") not in ("le", "lt"):
            problems.append(f"fail_when={p.get('fail_when')} 语义异常（应为 le/lt）")
    if (rule.get("name") or "").find("相似度审查") >= 0:
        problems.append("规则名只说「相似度审查」未体现「接近=违规」，是模型误填 fail 的锚点")
    for cp in rule.get("checkpoints") or []:
        if "是否不小于" in cp:
            problems.append(f"审核要点含双重否定「{cp}」，易致 pass/fail 映射反接")
    return problems


def _risk_flags(rule: dict) -> list[str]:
    """审计用：标记「否定式合规声明」类规则误用 forbid/require 的风险。"""
    flags: list[str] = []
    st = rule.get("structured") or {}
    if st.get("forbid_keywords"):
        flags.append("forbid_keywords 字面命中即 fail —— 否定式声明（如「不得被列入…」）会误判")
    if st.get("require_elements"):
        flags.append("require_elements 字面缺失即 fail —— 等效表述（如「无行贿犯罪记录承诺函」）会误判")
    return flags


def main() -> int:
    print("=" * 78)
    print(f" structured 体检{'（写入模式）' if APPLY else '（预演模式，加 --apply 落库）'}")
    print("=" * 78)

    rulesets = rules_store.list_rulesets()
    changed = 0
    audit: list[tuple[str, str, str, object]] = []

    for rs in rulesets:
        rid = rs.get("id")
        if rs.get("builtin"):
            audit.append((rid, rs.get("name") or "", "内置集（代码常量，本脚本不改）", None))
            for r in rs.get("rules") or []:
                st = r.get("structured")
                if isinstance(st, dict) and st:
                    audit.append((rid, r.get("id"), r.get("name") or "", st))
            continue

        rules = rs.get("rules") or []
        dirty = False
        for r in rules:
            problems = _needs_fix(r) if _is_provisional_rule(r) else []
            if not _is_provisional_rule(r):
                st = r.get("structured")
                if isinstance(st, dict) and st:
                    audit.append((rid, r.get("id"), r.get("name") or "", st))
                continue

            print(f"\n[命中] 规则集 {rid} | 规则 {r.get('id')} | {r.get('name')}")
            if not problems:
                print("       已修复，无需变更")
                continue
            for p in problems:
                print(f"       - {p}")
            before = json.dumps(
                {k: r.get(k) for k in ("name", "description", "checkpoints", "structured")},
                ensure_ascii=False,
            )
            r["name"] = PROVISIONAL_RULE["name"]
            r["description"] = PROVISIONAL_RULE["description"]
            r["checkpoints"] = list(PROVISIONAL_RULE["checkpoints"])
            r["structured"] = json.loads(json.dumps(PROVISIONAL_RULE["structured"]))
            after = json.dumps(
                {k: r.get(k) for k in ("name", "description", "checkpoints", "structured")},
                ensure_ascii=False,
            )
            print(f"       before: {before[:160]}...")
            print(f"       after : {after[:160]}...")
            dirty = True

        if dirty and APPLY:
            rules_store.save_ruleset(
                {
                    "id": rid,
                    "name": rs.get("name"),
                    "description": rs.get("description", ""),
                    "rules": rules,
                }
            )
            changed += 1
            print(f"\n[已写入] 规则集 {rid}")

    print("\n" + "=" * 78)
    print(" 其余带 structured 的规则（保留，供人工复核）")
    print("=" * 78)
    for rid, rk, nm, st in audit:
        if not isinstance(st, dict):
            print(f"  {rid} / {rk} / {nm}")
            continue
        print(f"  {rid} / {rk} / {nm}\n      {json.dumps(st, ensure_ascii=False)[:180]}")
        for f in _risk_flags({"structured": st}):
            print(f"      ⚠ {f}")

    print()
    if APPLY:
        print(f"完成：更新 {changed} 个规则集")
    else:
        print("预演结束：未写入任何数据。确认无误后加 --apply 重跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
