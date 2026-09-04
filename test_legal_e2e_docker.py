# -*- coding: utf-8 -*-
"""E2E 验证容器化后端(:9100)在 legal_chunk_chars=6000 配置下的法规挖掘。

构造 ~9.5k 字法规文本：旧配置(12000)会打成 1 个必然超时的大块；
新配置(6000)应为 2 个小块并行抽取。上传 → 生成 → 轮询到终态，输出统计。
"""
import json
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:9100"

# ~9.5k 字：若干条 × 多种条款形态（禁止/必含/金额/时限），保证可抽取性
_ARTICLES = [
    "第{n}条 招标人不得以不合理的条件限制或者排斥潜在投标人，不得对潜在投标人实行歧视待遇。",
    "第{n}条 投标人不得相互串通投标报价，不得排挤其他投标人的公平竞争，损害招标人或者其他投标人的合法权益。",
    "第{n}条 投标人不得以低于成本的报价竞标，也不得以他人名义投标或者以其他方式弄虚作假，骗取中标。",
    "第{n}条 招标文件要求中标人提交履约保证金的，中标人应当按照招标文件的要求提交履约保证金，履约保证金不得超过中标合同金额的百分之十。",
    "第{n}条 投标保证金不得超过招标项目估算价的百分之二，且最高不得超过八十万元人民币；投标截止后投标人撤销投标文件的，投标保证金不予退还。",
    "第{n}条 招标人应当在招标文件中载明投标有效期，投标有效期从提交投标文件的截止之日起算，不得少于二十日。",
    "第{n}条 中标人应当按照合同约定履行义务，完成中标项目；中标人不得向他人转让中标项目，也不得将中标项目肢解后分别向他人转让。",
    "第{n}条 评标委员会应当按照招标文件确定的评标标准和方法进行评审比较；设有标底的，应当参考标底，但不得作为评标的唯一依据。",
]


def build_law() -> str:
    parts = ["中华人民共和国招标投标法实施条例（模拟长文本，用于分块验证）", ""]
    n = 10
    total = 0
    idx = 0
    while total < 9500:
        a = _ARTICLES[idx % len(_ARTICLES)].format(n=n)
        parts.append(a)
        total += len(a) + 1
        n += 1
        idx += 1
    return "\n".join(parts)


def req(path, *, method="GET", data=None, headers=None, is_json=False, timeout=60):
    h = dict(headers or {})
    body = None
    if data is not None:
        if is_json:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
        else:
            body = data
    r = urllib.request.Request(BASE + path, data=body, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    law = build_law()
    print(f"law chars = {len(law)}")

    login = req("/api/auth/login", method="POST", data={"username": "admin", "password": "admin123"}, is_json=True)
    token = login["token"]
    H = {"X-Session-Token": token}

    boundary = uuid.uuid4().hex
    part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="模拟实施条例长文本.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n" + law + "\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="roles"\r\n\r\nlegal\r\n'
        f"--{boundary}--\r\n"
    ).encode()
    up = req("/api/files/upload", method="POST", data=part,
             headers={**H, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    f = (up.get("files") or [{}])[0]
    fid = f.get("file_id")
    print(f"UPLOAD OK file_id={fid} chars={f.get('char_count')}")
    if not fid:
        print("UPLOAD_RESP:", json.dumps(up)[:300])
        return 1

    # 预检：确认分块数
    pv = req("/api/legal-rules/preview", method="POST", data={"file_ids": [fid], "mode": "bid"}, headers=H, is_json=True)
    print(f"PREVIEW chunks={pv.get('chunks')} chunk_chars={pv.get('chunk_chars')} split={pv.get('split_mode')}")

    gen = req("/api/legal-rules/generate", method="POST",
              data={"file_ids": [fid], "name": "6000分块E2E验证", "mode": "bid", "reuse": False},
              headers=H, is_json=True)
    rid = gen["id"]
    print(f"GENERATE started id={rid}")

    t0 = time.time()
    last = None
    while time.time() - t0 < 900:
        time.sleep(10)
        d = req(f"/api/legal-rules/{rid}", headers=H)
        st = d.get("status")
        if st != last:
            stats = d.get("stats") or {}
            print(f"[{time.time()-t0:5.0f}s] status={st} chunks_done={stats.get('chunks_done')} raw={stats.get('raw_rules')}")
            last = st
        if st in ("ready", "done", "failed", "error"):
            stats = d.get("stats") or {}
            rules = d.get("rules") or []
            warns = d.get("warnings") or []
            print("---- 终态 ----")
            print(f"status={st} elapsed={time.time()-t0:.0f}s")
            print(f"stats={json.dumps(stats, ensure_ascii=False)}")
            print(f"rules={len(rules)} warnings={len(warns)}")
            for r_ in rules[:3]:
                print(" sample:", (r_.get("name") or "")[:50], "|", (r_.get("article_no") or ""))
            if warns:
                print("warnings:", warns[:3])
            return 0 if st in ("ready", "done") and rules else 2
    print("TIMEOUT polling")
    return 3


if __name__ == "__main__":
    sys.exit(main())
