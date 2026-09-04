"""监控审核任务直到终态，并打印最终结论。用法: python monitor_final.py <task_id> [max_minutes]"""
import sys, time, json, urllib.request

TASK_ID = sys.argv[1] if len(sys.argv) > 1 else "3aea76b75d65"
MAX_MIN = float(sys.argv[2]) if len(sys.argv) > 2 else 45
BASE = "http://localhost:9100/api/review/tasks/" + TASK_ID

def get():
    try:
        with urllib.request.urlopen(BASE, timeout=10) as r:
            return json.load(r)
    except Exception as e:
        return {"_err": str(e)}

start = time.time()
last_progress = None
print(f"[monitor] task={TASK_ID} 上限={MAX_MIN}min 开始={time.strftime('%H:%M:%S')}")
while True:
    d = get()
    if "_err" in d:
        print(f"[monitor] 拉取失败: {d['_err']}")
    else:
        st = d.get("status")
        pr = d.get("progress")
        fc = d.get("findings_count")
        msg = d.get("progress_message")
        logs = d.get("logs") or []
        last = logs[-1].get("text") if logs else None
        if pr != last_progress:
            print(f"  {time.strftime('%H:%M:%S')} status={st} progress={pr} findings={fc} msg={msg} | last_log={last}")
            last_progress = pr
        if st in ("completed", "failed", "cancelled"):
            print(f"\n[monitor] === 任务终态: {st} (耗时 {int(time.time()-start)}s) ===")
            print(f"  progress={pr} findings={fc} error={d.get('error')}")
            fs = d.get("findings") or []
            from collections import Counter
            c = Counter(f.get("status") for f in fs)
            print(f"  findings status 分布: {dict(c)}")
            print(f"  consistency_issues: {len(d.get('consistency_issues') or [])}")
            summ = d.get("summary") or {}
            print(f"  score: {summ.get('score')}")
            print(f"  progress_message: {msg}")
            break
    if (time.time() - start) > MAX_MIN * 60:
        print(f"\n[monitor] 超出上限 {MAX_MIN}min，停止监控（任务可能仍在跑，请手动查询）")
        break
    time.sleep(30)
