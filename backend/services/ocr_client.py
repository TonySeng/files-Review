"""OCR 客户端（图聆云 图文识别）。

实测契约（multipart 上传）：
  POST /tuling/uocr/v2/recognize
  form: trackId, category=atlas.doc, picFile=@图片
  响应: {"state":{"code":0,"success":true}, "body":"<JSON字符串>"}
        body 解析后为 {"pages":[{"lines":[{"content","conf","coord"}],"width","height"}]}
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any

import httpx

from .. import config

logger = logging.getLogger(__name__)


class OCRError(RuntimeError):
    pass


def _url() -> str:
    base = config.get("ocr_base_url", "").rstrip("/")
    path = config.get("ocr_path", "/tuling/uocr/v2/recognize")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _extract_text(body: Any) -> str:
    """从响应 body 中按行拼接文本，保留页间分隔。"""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OCRError(f"OCR 返回体解析失败: {exc}") from exc
    if not isinstance(body, dict):
        return ""

    pages_text: list[str] = []
    for page in (body.get("pages") or []):
        lines = [(ln.get("content") or "").strip() for ln in (page.get("lines") or [])]
        text = "\n".join(x for x in lines if x)
        if text:
            pages_text.append(text)
    return "\n\n".join(pages_text)


async def recognize(
    image_bytes: bytes,
    *,
    filename: str = "page.png",
    client: httpx.AsyncClient | None = None,
) -> str:
    """识别单张图片，失败抛 OCRError。"""
    timeout = httpx.Timeout(float(config.get("ocr_timeout", 60)), connect=15.0)
    own = client is None
    cli = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        resp = await cli.post(
            _url(),
            data={
                "trackId": uuid.uuid4().hex[:16],
                "category": config.get("ocr_category", "atlas.doc"),
            },
            files={"picFile": (filename, image_bytes, "image/png")},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OCRError(f"OCR 请求失败: {exc}") from exc
    finally:
        if own:
            await cli.aclose()

    if resp.status_code >= 400:
        raise OCRError(f"OCR 返回 {resp.status_code}: {resp.text[:200]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise OCRError(f"OCR 响应非 JSON: {resp.text[:200]}") from exc

    state = payload.get("state") or {}
    if state and not (state.get("success") or state.get("code") in (0, "0")):
        raise OCRError(f"OCR 业务失败: {state}")

    # 兼容 body 包裹与直接返回 data/pages 两种形态
    body = payload.get("body")
    if body is None:
        body = payload.get("data") or payload
    return _extract_text(body)


async def recognize_many(images: list[bytes]) -> list[str]:
    """顺序识别多张图，单页失败不中断整体流程。"""
    timeout = httpx.Timeout(float(config.get("ocr_timeout", 60)), connect=15.0)
    out: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        for idx, img in enumerate(images):
            try:
                out.append(await recognize(img, filename=f"p{idx}.png", client=client))
            except OCRError as exc:
                logger.warning("OCR 第 %d 页失败: %s", idx + 1, exc)
                out.append("")
    return out


async def test_connection() -> dict[str, Any]:
    """用一张带文字的小图探活。"""
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAADIAAAAWCAYAAAAQgLTMAAAAf0lEQVR4nO3XMQqAMAyF4b/"
        "gLTyF4hm8hVdwdhLBRRAcBEEQBEEQBEEQBEEQBEEQBEEQBEEQ5CV5hEAgH7wkTdMkTdM0TdM0"
        "TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0"
        "TdM0TdM0TdM0TdM0TdMcbwEDAAH/2m8kAAAAAElFTkSuQmCC"
    )
    try:
        text = await recognize(png, filename="probe.png")
        return {"ok": True, "message": "连接正常", "detail": {"sample": text[:80]}}
    except OCRError as exc:
        msg = str(exc)
        if "业务失败" in msg:
            return {"ok": True, "message": f"服务可达（{msg[:80]}）"}
        return {"ok": False, "message": msg}
