"""模拟验证：batch_timeout 是否只计「实际执行时间」而不计排队等待。

复现 2026-08-31 任务 17015e224166 的「94 批同一秒排队超时」根因：
旧实现 wait_for 包住 sem 获取 → 排队时间计入超时预算，未执行的批次被误杀；
新实现先获取 sem 再 wait_for → 900s 只计实际执行时间。
"""
import asyncio


async def run_test(sem_outside: bool):
    sem = asyncio.Semaphore(2)   # 并发 2（对应 concurrency）
    BATCH_TIMEOUT = 5.0          # 每批超时（对应 batch_timeout=900s）
    WORK = 1.0                   # 每批实际执行时长（模拟 LLM 调用）
    N = 10                       # 批次数：并发 2 → 靠后批次需排队 ~4s

    async def process_batch():
        await asyncio.sleep(WORK)

    async def _with_sem():
        async with sem:
            await process_batch()

    async def safe_batch_old():  # 旧实现：wait_for 包住 sem 获取
        try:
            await asyncio.wait_for(_with_sem(), timeout=BATCH_TIMEOUT)
            return "OK"
        except asyncio.TimeoutError:
            return "TIMEOUT(排队中被误杀)"

    async def safe_batch_new():  # 新实现：先 sem 再 wait_for
        async with sem:
            try:
                await asyncio.wait_for(process_batch(), timeout=BATCH_TIMEOUT)
                return "OK"
            except asyncio.TimeoutError:
                return "TIMEOUT(执行中超时)"

    tasks = [safe_batch_new() if sem_outside else safe_batch_old() for _ in range(N)]
    return await asyncio.gather(*tasks)


if __name__ == "__main__":
    for label, mode in [("旧实现(wait_for 包住 sem，排队计时)", False),
                        ("新实现(先 sem 后 wait_for，仅执行计时)", True)]:
        res = asyncio.run(run_test(mode))
        killed = sum(1 for r in res if r != "OK")
        print(f"{label}: 完成 {len(res) - killed}/{len(res)} 批，被超时杀掉 {killed} 批")
