"""监控超大文档任务 dba32a807da9：打印阶段日志与最终结果到 /tmp/huge.log。"""
import subprocess, json, time, sys

TID = "dba32a807da9"
BASE = "http://localhost:9100/api"

def curl(args):
    return subprocess.run(["curl", "-s", "-m", "120"] + args, capture_output=True, text=True).stdout

def main():
    t0 = time.time()
    last = ""
    with open("/tmp/huge.log", "w", encoding="utf-8") as log:
        def say(s):
            print(s, flush=True); log.write(s + "\n")
        say(f"[{time.time()-t0:5.0f}s] monitor start, tid={TID}")
        while True:
            d = json.loads(curl([f"{BASE}/review/tasks/{TID}"]))
            st = d.get("status"); prog = d.get("progress")
            logs = d.get("logs") or []
            # 打印新出现的日志行（重点看 doc_summarize / 各规则 / 是否还 ReadTimeout）
            new = [e for e in logs if (e.get("text") or e.get("message") or "") != last]
            for e in logs:
                t = (e.get("text") or e.get("message") or "")
                if t != last:
                    say(f"  [{time.time()-t0:5.0f}s][{st}/p{prog}] {t[:110]}")
                    last = t
            if st in ("completed", "failed", "cancelled", "error"):
                fc = d.get("findings") or []
                from collections import Counter
                say(f"\n=== 完成: status={st} 用时={time.time()-t0:.0f}s findings={len(fc)} "
                    f"分布={dict(Counter(f.get('status') for f in fc))} ===")
                say(f"error={d.get('error')}")
                break
            if time.time() - t0 > 1500:
                say("TIMEOUT 25min"); break
            time.sleep(10)

if __name__ == "__main__":
    main()
