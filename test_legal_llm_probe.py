"""联调前置探测：检查本地 LLM 配置是否就绪、能否真实调用。"""
import asyncio
import sys

sys.path.insert(0, ".")

from backend import config  # noqa: E402

k = config.get("llm_api_key") or ""
print("llm_base_url :", config.get("llm_base_url"))
print("llm_model    :", config.get("llm_model"))
print("llm_api_key  :", (k[:6] + "..." if k else "<EMPTY>"))


async def probe():
    try:
        from backend.services import llm_client

        r = await asyncio.wait_for(
            llm_client.chat(
                [{"role": "user", "content": "只回复一个字：好"}],
                temperature=0,
            ),
            timeout=40,
        )
        print("PROBE_OK:", str(r)[:300])
    except Exception as e:  # noqa: BLE001
        print("PROBE_FAIL:", type(e).__name__, str(e)[:300])


asyncio.run(probe())
