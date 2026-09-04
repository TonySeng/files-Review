#!/usr/bin/env python3
"""端到端真实验证：上传文档 -> 117 规则集 -> 创建任务 -> 轮询完成并计时。"""
import json, time, subprocess, sys

BASE = "http://localhost:9100"
DOCX = r"D:/Private Documents/app-project/biddingfiles-Review/backend/uploads/3491dbb4c9514db28594c3728e8ca771.docx"
PY = sys.executable


def curl(args, timeout=60):
    p = subprocess.run(
        ["curl", "-s", "-m", str(timeout)] + args,
        capture_output=True, text=True,
    )
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

# 2) 拉取规则集 rs-f97d16c3 的全部规则 id
print(">> fetch rules", flush=True)
rules_raw = curl([f"{BASE}/api/rulesets/rs-f97d16c3"])


def find_ruleset(x):
    if isinstance(x, dict):
        if x.get("id") == "rs-f97d16c3":
            return x
        for k in ("rulesets", "items", "data"):
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


try:
    rs = find_ruleset(json.loads(rules_raw))
    rids = [str(r["id"]) for r in rs["rules"]]
except Exception as e:
    print("RULES FAILED:", e, "| raw:", rules_raw[:300], flush=True)
    sys.exit(1)
print("rule_count =", len(rids), flush=True)

# 3) 创建任务 (KB 关闭，隔离 LLM 吞吐变量)
print(">> create task (117 rules, kb disabled)", flush=True)
payload = json.dumps({
    "file_ids": [fid],
    "ruleset_id": "rs-f97d16c3",
    "rule_ids": rids,
    "kb_enabled": False,
})
task_raw = curl(
    ["-X", "POST", f"{BASE}/api/review/tasks",
     "-H", "Content-Type: application/json", "-d", payload],
    timeout=30,
)
try:
    tid = json.loads(task_raw).get("task_id")
except Exception as e:
    print("CREATE FAILED:", e, "| raw:", task_raw[:300], flush=True)
    sys.exit(1)
print("task_id =", tid, flush=True)
if not tid:
    print("NO TASK ID, raw:", task_raw[:300], flush=True)
    sys.exit(1)

# 4) 轮询
print(">> poll", flush=True)
start = time.time()
final = None
for i in range(90):  # 最多 15 分钟
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
    print("=== RESULT ===", flush=True)
    print("status    =", final.get("status"), flush=True)
    print("findings  =", len(final.get("findings", [])), flush=True)
    print("elapsed   =", final.get("elapsed"), flush=True)
    print("wall_time =", f"{elapsed}s", flush=True)
else:
    print(f"TIMEOUT after {elapsed}s — task did not finish", flush=True)
    sys.exit(2)
