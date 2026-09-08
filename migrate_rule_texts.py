# -*- coding: utf-8 -*-
"""决标规则集（rs-8e3c51a9）规则文案外科手术式更新（final25）。

背景：2026-09-08 用户发现 dec-07（时间逻辑规则）下模型输出「投标人名称重复」
的口径外幻觉结论。根因之一是规则说明未限定判定对象/范围/认定标准/不触发条件。
本次按「判定对象/判定范围/认定标准/不触发条件」四要素重写全部 dec-* 规则文案。

⚠️ 只替换 description / checkpoints，保留库内已有的 severity、doc_types、
structured、enabled、need_legal_basis 等运行时配置
（直接走 JSON 重导入会把这些配置抹掉，故用本脚本做原地修补）。

用法：
  容器内:  docker exec biddingfiles-review-backend-1 python /app/migrate_rule_texts.py
  宿主机:  python migrate_rule_texts.py（需能 import backend.*）
  131 上:  解压部署包后 docker exec <backend容器> python /app/migrate_rule_texts.py
"""
import os
import sys

sys.path.insert(0, "/app" if os.path.exists("/app") else os.path.dirname(os.path.abspath(__file__)))

from backend.services import rules_store  # noqa: E402
from backend.gen_decision_rules import RULES  # noqa: E402

RULESET_ID = "rs-8e3c51a9"


def main() -> int:
    rs = rules_store.get_ruleset(RULESET_ID, None)
    if not rs:
        print(f"[ERR] 规则集 {RULESET_ID} 不存在")
        return 1
    texts = {rid: (desc, cps) for rid, _n, _c, _s, desc, cps, _l in RULES}
    changed = 0
    rules = rs.get("rules", [])
    for r in rules:
        rid = str(r.get("id") or "")
        if rid not in texts:
            continue
        desc, cps = texts[rid]
        if r.get("description") != desc or list(r.get("checkpoints") or []) != list(cps):
            r["description"] = desc
            r["checkpoints"] = list(cps)
            changed += 1
    if not changed:
        print("无变更（文案已是最新）")
        return 0
    saved = rules_store.save_ruleset(
        {
            "id": rs["id"],
            "name": rs.get("name"),
            "description": rs.get("description", ""),
            "builtin": False,
            "rules": rules,
        }
    )
    print(f"已更新 {changed} 条规则文案（severity/doc_types/structured 均保留）: {saved['id']}")
    for r in saved["rules"]:
        if str(r.get("id")) in texts:
            print(
                " -", r["id"], r.get("name"),
                "| sev:", r.get("severity"),
                "| doc_types:", r.get("doc_types"),
                "| structured:", "Y" if r.get("structured") else "-",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
