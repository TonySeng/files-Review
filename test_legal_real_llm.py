"""真实 LLM 联调（精简版）：用《招标投标法》若干真实条款，走完整法规规则挖掘链路。
验证：切分 → 抽取 → 归一化/去重/截断 → 产出规则形态，及一致性误判防护、版本指纹。
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")

from backend import config  # noqa: E402
from backend.services import file_store, legal_rules, rules_store  # noqa: E402

# 《招标投标法》真实条款节选（公开法律文本，测试输入）
LAW = """第三条 在中华人民共和国境内进行下列工程建设项目包括项目的勘察、设计、施工、监理以及与工程建设有关的重要设备、材料等的采购，必须进行招标：（一）大型基础设施、公用事业等关系社会公共利益、公众安全的项目；（二）全部或者部分使用国有资金投资或者国家融资的项目；（三）使用国际组织或者外国政府贷款、援助资金的项目。

第二十六条 投标人应当具备承担招标项目的能力；国家有关规定对投标人资格条件或者招标文件对投标人资格条件有规定的，投标人应当具备规定的资格条件。投标人应当按照招标文件的要求编制投标文件。投标文件应当对招标文件提出的实质性要求和条件作出响应。

第三十三条 投标人不得以低于成本的报价竞标，也不得以他人名义投标或者以其他方式弄虚作假，骗取中标。

第四十一条 中标人的投标应当符合下列条件之一：（一）能够最大限度地满足招标文件中规定的各项综合评价标准；（二）能够满足招标文件的实质性要求，并且经评审的投标价格最低；但是投标价格低于成本的除外。

第四十六条 招标人和中标人应当自中标通知书发出之日起三十日内，按照招标文件和中标人的投标文件订立书面合同。招标人和中标人不得再行订立背离合同实质性内容的其他协议。
"""

fid = "law-probe-001"


async def main() -> None:
    file_store.get = lambda f: {
        "file_id": f,
        "filename": "招标投标法节选.txt",
        "md5": "probe-md5",
        "ext": "txt",
        "role": "legal",
        "char_count": len(LAW),
        "text": LAW,
    }

    print(f"[inject] 法规 {len(LAW)} 字，单块（< {int(config.get('legal_chunk_chars', 12000))} 字）", flush=True)

    rec = await legal_rules.create_ruleset(
        name="招标投标法-联调真实验证",
        file_ids=[fid],
        mode="bid",
        user_id="u-probe",
    )
    rid = rec["id"]
    print(f"[created] {rid} status={rec['status']} fp={rec['sources_fingerprint'][:16]}", flush=True)

    for i in range(90):
        await asyncio.sleep(2)
        d = legal_rules.get_set(rid, None)
        st = d["status"]
        if i % 3 == 0 or st in ("ready", "failed"):
            print(f"  t={i*2}s status={st} progress={d.get('progress')} msg={d.get('progress_message')}", flush=True)
        if st in ("ready", "failed"):
            break
    else:
        print("[timeout] 180s 内未完成", flush=True)
        return

    rules = d.get("rules") or []
    print(f"\n[result] status={d['status']} rules={len(rules)}", flush=True)
    print(f"[stats] {json.dumps(d.get('stats'), ensure_ascii=False)}", flush=True)
    print(f"[warnings] {d.get('warnings')}", flush=True)
    print("=== 产出规则样例 ===", flush=True)
    for r in rules[:12]:
        print(f"  - [{r.get('severity')}/{r.get('category')}] {r.get('name')}", flush=True)
        print(f"      依据: {r.get('legal_basis')}", flush=True)
        print(f"      要点: {r.get('checkpoints')}", flush=True)

    bad = [r.get("name") for r in rules if rules_store.is_consistency_rule(r)]
    print(f"\n[guard] 一致性规则误判数: {len(bad)} {bad if bad else ''}", flush=True)
    cap = int(config.get("legal_max_rules", 30))
    print(f"[guard] 规则数 {len(rules)} ≤ 上限 {cap}: {len(rules) <= cap}", flush=True)

    with open("deliverables/法规挖掘联调简报.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "status": d["status"],
                "rule_count": len(rules),
                "stats": d.get("stats"),
                "warnings": d.get("warnings"),
                "consistency_false_positive": bad,
                "sample_rules": [
                    {
                        "name": r.get("name"),
                        "severity": r.get("severity"),
                        "category": r.get("category"),
                        "legal_basis": r.get("legal_basis"),
                        "checkpoints": r.get("checkpoints"),
                    }
                    for r in rules[:15]
                ],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print("[done] 简报已写入 deliverables/法规挖掘联调简报.json", flush=True)


asyncio.run(main())
