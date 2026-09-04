# -*- coding: utf-8 -*-
"""真实 LLM 联调：验证新挖掘提示词下模型是否输出 structured_hint。

链路：登录(:9101) → 上传《招标投标法》精选条款(role=legal) → 生成 → 轮询 →
检查产出规则的 structured_hint 数量与形态。千问 80B 单块约 137s，脚本总耗时 ~3min。
"""
import json
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:9101"

LAW = """
中华人民共和国招标投标法（节选）

第二十六条 招标保证金不得超过招标项目估算价的百分之二，且最高不得超过八十万元人民币。
第三十二条 投标人不得相互串通投标报价，不得排挤其他投标人的公平竞争，损害招标人或者其他投标人的合法权益。
第三十三条 投标人不得以低于成本的报价竞标，也不得以他人名义投标或者以其他方式弄虚作假，骗取中标。
第四十六条 招标文件要求中标人提交履约保证金的，中标人应当按照招标文件的要求提交履约保证金。
"""


def req(path, *, method="GET", data=None, headers=None, is_json=False):
    url = BASE + path
    h = dict(headers or {})
    body = None
    if data is not None:
        if is_json:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
        else:
            body = data
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())


# 1) 登录
login = req("/api/auth/login", method="POST", data={"username": "admin", "password": "admin123"}, is_json=True)
token = login.get("token") or (login.get("data") or {}).get("token")
if not token:
    print("LOGIN_RESP:", json.dumps(login)[:400])
    sys.exit(1)
H = {"X-Session-Token": token}
print("LOGIN OK")

# 2) 上传法规文件（multipart）
boundary = uuid.uuid4().hex
part = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="files"; filename="招标投标法节选.txt"\r\n'
    "Content-Type: text/plain\r\n\r\n"
    + LAW + "\r\n"
    f"--{boundary}\r\n"
    'Content-Disposition: form-data; name="roles"\r\n\r\nlegal\r\n'
    f"--{boundary}--\r\n"
).encode()
up = req(
    "/api/files/upload",
    method="POST",
    data=part,
    headers={**H, "Content-Type": f"multipart/form-data; boundary={boundary}"},
)
files = up.get("files") or []
fid = files[0]["file_id"]
print(f"UPLOAD OK file_id={fid} chars={files[0].get('char_count')}")

# 3) 生成（禁止复用旧指纹结果，确保走新提示词）
gen = req(
    "/api/legal-rules/generate",
    method="POST",
    data={"file_ids": [fid], "name": "structured_hint 真实联调", "mode": "bid", "reuse": False},
    headers=H,
    is_json=True,
)
rid = gen["id"]
print(f"GENERATE started id={rid}")

# 4) 轮询
t0 = time.time()
while True:
    time.sleep(5)
    d = req(f"/api/legal-rules/{rid}", headers=H)
    st = d.get("status")
    el = int(time.time() - t0)
    print(f"  [{el}s] status={st} progress={d.get('progress')}")
    if st in ("ready", "failed"):
        break
    if el > 600:
        print("TIMEOUT")
        sys.exit(1)

print("\n==== RESULT ====")
print("status:", d.get("status"))
print("version:", d.get("version"))
print("stats:", json.dumps(d.get("stats"), ensure_ascii=False))
hints = 0
for r in d.get("rules") or []:
    hint = r.get("structured_hint")
    print(f"- [{r.get('severity')}/{r.get('category')}] {r.get('name')}")
    print(f"  hint: {json.dumps(hint, ensure_ascii=False) if hint else '(无)'}")
    if hint:
        hints += 1
print(f"\nhint_rules={hints}/{len(d.get('rules') or [])}")
if d.get("status") != "ready":
    sys.exit(1)
