#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""讯飞 OCR webapi 本地鉴权探针（仅依赖标准库，无需安装任何包）。

用途：不经过本工具的 UI/容器，直接用与后端 ocr_client._xfyun_signed_url
**完全相同**的签名逻辑直连讯飞，一句话判断 AppID/APIKey/APISecret 是否配对正确。

用法：
  python xfyun_ocr_probe.py --app-id <APPID> --api-key <APIKey> --api-secret <APISecret>
  python xfyun_ocr_probe.py --app-id ... --api-key ... --api-secret ... --path hh_ocr_recognize_doc
  python xfyun_ocr_probe.py ... --image 一张真实图片.png   # 想验证真实识别效果时

诊断含义：
  - 401 + "apikey not found"      -> APIKey 无效/已失效（控制台重置过密钥，旧 key 立即失效）
  - 401 + "HMAC signature" / sig  -> APIKey 已被识别，但 APISecret 不配对（与 APIKey 非同源，
                                      或把星火 LLM 的 APIPassword 误填进 OCR 字段）
  - 200 + header.code != 0        -> 鉴权通过，但业务未开通/流控（如 11201），属账号侧开通问题
  - 200 + 含文本                  -> 完全正常

注意：讯飞 OCR webapi 的 APIKey+APISecret 与星火 LLM 的 APIPassword 是**两套不同凭证**，
本探针专验 OCR webapi 这一套。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import urllib.parse
from email.utils import formatdate

_XF_BASE = "https://api.xf-yun.com"

# 一张极小的有效 PNG（白底），仅供鉴权探针；想验真实识别请传 --image。
_FALLBACK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAADIAAAAWCAYAAAAQgLTMAAAAf0lEQVR4nO3XMQqAMAyF4b/"
    "gLTyF4hm8hVdwdhLBRRAcBEEQBEEQBEEQBEEQBEEQBEEQ5CV5hEAgH7wkTdMkTdM0TdM0"
    "TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0"
    "TdM0TdM0TdM0TdM0TdMcbwEDAAH/2m8kAAAAAElFTkSuQmCC"
)


def sign(url: str, api_key: str, api_secret: str) -> str:
    """与 backend/services/ocr_client.py 的 _xfyun_signed_url 逐字节一致的签名。"""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    date = formatdate(usegmt=True)
    sig_origin = f"host: {host}\ndate: {date}\nPOST {path} HTTP/1.1"
    sig = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), sig_origin.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    auth_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{sig}"'
    )
    auth = base64.b64encode(auth_origin.encode("utf-8")).decode("utf-8")
    return f"{url}?" + urllib.parse.urlencode({"host": host, "date": date, "authorization": auth})


def build_body(service: str, app_id: str, encoding: str, b64: str) -> dict:
    body: dict = {"header": {"app_id": app_id, "status": 3}}
    if service == "hh_ocr_recognize_doc":
        body["parameter"] = {
            service: {"recognizeDocumentRes": {"encoding": "utf8", "compress": "raw", "format": "json"}}
        }
        body["payload"] = {"image": {"encoding": encoding, "image": b64, "status": 3}}
    else:
        body["parameter"] = {
            service: {
                "category": "ch_en_public_cloud",
                "result": {"encoding": "utf8", "compress": "raw", "format": "json"},
            }
        }
        body["payload"] = {f"{service}_data_1": {"encoding": encoding, "image": b64, "status": 3}}
    return body


def diagnose(status: int, raw: str) -> str:
    rl = raw.lower()
    if status == 401 and "apikey not found" in rl:
        return "诊断: APIKey 无效/已失效（控制台可能重置过密钥，旧 key 立即失效）"
    if status == 401 and ("hmac" in rl or "signature" in rl):
        return ("诊断: APIKey 已被识别，但 APISecret 不配对！多半 APIKey/APISecret 非同源，"
                "或把星火 LLM 的 APIPassword 误填进了 OCR 字段")
    if status == 403:
        return "诊断: 服务器时钟偏差超过 300s，请校准本机系统时间"
    if status == 200:
        try:
            j = json.loads(raw)
        except ValueError:
            return "诊断: HTTP 200 但响应非预期 JSON"
        code = (j.get("header") or {}).get("code")
        if code in (0, "0"):
            return "诊断: 鉴权通过，OCR 正常返回（密钥配对正确）"
        return (f"诊断: 鉴权通过，但业务码 {code}："
                f"{(j.get('header') or {}).get('message')}（通常是该 OCR 能力未开通/流控，属账号侧开通问题）")
    return "诊断: 其他 HTTP 状态，见上方 BODY"


def main() -> None:
    ap = argparse.ArgumentParser(description="讯飞 OCR webapi 本地鉴权探针")
    ap.add_argument("--app-id", required=True, help="讯飞应用 APPID")
    ap.add_argument("--api-key", required=True, help="讯飞 WebAPI APIKey（注意不是星火 APIPassword）")
    ap.add_argument("--api-secret", required=True, help="讯飞 WebAPI APISecret")
    ap.add_argument("--path", default="sf8e6aca1",
                    choices=["sf8e6aca1", "hh_ocr_recognize_doc"])
    ap.add_argument("--image", help="可选：真实图片路径（png/jpg），不传则用内置极简 PNG")
    ap.add_argument("--base", default=_XF_BASE)
    args = ap.parse_args()

    if args.image:
        with open(args.image, "rb") as f:
            data = f.read()
    else:
        data = _FALLBACK_PNG
    enc = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
    b64 = base64.b64encode(data).decode("ascii")

    url = args.base.rstrip("/") + "/v1/private/" + args.path
    signed = sign(url, args.api_key, args.api_secret)
    body = build_body(args.path, args.app_id, enc, b64)

    parsed = urllib.parse.urlparse(signed)
    path_q = parsed.path + (("?" + parsed.query) if parsed.query else "")
    try:
        conn = http.client.HTTPSConnection(parsed.netloc, timeout=30)
        conn.request(
            "POST", path_q,
            body=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "host": parsed.netloc},
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
    except OSError as exc:
        print(f"连接失败（无法直连讯飞，请检查本机网络/代理）：{exc}")
        return

    print(f"HTTP {resp.status}")
    print("BODY:", raw[:600])
    print(diagnose(resp.status, raw))


if __name__ == "__main__":
    main()
