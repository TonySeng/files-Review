import sys, time, asyncio
sys.path.insert(0, '/app/backend')
import httpx
from backend import config

key = config.get('llm_api_key')
base = (config.get('llm_base_url') or '').rstrip('/')
base = base if base.endswith('/v1') else base + '/v1'
model = config.get('llm_model')
url = base + '/chat/completions'
headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'}
tools = [{"type": "function", "function": {"name": "get_status", "description": "返回状态",
         "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}}]

async def one(i, client):
    payload = {'model': model,
               'messages': [{'role': 'user', 'content': '请调用 get_status 工具查询 x=hello 的状态，并给出JSON结论'}],
               'tools': tools, 'tool_choice': 'auto', 'max_tokens': 300, 'stream': False}
    t = time.time()
    try:
        r = await client.post(url, json=payload, headers=headers)
        dt = round(time.time() - t, 2)
        if r.status_code == 200:
            msg = r.json().get('choices', [{}])[0].get('message', {})
            return (i, r.status_code, dt, 'tool_calls' if msg.get('tool_calls') else 'no_tool')
        return (i, r.status_code, dt, r.text[:50])
    except Exception as e:
        return (i, 'ERR', round(time.time() - t, 2), str(e)[:50])

async def run():
    for N in [2, 4, 6]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            t0 = time.time()
            res = await asyncio.gather(*[one(i, client) for i in range(N)])
            print(f'N={N} wall={round(time.time() - t0, 2)}s -> {sorted(res)}', flush=True)

asyncio.run(run())
