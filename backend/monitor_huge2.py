"""健壮监控：盯住一个审核任务直到终态或判定卡死。
- 每 15s 轮询一次
- 终态(completed/failed/cancelled/error) -> 打印最终统计并退出 0
- 连续 >15 分钟无进度推进 -> 判定卡死并打印卡点规则，退出 1
用法：python monitor_huge2.py <task_id>
"""
import json, sys, time, subprocess

BASE = "http://localhost:9100/api"
TID = sys.argv[1] if len(sys.argv) > 1 else "c338d79e8983"
HANG_LIMIT = 15 * 60  # 15 分钟无进展视为卡死


def curl(args):
    return subprocess.run(["curl", "-s", "-m", "30"] + args,
                          capture_output=True, text=True).stdout


def main():
    t0 = time.time()
    last_progress = -1
    last_change = time.time()
    last_tail = ""
    print(f"[monitor] tid={TID} start={time.strftime('%H:%M:%S')}")
    while True:
        raw = curl([f"{BASE}/review/tasks/{TID}"])
        try:
            d = json.loads(raw)
        except Exception:
            time.sleep(10)
            continue
        st = d.get("status")
        prog = d.get("progress")
        pm = d.get("progress_message") or ""
        logs = d.get("logs") or []
        tail = logs[-1].get("text") or "" if logs else ""
        if tail != last_tail:
            print(f"  [{time.time()-t0:6.0f}s][{st} p{prog}] {tail[:96]}")
            last_tail = tail
        # 进度有推进 -> 重置卡死计时
        if prog != last_progress:
            last_progress = prog
            last_change = time.time()
        if st in ("completed", "failed", "cancelled", "error"):
            el = time.time() - t0
            fc = d.get("findings") or []
            from collections import Counter
            dist = Counter(f.get("status") for f in fc)
            print(f"\n=== 终态: status={st} 用时={el:.0f}s findings={d.get('findings_count')} ===")
            print("findings 状态分布:", dict(dist))
            print("error:", d.get("error"))
            for f in fc[:6]:
                print(f"  - [{f.get('status')}] {f.get('rule_id')}: {f.get('title','')[:70]}")
            sys.exit(0)
        if time.time() - last_change > HANG_LIMIT:
            print(f"\n!!! HANG DETECTED at p{prog} rule='{pm}' (无进展 {time.time()-last_change:.0f}s) !!!")
            sys.exit(1)
        time.sleep(15)


if __name__ == "__main__":
    main()
