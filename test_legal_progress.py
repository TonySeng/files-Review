# -*- coding: utf-8 -*-
"""验证法规解析的「实时过程日志」：与前端轮询同一视角。

上传法规 → 生成 → 每 2s 轮询一次（等同前端 1s 轮询的采样），
增量打印新出现的日志行与进度，校验：
  1) 启动后 10s 内就有过程日志（不再是干等一个百分比）
  2) progress 单调不回退
  3) 有「第 X/N 块开始抽取」与「第 X/N 块完成」的块级日志
  4) 终态日志以「生成完成 / 生成失败」收尾，stats 含耗时
"""
import json
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:9100"
POLL = 2.0

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
    parts = ["中华人民共和国招标投标法实施条例（进度验证用模拟文本）", ""]
    n, total, idx = 10, 0, 0
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
    token = req("/api/auth/login", method="POST",
                data={"username": "admin", "password": "admin123"}, is_json=True)["token"]
    H = {"X-Session-Token": token}

    boundary = uuid.uuid4().hex
    part = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="模拟实施条例_进度验证.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n" + law + "\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="roles"\r\n\r\nlegal\r\n'
        f"--{boundary}--\r\n"
    ).encode()
    up = req("/api/files/upload", method="POST", data=part,
             headers={**H, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    fid = (up.get("files") or [{}])[0].get("file_id")
    if not fid:
        print("UPLOAD FAILED")
        return 1
    print(f"UPLOAD OK chars={len(law)}")

    gen = req("/api/legal-rules/generate", method="POST",
              data={"file_ids": [fid], "name": "解析进度验证", "mode": "bid", "reuse": False},
              headers=H, is_json=True)
    rid = gen["id"]
    print(f"GENERATE id={rid}\n--- 实时过程（每 {POLL}s 采样，等同前端视角）---")

    t0 = time.time()
    seen = 0
    last_prog = -1.0
    monotonic = True
    first_log_at = None
    started_seen: set[str] = set()
    done_seen: set[str] = set()
    final = None

    while time.time() - t0 < 900:
        time.sleep(POLL)
        d = req(f"/api/legal-rules/{rid}", headers=H)
        logs = d.get("logs") or []
        if logs and first_log_at is None:
            first_log_at = time.time() - t0

        # 增量打印新日志行
        for entry in logs[seen:]:
            print(f"  [{time.time()-t0:5.0f}s] {entry['time']} {entry['text']}")
            if "块开始抽取" in entry["text"]:
                started_seen.add(entry["text"].split("块")[0])
            if "块完成" in entry["text"] or "块未抽取到" in entry["text"] or "块抽取失败" in entry["text"]:
                done_seen.add(entry["text"].split("块")[0])
        seen = len(logs)

        prog = float(d.get("progress") or 0)
        if prog < last_prog - 0.01:
            monotonic = False
            print(f"  !! 进度回退 {last_prog} -> {prog}")
        last_prog = max(last_prog, prog)

        if d.get("status") in ("ready", "failed"):
            final = d
            break

    print("--- 终态 ---")
    if not final:
        print("TIMEOUT")
        return 3
    stats = final.get("stats") or {}
    logs = final.get("logs") or []
    print(f"status={final['status']} rules={len(final.get('rules') or [])} "
          f"elapsed={stats.get('elapsed_sec')}s logs={len(logs)}")
    print(f"chunks={stats.get('chunks')} started={stats.get('chunks_started')} done={stats.get('chunks_done')}")

    checks = [
        ("启动 12s 内出现过程日志", first_log_at is not None and first_log_at <= 12),
        ("进度单调不回退", monotonic),
        ("块级开始日志", len(started_seen) >= 1),
        ("块级完成日志", len(done_seen) >= 1),
        ("块开始/完成数量一致", len(started_seen) == len(done_seen)),
        ("终态日志收尾", bool(logs) and ("生成完成" in logs[-1]["text"] or "生成失败" in logs[-1]["text"])),
        ("stats 含耗时", bool(stats.get("elapsed_sec"))),
        ("终态为 ready 且有规则", final["status"] == "ready" and bool(final.get("rules"))),
    ]
    ok = True
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
