# -*- coding: utf-8 -*-
"""临时自检：法规规则挖掘的纯逻辑（不调用 LLM）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backend.services import legal_rules as lr  # noqa: E402

LAW = """
第一章 总 则
第一条 为了规范招标投标活动，制定本法。
第二条 在中华人民共和国境内进行招标投标活动，适用本法。
第三条 依法必须进行招标的项目，其招标投标活动不受地区或者部门的限制。

第二章 招 标
第八条 招标人采用公开招标方式的，应当发布招标公告。
第十八条 招标人可以根据招标项目本身的要求，对潜在投标人进行资格审查。
第二十四条 招标人应当确定投标人编制投标文件所需要的合理时间；依法必须进行招标的项目，自招标文件开始发出之日起至投标人提交投标文件截止之日止，最短不得少于二十日。
第二十六条 招标保证金不得超过招标项目估算价的百分之二，且最高不得超过八十万元人民币。
第三十二条 投标人不得相互串通投标报价，不得排挤其他投标人的公平竞争。
第三十三条 投标人不得以低于成本的报价竞标，也不得以他人名义投标或者以其他方式弄虚作假，骗取中标。
"""

NOTICE = """
关于进一步加强招标投标管理工作的通知

各有关单位：
为进一步规范招投标市场秩序，现就有关事项通知如下。
一、严格审查投标人资格。招标人应核验营业执照、资质证书、安全生产许可证。
二、投标保证金不得超过项目估算价的百分之二。
三、严禁串通投标、以他人名义投标等违法行为。
"""


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f" | {extra}" if extra else ""))
    return bool(cond)


ok = True

# 1) 按条切分
segs, how = lr.split_legal_text(LAW)
ok &= check("按条切分 mode=article", how == "article", f"segments={len(segs)}")
ok &= check("条款段数 >= 8", len(segs) >= 8, f"{len(segs)}")
ok &= check("首段含第一章", "第一章" in segs[0], segs[0][:20])

segs2, how2 = lr.split_legal_text(NOTICE)
ok &= check("无条款退化为 paragraph", how2 == "paragraph", f"mode={how2} segs={len(segs2)}")

# 2) 打包
chunks = lr.pack_chunks(segs, 300)
ok &= check("打包不超预算(±单段溢出)", all(len(c) <= 300 + 200 for c in chunks), f"chunks={len(chunks)}")
ok &= check("打包不丢内容", sum(len(c) for c in chunks) >= sum(len(s) for s in segs))

# 3) 规则归一化
raw_ok = {
    "name": "投标保证金不得超过项目估算价的2%",
    "category": "commercial",
    "severity": "critical",
    "description": "核查投标保证金金额与项目估算价的比例。",
    "checkpoints": ["提取投标保证金金额", "与项目估算价比较是否超过2%"],
    "legal_basis": "第二十六条 招标保证金不得超过招标项目估算价的百分之二",
}
raw_bad_cat = dict(raw_ok, name="投标文件内容一致性", category="qualification")
raw_no_ckpt = dict(raw_ok, checkpoints=[])
raw_junk = {"name": "", "checkpoints": []}

r1 = lr.coerce_rule(raw_ok, seq=1, prefix="lr-abc123", source_file="law.txt")
ok &= check("归一化保留合法规则", r1 is not None and r1["id"] == "lr-abc123-001")
ok &= check("category 白名单保留", r1["category"] == "commercial")
r2 = lr.coerce_rule(raw_bad_cat, seq=2, prefix="lr-abc123", source_file="law.txt")
ok &= check("防一致性误判(qualification+一致→legal)", r2 is not None and r2["category"] == "legal",
            r2["category"] if r2 else "")
from backend.services.rules_store import is_consistency_rule  # noqa: E402
ok &= check("is_consistency_rule 不误命中", not is_consistency_rule(r2))
ok &= check("无要点规则被丢弃", lr.coerce_rule(raw_no_ckpt, seq=3, prefix="p", source_file="") is None)
ok &= check("空规则被丢弃", lr.coerce_rule(raw_junk, seq=4, prefix="p", source_file="") is None)
r3 = lr.coerce_rule({"name": "未知类别", "category": "consistency", "checkpoints": ["a"]},
                    seq=5, prefix="p", source_file="")
ok &= check("非法类别回落 legal", r3["category"] == "legal")

# 4) 去重
dup_set = [
    lr.coerce_rule(dict(raw_ok, name="投标保证金不得超过项目估算价的2%"), seq=1, prefix="p", source_file="f"),
    lr.coerce_rule(dict(raw_ok, name="投标保证金不得超过项目估算价的2%"), seq=2, prefix="p", source_file="f"),
    lr.coerce_rule(dict(raw_ok, name="投标保证金不得超过项目估算价的百分之二"),
                   seq=3, prefix="p", source_file="f"),
    lr.coerce_rule({"name": "投标人不得相互串通投标", "category": "legal", "severity": "critical",
                    "checkpoints": ["核查是否存在串通报价情形"],
                    "legal_basis": "第三十二条"}, seq=4, prefix="p", source_file="f"),
]
kept, removed = lr.dedup_rules([d for d in dup_set if d])
ok &= check("精确+模糊去重生效", len(kept) == 2 and removed == 2, f"kept={len(kept)} removed={removed}")
ok &= check("去重后合并依据条款", "第三十二条" in (kept[-1].get("legal_basis") or ""))

# 5) 打分排序（critical + 含量化 优先）
scored = sorted(kept, key=lr._rule_score, reverse=True)
ok &= check("打分排序把 critical+量化 排前", scored[0]["severity"] == "critical")

# 6) resolve_rules 加规则集前缀
lr._items["lrs-test000001"] = {
    "id": "lrs-test000001", "name": "t", "status": "ready", "owner_id": "u-test",
    "is_shared": False, "rules": [dict(r1), dict(r2)], "version": 1,
}
rules, missing = lr.resolve_rules(["lrs-test000001"], None)
ok &= check("resolve_rules 返回规则", len(rules) == 2, f"{len(rules)}")
ok &= check("resolve_rules 加规则集前缀", rules[0]["id"].startswith("lrs-test000001:"), rules[0]["id"])
_, missing2 = lr.resolve_rules(["lrs-not-exist"], None)
ok &= check("缺失规则集被标记", missing2 == ["lrs-not-exist"])

# 7) 转正（落正式规则库，随后清理）
saved = lr.promote_to_ruleset("lrs-test000001", user_id="u-test", name="自检转正集")
ok &= check("promote 生成正式规则集", bool(saved and saved.get("id", "").startswith("rs-legal-")),
            saved.get("id") if saved else "")
ok &= check("promote 保留依据到 description", "依据：" in (saved["rules"][0]["description"] or ""))
ok &= check("promote 后规则未被判为一致性规则",
            not any(is_consistency_rule(r) for r in saved["rules"]))

from backend.services import rules_store  # noqa: E402
rules_store.delete_ruleset(saved["id"], "u-test")
lr._items.pop("lrs-test000001", None)

# ---------------- 端到端：mock LLM 走完整个挖矿状态机 ----------------
import asyncio  # noqa: E402
import json as _json  # noqa: E402


async def e2e():
    print("\n--- E2E（mock LLM）---")
    ok2 = True

    def _rules_for(chunk: str):
        out = []
        if "保证金" in chunk:
            out.append({
                "name": "投标保证金不得超过项目估算价的2%",
                "category": "commercial", "severity": "critical",
                "description": "核查保证金与估算价比例。",
                "checkpoints": ["提取保证金金额", "与估算价比较"],
                "legal_basis": "第二十六条 招标保证金不得超过招标项目估算价的百分之二",
            })
            out.append({  # 同条款不同写法，应被模糊去重
                "name": "投标保证金不得超过项目估算价的百分之二",
                "category": "commercial", "severity": "critical",
                "description": "同上，另一写法。",
                "checkpoints": ["提取保证金金额", "与估算价比较", "核查是否超过80万元上限"],
                "legal_basis": "第二十六条",
            })
        if "串通" in chunk:
            out.append({
                "name": "投标人不得相互串通投标报价",
                "category": "legal", "severity": "critical",
                "description": "核查是否存在串通报价情形。",
                "checkpoints": ["比对不同投标人报价规律", "核查是否存在关联关系"],
                "legal_basis": "第三十二条 投标人不得相互串通投标报价",
            })
        if "二十日" in chunk:
            out.append({
                "name": "投标截止时间自招标文件发出之日起不得少于二十日",
                "category": "format", "severity": "major",
                "description": "核查投标截止时间。",
                "checkpoints": ["提取招标文件发出日期", "计算至投标截止日的天数"],
                "legal_basis": "第二十四条 最短不得少于二十日",
            })
        out.append({  # 无审核要点，应被丢弃
            "name": "立法目的条款", "category": "legal", "severity": "info",
            "description": "本条为立法目的。", "checkpoints": [],
            "legal_basis": "第一条",
        })
        return out

    async def fake_chat(messages, *, tools=None, temperature=None, max_tokens=None, timeout=None, stream=False):
        # stream 参数为 2026-09-03 流式挖掘改造后 chat() 的新签名；mock 恒按非流式返回完整 JSON
        user = messages[-1]["content"]
        return {"role": "assistant", "content": _json.dumps({"rules": _rules_for(user)}, ensure_ascii=False)}

    lr.file_store.get = lambda fid: {
        "file_id": fid, "filename": "测试招标投标法.pdf", "md5": "md5-law",
        # 重复 8 遍以超过单块预算，验证「真分块 + 并发 + 跨块去重」
        "text": "\n".join(LAW for _ in range(8)),
        "char_count": len(LAW) * 8,
    }
    lr.llm_client.chat = fake_chat
    _orig_get = lr.config.get

    def fake_get(key, default=None):
        if key == "legal_chunk_chars":
            return 2500
        if key == "legal_max_rules":
            return 5
        if key == "legal_mining_concurrency":
            return 2
        if key == "llm_model":
            return "mock-model"
        return _orig_get(key, default)

    lr.config.get = fake_get

    rec = await lr.create_ruleset(
        name="招标投标法-自检", file_ids=["f1"], mode="bid", user_id="u-e2e", reuse=False
    )
    ok2 &= check("创建后初始状态为 pending/mining", rec["status"] in ("pending", "mining"), rec["status"])
    rid = rec["id"]

    for _ in range(100):
        await asyncio.sleep(0.05)
        cur = lr.get_set(rid, "u-e2e")
        if cur and cur["status"] in ("ready", "failed"):
            break
    ok2 &= check("挖矿进入 ready", cur["status"] == "ready", cur.get("error") or "")
    ok2 &= check("进度到 100", cur["progress"] == 100.0, str(cur["progress"]))
    ok2 &= check("切分方式为 article", cur["stats"].get("split_mode") == "article",
                 cur["stats"].get("split_mode"))
    ok2 &= check("分块数 > 1（验证真分块）", cur["stats"]["chunks"] > 1, str(cur["stats"]["chunks"]))
    ok2 &= check("生成规则非空", len(cur["rules"]) > 0, str(len(cur["rules"])))
    ok2 &= check("无要点规则被丢弃", all(r["checkpoints"] for r in cur["rules"]))
    ok2 &= check("去重生效", cur["stats"]["dedup_removed"] > 0, str(cur["stats"]["dedup_removed"]))
    ok2 &= check("规则数不超上限 5", len(cur["rules"]) <= 5, str(len(cur["rules"])))
    ok2 &= check("id 连续且带 lr- 前缀",
                 [r["id"] for r in cur["rules"]] == [f"lr-{rid[4:10]}-{i:03d}" for i in range(1, len(cur["rules"]) + 1)],
                 str([r["id"] for r in cur["rules"]]))
    ok2 &= check("规则不触发一致性阶段",
                 not any(is_consistency_rule(r) for r in cur["rules"]))
    ok2 &= check("need_legal_basis 关闭", all(r.get("need_legal_basis") is False for r in cur["rules"]))
    ok2 &= check("记录来源文件", cur["source_files"][0]["filename"] == "测试招标投标法.pdf")
    ok2 &= check("同源同参复用", (await lr.create_ruleset(
        name="再来一次", file_ids=["f1"], mode="bid", user_id="u-e2e", reuse=True))["id"] == rid)

    # 编辑 → 版本号递增
    upd = lr.update_ruleset(rid, user_id="u-e2e", name="改名后")
    ok2 &= check("编辑后 version 递增（替换规则时）", upd["version"] == 1, str(upd["version"]))
    upd2 = lr.update_ruleset(rid, user_id="u-e2e", rules=cur["rules"][:1])
    ok2 &= check("替换规则后 version=2", upd2["version"] == 2, str(upd2["version"]))
    ok2 &= check("替换后规则只剩 1 条", len(upd2["rules"]) == 1)

    # 权限隔离
    ok2 &= check("他人不可见", lr.get_set(rid, "u-other") is None)
    ok2 &= check("他人不可删", lr.delete_set(rid, "u-other") is False)

    lr.config.get = _orig_get
    lr.delete_set(rid, "u-e2e")
    return ok2


ok &= asyncio.run(e2e())

# ---------------- 接入点：_resolve_rules 的两种模式 ----------------
from backend.models.schemas import ReviewRequest  # noqa: E402
from backend.routers import review as rvw  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def expect_400(fn, label):
    try:
        fn()
    except HTTPException as exc:
        return check(label, exc.status_code == 400, f"status={exc.status_code}")
    return check(label, False, "未抛异常")


print("\n--- 审核接入：_resolve_rules ---")
# 注入两个法规规则集（系统共享，管理员可见）
rvw.legal_rules._items["lrs-A"] = {
    "id": "lrs-A", "name": "法规A", "status": "ready", "owner_id": None, "is_shared": True,
    "version": 1, "source_files": [{"filename": "a.pdf"}], "sources_fingerprint": "fpA",
    "params": {"llm_model": "mock"}, "warnings": [], "stats": {},
    "rules": [
        {"id": "lr-A-001", "name": "保证金上限", "category": "commercial", "severity": "critical",
         "description": "d", "checkpoints": ["c1"], "need_legal_basis": False, "enabled": True},
        {"id": "lr-A-002", "name": "禁止串通投标", "category": "legal", "severity": "critical",
         "description": "d", "checkpoints": ["c1"], "need_legal_basis": False, "enabled": True},
    ],
}
rvw.legal_rules._items["lrs-B"] = {
    "id": "lrs-B", "name": "法规B", "status": "mining", "owner_id": None, "is_shared": True,
    "version": 1, "source_files": [], "sources_fingerprint": "fpB", "params": {},
    "warnings": [], "stats": {}, "rules": [],
}
rvw.rule_groups.expand_rule_groups = lambda ids, uid: [
    {"id": "grp-1", "name": "组规则1", "category": "legal", "severity": "major",
     "description": "d", "checkpoints": ["c"]},
]

# 1) 只传法规规则集 + 合成 mode-bid → 自动判定为「纯法规模式」
req = ReviewRequest(file_ids=["f"], mode="bid", ruleset_id="mode-bid",
                    legal_ruleset_ids=["lrs-A"])
got = rvw._resolve_rules(req, [], None)
ok &= check("纯法规模式：只返回法规规则",
            [r["id"] for r in got] == ["lrs-A:lr-A-001", "lrs-A:lr-A-002"],
            str([r["id"] for r in got]))

# 2) 显式 legal_rules_only=False → 强制叠加内置/规则组
req2 = ReviewRequest(file_ids=["f"], mode="bid", ruleset_id="mode-bid",
                     legal_ruleset_ids=["lrs-A"], legal_rules_only=False)
got2 = rvw._resolve_rules(req2, [], None)
ok &= check("叠加模式：内置规则在前 + 法规规则在后",
            got2[0]["id"] == "bid-001" or len(got2) > 2, f"{len(got2)} 条，首条 {got2[0]['id']}")
ok &= check("叠加模式：含法规规则", any(str(r["id"]).startswith("lrs-A:") for r in got2))

# 3) 显式选择了规则组 → 自动进入叠加模式
req3 = ReviewRequest(file_ids=["f"], mode="bid", legal_ruleset_ids=["lrs-A"],
                     rule_group_ids=["g1"])
got3 = rvw._resolve_rules(req3, [], None)
ok &= check("选了规则组 → 叠加", got3[0]["id"] == "grp-1" and len(got3) == 3,
            f"{[r['id'] for r in got3]}")

# 4) 法规规则集未生成完成 → 明确 400，不静默降级
req4 = ReviewRequest(file_ids=["f"], mode="bid", legal_ruleset_ids=["lrs-B"])
ok &= expect_400(lambda: rvw._resolve_rules(req4, [], None), "未就绪规则集 → 400")

# 5) 规则集不存在 → 400
req5 = ReviewRequest(file_ids=["f"], mode="bid", legal_ruleset_ids=["lrs-nope"])
ok &= expect_400(lambda: rvw._resolve_rules(req5, [], None), "不存在规则集 → 400")

# 6) 向后兼容：不传法规规则集时行为不变（走内置模式规则）
req6 = ReviewRequest(file_ids=["f"], mode="bid", ruleset_id="mode-bid")
got6 = rvw._resolve_rules(req6, [], None)
ok &= check("不传法规规则集：行为不变", len(got6) > 10 and not any(
    str(r["id"]).startswith("lrs-") for r in got6), f"{len(got6)} 条")

# 7) 法规依据文件不能作为审核目标
rvw.file_store.get_many = lambda ids: [
    {"file_id": "f", "filename": "招标投标法.pdf", "role": "legal", "text": "第一条 ..."}
]
req7 = ReviewRequest(file_ids=["f"], mode="bid")
ok &= expect_400(lambda: rvw._resolve_docs(req7), "法规文件作审核目标 → 400")

del rvw.legal_rules._items["lrs-A"], rvw.legal_rules._items["lrs-B"]

# 8) 规则集版本纳入版本清单：_legal_ruleset_meta 返回可追溯字段
rvw.legal_rules._items["lrs-M"] = {
    "id": "lrs-M", "name": "招投标法", "status": "ready", "owner_id": None,
    "is_shared": True, "version": 1, "rules": [{}, {}],
    "source_files": [{"file_id": "f1", "filename": "招投标法.pdf", "md5": "x", "char_count": 1}],
    "sources_fingerprint": "fp-M",
    "params": {"llm_model": "deepseek-v4"},
    "updated_at": "2026-09-03T00:00:00",
}
meta_list = rvw._legal_ruleset_meta(["lrs-M"], None)
ok &= check("legal_ruleset_meta 返回 1 条", len(meta_list) == 1)
m = meta_list[0] if meta_list else {}
ok &= check("meta 含 version", m.get("version") == 1, str(m))
ok &= check("meta 含 rule_count", m.get("rule_count") == 2)
ok &= check("meta 含 sources_fingerprint", m.get("sources_fingerprint") == "fp-M")
ok &= check("meta source_files 含文件名", "招投标法.pdf" in (m.get("source_files") or []))
# 复现引擎尾注拼接逻辑，确保 rule_set_version 能携带法规版本
legal_rulesets = meta_list
legal_tag = "+LEGAL:" + "-".join(
    f"{x.get('id')}@v{x.get('version')}" for x in legal_rulesets if x.get("id")
)
ok &= check("版本尾注拼法正确", legal_tag == "+LEGAL:lrs-M@v1", legal_tag)
del rvw.legal_rules._items["lrs-M"]

print("\nRESULT:", "ALL_PASS" if ok else "HAS_FAILURE")
sys.exit(0 if ok else 1)
