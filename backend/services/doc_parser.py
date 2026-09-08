"""文档解析：PDF / Word / Excel / 图片 / 纯文本，扫描件自动回退 OCR。"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

from . import ocr_client

logger = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".md",
             ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}

# 每页字符数低于此值判定为扫描页，触发 OCR
SCAN_PAGE_THRESHOLD = 40
MAX_OCR_PAGES = 30


class ParseError(RuntimeError):
    pass


async def parse(path: Path, ext: str) -> dict[str, Any]:
    """返回 {text, page_count, used_ocr}。"""
    ext = ext.lower()
    if ext == ".pdf":
        return await _parse_pdf(path)
    if ext in (".docx", ".doc"):
        return _parse_docx(path)
    if ext in (".xlsx", ".xls"):
        return _parse_excel(path)
    if ext in IMAGE_EXT:
        text = await ocr_client.recognize(path.read_bytes())
        return {"text": text, "page_count": 1, "used_ocr": True}
    if ext in (".txt", ".md"):
        return {
            "text": path.read_text(encoding="utf-8", errors="replace"),
            "page_count": 1,
            "used_ocr": False,
        }
    raise ParseError(f"不支持的文件类型: {ext}")


async def _parse_pdf(path: Path) -> dict[str, Any]:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ParseError("缺少 PyMuPDF 依赖，无法解析 PDF") from exc

    texts: list[str] = []
    scanned: list[int] = []
    with fitz.open(path) as doc:
        page_count = doc.page_count
        for idx, page in enumerate(doc):
            text = page.get_text("text").strip()
            texts.append(text)
            if len(text) < SCAN_PAGE_THRESHOLD:
                scanned.append(idx)

        used_ocr = False
        if scanned:
            logger.info("PDF %s 检测到 %d 个扫描页，走 OCR", path.name, len(scanned))
            images: list[bytes] = []
            targets = scanned[:MAX_OCR_PAGES]
            for idx in targets:
                # 200 DPI 足够识别，且控制体积
                pix = doc[idx].get_pixmap(dpi=200)
                images.append(pix.tobytes("png"))
            results = await ocr_client.recognize_many(images)
            for idx, ocr_text in zip(targets, results):
                if ocr_text.strip():
                    texts[idx] = ocr_text
                    used_ocr = True

    numbered = [f"[第{i + 1}页]\n{t}" for i, t in enumerate(texts) if t.strip()]
    return {"text": "\n\n".join(numbered), "page_count": page_count, "used_ocr": used_ocr}


def _docx_heading_level(p, style_name: str) -> bool:
    """判断 docx 段落是否标题（对齐 document-split 参考项目的识别策略）。

    优先级：大纲级别 outline_level（Word 内置，最可靠）> 样式名精确匹配
    （Heading N / 标题 N / 中文数字 / Title / Subtitle / 小标题）> 旧口径的
    宽泛包含匹配（兜底，保持向后兼容）。
    """
    # 1) 大纲级别：0=正文，1-9=层级
    try:
        outline = p.paragraph_format.outline_level
        if outline is not None and int(outline) > 0:
            return True
    except (AttributeError, TypeError, ValueError):
        pass

    sn = (style_name or "").strip()
    if not sn:
        return False
    # 2) 样式名精确匹配（阿拉伯/中文数字、常见标题样式）
    if re.match(r"^(?:[Hh]eading|标题)\s*\d+$", sn):
        return True
    if re.match(r"^(?:[Hh]eading|标题)\s*[一二三四五六七八九十]+$", sn):
        return True
    if sn.lower() in ("title", "subtitle", "subheading", "toc heading"):
        return True
    if "小标题" in sn:
        return True
    # 3) 兜底：旧口径宽泛包含（部分文档用自定义样式名如「标题-1」）
    return "Heading" in sn or "标题" in sn


def _parse_docx(path: Path) -> dict[str, Any]:
    try:
        import docx
        from docx.oxml.ns import qn
    except ImportError as exc:
        raise ParseError("缺少 python-docx 依赖，无法解析 Word") from exc
    if path.suffix.lower() == ".doc":
        raise ParseError("旧版 .doc 不受支持，请先转换为 .docx")

    document = docx.Document(str(path))
    parts = []
    chapter_idx = 0
    table_idx = 0

    # 按文档流顺序遍历 body 子元素（段落+表格交错），表格落在所属章节内——
    # 对齐参考项目 parse_docx；旧实现把全部表格追加文末，导致报价表等脱离章节
    para_objs = {p._element: p for p in document.paragraphs}
    table_objs = {t._element: t for t in document.tables}

    for child in document.element.body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            p = para_objs.get(child)
            if p is None:
                continue
            text = p.text.strip()
            if not text:
                continue
            style_name = (p.style.name or "") if p.style else ""
            if _docx_heading_level(p, style_name):
                chapter_idx += 1
                # 对标题样式段落插入章节标记，供 text_locator 推断「第N章」位置
                parts.append(f"[第{chapter_idx}章: {text}]\n")
            else:
                parts.append(text)
        elif tag == qn("w:tbl"):
            t = table_objs.get(child)
            if t is None:
                continue
            table_idx += 1
            rows = []
            for row in t.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"[表格{table_idx}]\n" + "\n".join(rows))
    return {"text": "\n\n".join(parts), "page_count": 0, "used_ocr": False}


def _parse_excel(path: Path) -> dict[str, Any]:
    """按扩展名分派：旧版 .xls/.xlt 用 xlrd，.xlsx/.xlsm 用 openpyxl。"""
    ext = path.suffix.lower()
    if ext in (".xls", ".xlt"):
        return _parse_xls(path)
    return _parse_xlsx(path)


def _parse_xlsx(path: Path) -> dict[str, Any]:
    try:
        import openpyxl
    except ImportError as exc:
        raise ParseError("缺少 openpyxl 依赖，无法解析 Excel") from exc

    try:
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    except Exception as exc:  # 加密/损坏/非标准格式
        raise ParseError(f"Excel(.xlsx)解析失败，可能是加密文件或格式损坏: {exc}") from exc
    parts: list[str] = []
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"[工作表: {ws.title}]\n" + "\n".join(rows))
    finally:
        wb.close()
    return {"text": "\n\n".join(parts), "page_count": len(parts), "used_ocr": False}


def _parse_xls(path: Path) -> dict[str, Any]:
    """旧版 OLE2 格式 .xls/.xlt，openpyxl 不支持，改用 xlrd。"""
    try:
        import xlrd
    except ImportError as exc:
        raise ParseError("缺少 xlrd 依赖，无法解析旧版 Excel(.xls)") from exc

    from xlrd import XL_CELL_DATE

    try:
        book = xlrd.open_workbook(str(path))
    except Exception as exc:
        raise ParseError(f"Excel(.xls)解析失败，可能是加密文件或格式损坏: {exc}") from exc

    parts: list[str] = []
    for sh in book.sheets():
        rows = []
        for r in range(sh.nrows):
            cells = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == XL_CELL_DATE:
                    try:
                        val = xlrd.xldate_as_datetime(
                            cell.value, book.datemode
                        ).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        val = cell.value
                else:
                    val = cell.value
                text = "" if val is None else str(val).strip()
                cells.append(text)
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"[工作表: {sh.name}]\n" + "\n".join(rows))
    return {"text": "\n\n".join(parts), "page_count": len(parts), "used_ocr": False}


def truncate(text: str, limit: int) -> str:
    """超长文本保留首尾（关键信息通常在开头的须知与结尾的承诺/签章部分）。"""
    if len(text) <= limit:
        return text
    head = int(limit * 0.65)
    tail = limit - head
    return (
        text[:head]
        + f"\n\n...[已省略中间 {len(text) - limit} 字]...\n\n"
        + text[-tail:]
    )
