"""OCR 客户端（支持两种服务）。

1) 图聆云（tuling，免鉴权，multipart 上传）
  POST {base}{path}  默认 /tuling/uocr/v2/recognize
  form: trackId, category=atlas.doc, picFile=@图片
  响应: {"state":{"code":0,"success":true}, "body":"<JSON字符串>"}
        body 解析后为 {"pages":[{"lines":[{"content","conf","coord"}],"width","height"}]}

2) 百度智能云 OCR（baidu，AK/SK 换 access_token，图片 base64 + form 表单）
  取 token: POST {base}/oauth/2.0/token
            body: grant_type=client_credentials&client_id=<API Key>&client_secret=<Secret Key>
            响应: {"access_token":"...", "expires_in":2592000}（30 天）
  识别:     POST {base}/rest/2.0/ocr/v1/general_basic?access_token=<token>
            Content-Type: application/x-www-form-urlencoded
            body: image=<urlencoded base64>
            响应: {"words_result":[{"words":"..."}], "words_result_num":N}
                  失败: {"error_code":17,"error_msg":"..."}

新增 provider 只需在此实现 _recognize_xxx 并登记到 _PROVIDERS。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
import uuid
from typing import Any

import httpx

from .. import config

logger = logging.getLogger(__name__)


class OCRError(RuntimeError):
    pass


# 百度图片限制：base64 编码后 ≤4M、最长边 ≤8192、最短边 ≥15
_BAIDU_MAX_B64 = 4 * 1024 * 1024
_BAIDU_MAX_SIDE = 4096  # 硬限 8192，保守取 4096 兼顾识别耗时
_BAIDU_MIN_SIDE = 15

# 内嵌探活兜底图（无 PyMuPDF 时使用；百度对无文字小图会返回 216630，故优先动态生成）
_FALLBACK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAADIAAAAWCAYAAAAQgLTMAAAAf0lEQVR4nO3XMQqAMAyF4b/"
    "gLTyF4hm8hVdwdhLBRRAcBEEQBEEQBEEQBEEQBEEQBEEQ5CV5hEAgH7wkTdMkTdM0TdM0"
    "TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0TdM0"
    "TdM0TdM0TdM0TdM0TdMcbwEDAAH/2m8kAAAAAElFTkSuQmCC"
)


def _fitz():
    """惰性获取 PyMuPDF（文档解析已依赖，缺失时不阻断 OCR 其他 provider）。"""
    try:
        import fitz  # PyMuPDF

        return fitz
    except Exception:  # noqa: BLE001
        return None


def probe_image_bytes() -> bytes:
    """生成一张带清晰文字的探活图。

    百度对无文字/过小/纯色图片统一返回 216630 recognize error，用历史那张
    50×22 的杂点小图探活必然失败。这里用 PyMuPDF 渲染白底黑字（内置中文字体，
    无需外部字体文件），内容含中文与英数，任一被识别即可判定链路可用。
    """
    fitz = _fitz()
    if fitz is None:
        return _FALLBACK_PNG
    try:
        doc = fitz.open()
        try:
            page = doc.new_page(width=440, height=170)
            page.insert_text(
                (36, 72), "OCR 连通性测试", fontname="china-s", fontsize=30, color=(0, 0, 0)
            )
            page.insert_text(
                (36, 124), "OCR TEST 12345", fontname="helv", fontsize=26, color=(0, 0, 0)
            )
            return page.get_pixmap(dpi=200).tobytes("png")
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 - 探活图生成失败不应阻断连通性检测
        logger.warning("生成探活图失败，回退内嵌图片: %s", exc)
        return _FALLBACK_PNG


def _fit_image(image_bytes: bytes) -> bytes:
    """把图片压到百度限制内（base64 ≤4M、长边 ≤4096），超限自动降采样/转 JPEG。

    PDF 扫描页按 dpi=200 渲染后常为 1~3MB，base64 膨胀 33% 会直接撞 4M 上限
    导致 216201/216630；扫描件多为黑白文字页，转 JPEG 可在不降分辨率的前提下
    大幅瘦身，仍超限再逐级降采样。
    """
    fitz = _fitz()
    if fitz is None:
        return image_bytes
    try:
        pix = fitz.Pixmap(image_bytes)
        longest = max(pix.width, pix.height)
        if longest > _BAIDU_MAX_SIDE:
            while (
                max(pix.width, pix.height) > _BAIDU_MAX_SIDE
                and min(pix.width, pix.height) // 2 >= _BAIDU_MIN_SIDE
            ):
                pix.shrink(1)  # 边长减半

        data = pix.tobytes("png")
        if len(data) * 4 // 3 <= _BAIDU_MAX_B64:
            return data

        # 体积仍超限：优先转 JPEG 保住分辨率
        try:
            jpg = pix.tobytes("jpg")
            if len(jpg) * 4 // 3 <= _BAIDU_MAX_B64:
                return jpg
        except Exception:  # noqa: BLE001
            pass

        # 最后手段：逐级降采样后重编码
        while (
            len(data) * 4 // 3 > _BAIDU_MAX_B64
            and min(pix.width, pix.height) // 2 >= _BAIDU_MIN_SIDE
        ):
            pix.shrink(1)
            data = pix.tobytes("png")
        return data
    except Exception as exc:  # noqa: BLE001 - 压缩失败时按原图提交，交由百度裁定
        logger.warning("OCR 图片预处理失败，按原图提交: %s", exc)
        return image_bytes


# access_token 缓存（百度）：{api_key: (token, 过期时间戳)}
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = asyncio.Lock()
# token 有效期到期前提前刷新的余量（秒），避免临界处用到刚过期的 token
_TOKEN_SKEW = 300


def provider() -> str:
    """当前 OCR 服务类型：tuling | baidu。"""
    return str(config.get("ocr_provider", "tuling") or "tuling").strip().lower()


def _join(base_key: str, path_key: str, default_path: str) -> str:
    base = str(config.get(base_key, "") or "").rstrip("/")
    path = str(config.get(path_key, default_path) or default_path)
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _url() -> str:
    return _join("ocr_base_url", "ocr_path", "/tuling/uocr/v2/recognize")


def _extract_text(body: Any) -> str:
    """从响应 body 中按行拼接文本，保留页间分隔。"""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OCRError(f"OCR 返回体解析失败: {exc}") from exc
    if not isinstance(body, dict):
        return ""

    # 百度形态：words_result: [{words: "..."}]
    words_result = body.get("words_result")
    if isinstance(words_result, list):
        lines = [
            str((item or {}).get("words") or "").strip()
            for item in words_result
            if isinstance(item, dict)
        ]
        return "\n".join(x for x in lines if x)

    pages_text: list[str] = []
    for page in (body.get("pages") or []):
        lines = [(ln.get("content") or "").strip() for ln in (page.get("lines") or [])]
        text = "\n".join(x for x in lines if x)
        if text:
            pages_text.append(text)
    return "\n\n".join(pages_text)


def _check_baidu_error(payload: dict[str, Any]) -> None:
    """百度失败时不在 HTTP 状态码上体现，需看 error_code 字段。"""
    if "error_code" not in payload:
        return
    code = payload.get("error_code")
    msg = payload.get("error_msg") or "未知错误"
    hint = {
        1: "未知错误",
        2: "服务暂不可用",
        3: "调用的 API 不存在",
        4: "集群超限额",
        6: "无权限使用该接口（该接口未开通或 AK 无权限）",
        13: "获取 token 失败（AK/SK 不正确或账号未实名）",
        14: "IAM 鉴权失败",
        15: "应用不存在或已被删除",
        17: "请求频率超限（QPS 超限）",
        18: "请求量超限（免费额度用尽）",
        19: "请求总量超限",
        110: "access_token 无效",
        111: "access_token 已过期",
        216015: "模块关闭",
        216100: "非法参数",
        216101: "参数数量不够",
        216201: "图片格式错误",
        216202: "图片大小超限（base64 后需 ≤4M）",
        216630: "识别错误（图片可能无文字、分辨率过低或格式不受支持）",
        216631: "识别银行卡错误",
        282810: "图片识别失败",
    }.get(int(code) if isinstance(code, int) or str(code).isdigit() else -1)
    raise OCRError(f"OCR 业务失败[{code}] {msg}" + (f"（{hint}）" if hint else ""))


async def _baidu_access_token(force: bool = False) -> str:
    """用 AK/SK 换取 access_token，带进程内缓存与并发保护。

    百度 token 有效期 30 天，缓存到过期前 5 分钟；AK 变化时自动换新的缓存条目；
    用 asyncio.Lock 防止并发首次调用时重复换取（节流并避免触发 QPS 限制）。
    """
    api_key = str(config.get("ocr_api_key", "") or "").strip()
    secret_key = str(config.get("ocr_secret_key", "") or "").strip()
    if not api_key or not secret_key:
        raise OCRError(
            "百度 OCR 未配置 API Key / Secret Key，请在「服务配置 → OCR 服务」中填写后保存"
        )

    now = time.time()
    if not force:
        cached = _token_cache.get(api_key)
        if cached and cached[1] > now:
            return cached[0]

    async with _token_lock:
        # 双检：等待锁期间可能已被其他协程刷新
        cached = _token_cache.get(api_key)
        if not force and cached and cached[1] > time.time():
            return cached[0]

        token_url = _join("ocr_base_url", "ocr_token_path", "/oauth/2.0/token")
        timeout = httpx.Timeout(float(config.get("ocr_timeout", 60)), connect=15.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as cli:
                resp = await cli.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": api_key,
                        "client_secret": secret_key,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            raise OCRError(f"获取 OCR access_token 失败: {exc}") from exc

        if resp.status_code >= 400:
            raise OCRError(
                f"获取 OCR access_token 返回 {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OCRError(f"access_token 响应非 JSON: {resp.text[:200]}") from exc

        if not payload.get("access_token"):
            _check_baidu_error(payload)
            raise OCRError(f"access_token 响应缺少 token: {str(payload)[:200]}")

        expires = float(payload.get("expires_in") or 2592000)
        _token_cache[api_key] = (str(payload["access_token"]), now + expires - _TOKEN_SKEW)
        return str(payload["access_token"])


async def _recognize_tuling(
    image_bytes: bytes, filename: str, client: httpx.AsyncClient
) -> str:
    """图聆云：multipart 上传图片。"""
    timeout = httpx.Timeout(float(config.get("ocr_timeout", 60)), connect=15.0)
    try:
        resp = await client.post(
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


async def _recognize_baidu(
    image_bytes: bytes, filename: str, client: httpx.AsyncClient
) -> str:
    """百度智能云 OCR：base64 图片 + form 表单，token 失效自动换取一次重试。"""
    timeout = httpx.Timeout(float(config.get("ocr_timeout", 60)), connect=15.0)

    async def call(token: str) -> httpx.Response:
        url = _url()
        sep = "&" if "?" in url else "?"
        # 提交前压缩到百度限制内（base64 ≤4M / 长边 ≤4096），避免大页面图被拒
        payload = _fit_image(image_bytes)
        return await client.post(
            f"{url}{sep}access_token={urllib.parse.quote(token)}",
            data={
                "image": base64.b64encode(payload).decode("ascii"),
                # 可选参数：通用场景不做方向检测/语言自动判定，减少误判
                "detect_direction": "false",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )

    token = await _baidu_access_token()
    try:
        resp = await call(token)
        # token 失效/被回收（110/111）→ 强制换取新 token 重试一次
        if resp.status_code < 400:
            try:
                if (resp.json() or {}).get("error_code") in (110, 111, "110", "111"):
                    token = await _baidu_access_token(force=True)
                    resp = await call(token)
            except ValueError:
                pass
    except httpx.HTTPError as exc:
        raise OCRError(f"OCR 请求失败: {exc}") from exc

    if resp.status_code >= 400:
        raise OCRError(f"OCR 返回 {resp.status_code}: {resp.text[:200]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise OCRError(f"OCR 响应非 JSON: {resp.text[:200]}") from exc

    _check_baidu_error(payload)
    return _extract_text(payload)


_PROVIDERS = {
    "tuling": _recognize_tuling,
    "baidu": _recognize_baidu,
}


async def recognize(
    image_bytes: bytes,
    *,
    filename: str = "page.png",
    client: httpx.AsyncClient | None = None,
) -> str:
    """识别单张图片，失败抛 OCRError。按 ocr_provider 分发到具体实现。"""
    prov = provider()
    impl = _PROVIDERS.get(prov)
    if impl is None:
        raise OCRError(
            f"不支持的 OCR 服务类型: {prov}（可选 {', '.join(sorted(_PROVIDERS))}）"
        )
    own = client is None
    if own:
        timeout = httpx.Timeout(float(config.get("ocr_timeout", 60)), connect=15.0)
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        return await impl(image_bytes, filename, client)
    finally:
        if own:
            await client.aclose()


def invalidate_token() -> None:
    """清空 access_token 缓存（更换 AK/SK 或测试失败排查时调用）。"""
    _token_cache.clear()


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
    """用一张带清晰文字的探活图检测连通性（百度对无文字小图会返回 216630）。"""
    png = probe_image_bytes()
    prov = provider()
    if prov == "baidu":
        # 百度先用 AK/SK 换取 token 校验鉴权，再实际识别一次，两段错误分开提示
        try:
            await _baidu_access_token(force=True)
        except OCRError as exc:
            return {"ok": False, "message": f"鉴权失败：{exc}"}
    try:
        text = await recognize(png, filename="probe.png")
        label = {"tuling": "图聆云", "baidu": "百度智能云"}.get(prov, prov)
        return {
            "ok": True,
            "message": f"{label} OCR 连接正常",
            "detail": {"sample": text[:80], "provider": prov},
        }
    except OCRError as exc:
        msg = str(exc)
        # 图聆云探活图无实际文字时会返回业务失败，但服务本身可达
        if prov == "tuling" and "业务失败" in msg:
            return {"ok": True, "message": f"服务可达（{msg[:80]}）"}
        return {"ok": False, "message": msg}
