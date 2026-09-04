#!/usr/bin/env python3
"""冒烟验证（2026-08-31 效率优化）：上传文档 -> N 条规则（含需法规依据的）-> KB 开启 ->
创建任务 -> 轮询计时。目标：确认 kb_timeout=150 + concurrency=10 + 持久化 KB 缓存下
每批耗时显著优于旧配置（45s 超时×重试≈90s+/批）。

用法：python smoke_verify_optimize.py [docx路径]
  - 默认用 backend/uploads/ 下的内存测试文件（findings_cache 可能命中，KB 不触发）
  - 传不同文档（如 tests/fixtures/招标文件.docx）→ 结论缓存 miss → 真正触发 KB 检索并填充 kb_cache
  - 再传第三个文档（如 tests/fixtures/报价明细表.docx）→ 结论缓存 miss 但 KB 缓存命中 → 验证 KB 秒回
"""
import json, time, subprocess, sys, os

BASE = "http://localhost:9100"
DOCX = sys.argv[1] if len(sys.argv) > 1 else r"D:/Private Documents/app-project/biddingfiles-Review/backend/uploads/3491dbb4c9514db28594c3728e8ca771.docx"
KB_ID = "ea8cc4e9-7e41-4db3-a3d7-62a701aeb427"
RULE_IDS = None  # 从 rs-f97d16c3 动态选取：16 条 need_legal_basis + 8 条普通


def curl(args, timeout=60):
    p = subprocess.run(["curl", "-s", "-m", str(timeout)] + args, capture_output=True, text=True)
    return p.stdout


# 1) 上传
print(">> upload", flush=True)
up = curl(["-F", f"files=@{DOCX}", f"{BASE}/api/files/upload"])
try:
    fid = json.loads(up)["files"][0]["file_id"]
except Exception as e:
    print("UPLOAD FAILED:", e, "| raw:", up[:300], flush=True)
    sys.exit(1)
print("file_id =", fid, flush=True)

# 2) 从 rs-f97d16c3 动态选取规则：16 条 need_legal_basis + 8 条普通
print(">> fetch rules", flush=True)
rules_raw = curl([f"{BASE}/api/rulesets/rs-f97d16c3"])


def find_ruleset(x):
    if isinstance(x, dict):
        if x.get("id") == "rs-f97d16c3":
            return x
        for k in ("rulesets", "items", "data", "ruleset"):
            if k in x:
                r = find_ruleset(x[k])
                if r:
                    return r
    if isinstance(x, list):
        for i in x:
            r = find_ruleset(i)
            if r:
                return r
    return None


rs = find_ruleset(json.loads(rules_raw))
if not rs:
    print("RULESET NOT FOUND, raw:", rules_raw[:300], flush=True)
    sys.exit(1)
need = [str(r["id"]) for r in rs["rules"] if r.get("need_legal_basis")]
plain = [str(r["id"]) for r in rs["rules"] if not r.get("need_legal_basis")]
RULE_IDS = (need + plain)[:24]
print("picked rules =", len(RULE_IDS), "(need_lb:", len(need[:16]), ")", flush=True)

# 3) 创建任务：KB 开启 + 指定 kb_id
print(">> create task (%d rules, kb enabled)" % len(RULE_IDS), flush=True)
payload = json.dumps({
    "file_ids": [fid],
    "ruleset_id": "rs-f97d16c3",
    "rule_ids": RULE_IDS,
    "kb_enabled": True,
    "kb_id": KB_ID,
})
task_raw = curl(["-X", "POST", f"{BASE}/api/review/tasks",
                 "-H", "Content-Type: application/json", "-d", payload], timeout=30)
try:
    tid = json.loads(task_raw).get("task_id")
except Exception:
    tid = None
if not tid:
    print("CREATE FAILED, raw:", task_raw[:300], flush=True)
    sys.exit(1)
print("task_id =", tid, flush=True)

# 3) 轮询（每 10s 打点进度；冷跑 KB 填充上限 40 分钟）
print(">> poll", flush=True)
start = time.time()
final = None
for i in range(240):  # 最多 40 分钟
    st = curl([f"{BASE}/api/review/tasks/{tid}"], timeout=20)
    try:
        d = json.loads(st)
    except Exception:
        d = {}
    status = d.get("status")
    el = int(time.time() - start)
    prog = d.get("progress", {})
    done = prog.get("done_rules") if isinstance(prog, dict) else None
    total = prog.get("total_rules") if isinstance(prog, dict) else None
    print(f"[{el}s] status={status} rules={done}/{total}", flush=True)
    if status in ("done", "completed", "failed", "error"):
        final = d
        break
    time.sleep(10)

elapsed = int(time.time() - start)
if final:
    kb = final.get("kb_traces") or []
    print("=== RESULT ===", flush=True)
    print("status    =", final.get("status"), flush=True)
    print("findings  =", len(final.get("findings", [])), flush=True)
    print("kb_traces =", len(kb), flush=True)
    print("wall_time =", f"{elapsed}s", flush=True)
    # 抽样 KB trace 耗时（若带时间戳）
    if kb and isinstance(kb[0], dict):
        print("kb sample keys:", list(kb[0].keys()), flush=True)
    # 超时/失败日志
    logs = final.get("logs") or []
    warn = [l for l in logs if isinstance(l, dict) and l.get("level") in ("warn", "error")]
    print("warn/error log lines:", len(warn), flush=True)
    for w in warn[:5]:
        print("   ", w, flush=True)
else:
    print(f"TIMEOUT after {elapsed}s — task did not finish", flush=True)
    sys.exit(2)
