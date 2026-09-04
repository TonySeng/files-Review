# -*- coding: utf-8 -*-
"""临时自检：法规版本/时效性元数据提取（纯文本，不调用 LLM）。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backend.services import file_store  # noqa: E402
from backend.services import legal_rules as lr  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(label, cond, extra=""):
    RESULTS.append((bool(cond), label + (f" | {extra}" if extra else "")))
    print(("PASS " if cond else "FAIL ") + label + (f" | {extra}" if extra else ""))


HEADER_TBD = """中华人民共和国招标投标法
（1999年8月30日第九届全国人民代表大会常务委员会第十一次会议通过
根据2017年12月27日第十二届全国人民代表大会常务委员会第三十一次会议《关于修改〈中华人民共和国招标投标法〉、〈中华人民共和国招标投标法实施条例〉的决定》修正）

第一章 总则
第一条 为了规范招标投标活动，保护国家利益、社会公共利益和招标投标活动当事人的合法权益，提高经济效益，保证项目质量，制定本法。
"""

HEADER_NOTICE = """国务院办公厅关于进一步加强政府投资项目管理的通知
国办发〔2026〕12号

各省、自治区、直辖市人民政府，国务院各部委、各直属机构：
为规范政府投资行为，现就有关事项通知如下。
一、严格履行项目审批程序。
"""

META_BID = lr._extract_doc_meta(HEADER_TBD)
check("提取法规名称（书名号）", META_BID and META_BID.get("law_name") == "中华人民共和国招标投标法", str(META_BID))
check("提取最新修正日期", META_BID and META_BID.get("latest_date") == "2017-12-27", str(META_BID))
check("不伪造文号", META_BID and not META_BID.get("doc_number"), str(META_BID))

META_NOTICE = lr._extract_doc_meta(HEADER_NOTICE)
check("提取通知标题", META_NOTICE and META_NOTICE.get("law_name") == "国务院办公厅关于进一步加强政府投资项目管理的通知", str(META_NOTICE))
check("提取文号", META_NOTICE and META_NOTICE.get("doc_number") == "国办发〔2026〕12号", str(META_NOTICE))

# 施行日期兜底：文本末尾写施行日期，头部无版本信息
FOOTER_ONLY = """某省招标投标管理办法

第一章 总则
第一条 ...

第五十条 本办法自2015年3月1日起施行。
"""
META_FOOTER = lr._extract_doc_meta(FOOTER_ONLY)
check("施行日期兜底", META_FOOTER and META_FOOTER.get("latest_date") == "2015-03-01", str(META_FOOTER))

# 无日期无法识别
check("垃圾文本返回 None", lr._extract_doc_meta("这里是一些无关内容，没有任何法规信息。") is None)
check("空文本返回 None", lr._extract_doc_meta("") is None)

# 时效性
stale, years = lr._is_stale({"latest_date": "2010-06-01"})
check("2010年版本判定为过时", stale and years >= 15, f"stale={stale} years={years}")
warn = lr._stale_warning({"law_name": "测试法", "latest_date": "2010-06-01"})
check("过时警告文案含关键词", "可能已被修订或废止" in warn, warn)

fresh, _ = lr._is_stale({"latest_date": "2025-01-01"})
check("2025年版本判定为未过时", not fresh)


# create_ruleset 集成：验证 source_meta 与 warnings 被写入
async def _test_create_meta():
    fake_id = "file-test-meta-001"
    fake_text = HEADER_TBD

    _orig_get = file_store.get
    _orig_mining = lr._run_mining
    _orig_persist = lr._persist

    def fake_get(fid):
        if fid == fake_id:
            return {
                "file_id": fake_id,
                "filename": "招标投标法.txt",
                "text": fake_text,
                "char_count": len(fake_text),
                "md5": "deadbeef",
            }
        return _orig_get(fid)

    async def fake_mining(rid):
        return None

    lr._persist = lambda rec: rec
    file_store.get = fake_get
    lr._run_mining = fake_mining
    try:
        rec = await lr.create_ruleset(
            name="meta测试",
            file_ids=[fake_id],
            mode="bid",
            reuse=False,
        )
        check("create_ruleset 返回记录", rec is not None)
        meta = rec.get("source_meta") or []
        check("source_meta 写入", len(meta) == 1, str(meta))
        check("source_meta 含 law_name", meta[0].get("law_name") == "中华人民共和国招标投标法")
        check("source_meta 含 latest_date", meta[0].get("latest_date") == "2017-12-27")
        # 2017-12 距今 > 8 年，应生成过时警告
        warnings = rec.get("warnings") or []
        check("生成过时警告", any("可能已被修订或废止" in w for w in warnings), str(warnings))
    finally:
        file_store.get = _orig_get
        lr._run_mining = _orig_mining
        lr._persist = _orig_persist


asyncio.run(_test_create_meta())

# 汇总
fails = [m for okk, m in RESULTS if not okk]
print(f"\nTOTAL={len(RESULTS)} PASS={len(RESULTS) - len(fails)} FAIL={len(fails)}")
if fails:
    print("FAILED ITEMS:")
    for m in fails:
        print("  -", m)
    sys.exit(1)
print("ALL_PASS")
