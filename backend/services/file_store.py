"""上传文件存储与解析结果缓存。

文件字节与元数据均持久化到数据卷 backend/data（uploads 子目录 + files.json），
因此容器重建/重启后已上传文件不会丢失（此前字节写在镜像内 uploads、元数据在内存，
重启即清空）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from . import doc_parser
from . import doc_splitter
from . import file_types
from .. import config
from .. import storage

logger = logging.getLogger(__name__)

# 上传目录经统一存储层动态解析（files.local.base_dir 可热切换）；
# 元数据持久化为 files_meta 集合（统一存储层）

_lock = threading.Lock()
_files: dict[str, dict[str, Any]] = {}

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB


def _load_files() -> None:
    """启动时从磁盘恢复文件元数据（字节已在 uploads 卷内）。"""
    raw = storage.read_collection("files_meta")
    if not isinstance(raw, dict):
        return
    for fid, rec in raw.items():
        # 仅保留磁盘上字节仍存在的记录，避免指向已丢失文件
        if rec.get("path") and Path(rec["path"]).exists():
            _files[fid] = rec


def _save_files() -> None:
    try:
        storage.write_collection("files_meta", _files)
    except Exception:  # noqa: BLE001
        pass


def _persist_on_switch(_old_driver: str, _new_driver: str) -> None:
    """存储后端热切换时，把当前内存态整写到新后端（数据以内存为权威）。"""
    storage.write_collection("files_meta", _files)

storage.on_structured_switch(_persist_on_switch)

_load_files()


class StoreError(RuntimeError):
    pass


def _safe_name(name: str) -> str:
    """去除路径分隔与危险字符，防目录穿越。"""
    name = Path(name).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name[:180] or "unnamed"


def _owner_ok(record: dict[str, Any] | None, scope_user_id: str | None) -> bool:
    """归属校验：scope_user_id 为 None 表示管理员/内部调用（不过滤）；
    否则仅当记录归属为空（历史/系统级）或等于该用户时放行。"""
    if record is None:
        return False
    if scope_user_id is None:
        return True
    return record.get("owner_id") in (None, scope_user_id)


async def save_and_parse(
    filename: str, content: bytes, role: str = "bid", file_type: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    if len(content) > MAX_FILE_SIZE:
        raise StoreError(f"文件超过 {MAX_FILE_SIZE // 1024 // 1024}MB 大小限制")

    safe = _safe_name(filename)
    ext = Path(safe).suffix.lower()
    if ext not in doc_parser.SUPPORTED:
        supported = ", ".join(sorted(doc_parser.SUPPORTED))
        raise StoreError(
            f"不支持的文件格式 {ext or '(无扩展名)'}，系统支持：{supported}"
        )

    # 文件类型校验：若该类型限定了后缀，则扩展名必须匹配（格式规则校验）
    ft_record = file_types.get(file_type) if file_type else None
    type_exts = ft_record.get("extensions") if ft_record else []
    if type_exts and ext not in type_exts:
        raise StoreError(
            f"文件【{safe}】的类型「{ft_record.get('name')}」仅允许后缀 "
            f"{', '.join(type_exts)}，但当前为 {ext or '(无扩展名)'}"
        )

    file_id = uuid.uuid4().hex
    upload_root = storage.upload_dir()
    upload_root.mkdir(parents=True, exist_ok=True)
    dest = upload_root / f"{file_id}{ext}"
    dest.write_bytes(content)
    md5 = hashlib.md5(content).hexdigest()

    record: dict[str, Any] = {
        "file_id": file_id,
        "filename": safe,
        "md5": md5,
        "size": len(content),
        "ext": ext,
        # role=legal：法规依据文件，仅用于生成临时规则，不参与审核（review 侧会拦截）
        "role": role if role in ("tender", "bid", "attachment", "legal") else "bid",
        "file_type": file_type,
        # 归属用户：普通用户上传记本人 user_id；管理员/系统级为 None（全局可见）。
        # 文件读/写/删按此字段做作用域隔离，防止跨用户越权访问送审原文。
        "owner_id": owner_id,
        "path": str(dest),
        "text": "",
        "char_count": 0,
        "page_count": 0,
        "used_ocr": False,
        "parse_error": None,
        # 解析阶段产出的章节结构（doc_splitter，对齐 document-split 参考项目），
        # 审核引擎按规则关联章节直接复用，避免逐批次全文重切
        "sections": [],
        "section_count": 0,
        "validation": {"ok": True, "level": "ok", "messages": []},
    }

    try:
        parsed = await doc_parser.parse(dest, ext)
        record["text"] = parsed["text"]
        record["char_count"] = len(parsed["text"])
        record["page_count"] = parsed.get("page_count", 0)
        record["used_ocr"] = parsed.get("used_ocr", False)
        if not parsed["text"].strip():
            record["parse_error"] = "未提取到文本内容，可能是空文件或纯图片且 OCR 无结果"
    except Exception as exc:  # 解析失败不阻塞其他文件
        logger.warning("解析 %s 失败: %s", safe, exc)
        record["parse_error"] = str(exc)

    # 章节拆分（解析阶段一次完成；失败仅降级为空，审核侧回退实时裁剪）
    if record["text"].strip() and not record["parse_error"]:
        try:
            secs = doc_splitter.split_document(record["text"], ext)
            record["sections"] = secs
            record["section_count"] = sum(1 for s in secs if s.get("title"))
            logger.info(
                "章节拆分 %s：%d 段（%s）",
                safe,
                record["section_count"],
                doc_splitter.stats(secs).get("by_source", {}),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("章节拆分 %s 失败（审核侧回退实时裁剪）: %s", safe, exc)

    # 内容规则校验：空文本/解析异常在状态中提示（不阻断上传，但审核前会再校验）
    if record["parse_error"]:
        record["validation"] = {
            "ok": False,
            "level": "error",
            "messages": [record["parse_error"]],
        }

    with _lock:
        _files[file_id] = record
    _save_files()
    return record


def _role_for_file_type(file_type: str | None) -> str:
    """文件类型 → 引擎角色映射（保持招标要求提取逻辑向后兼容）。"""
    if not file_type:
        return "bid"
    name = (file_types.get(file_type) or {}).get("name", "")
    if "招标" in name:
        return "tender"
    if "投标" in name:
        return "bid"
    return "attachment"


def get(file_id: str, scope_user_id: str | None = None) -> dict[str, Any] | None:
    """按 id 取文件记录。scope_user_id 非 None 时做归属校验，越权返回 None。"""
    with _lock:
        record = _files.get(file_id)
    return record if _owner_ok(record, scope_user_id) else None


def get_many(file_ids: list[str], scope_user_id: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        found = [_files[f] for f in file_ids if f in _files]
    # 归属校验：越权文件视同不存在（避免泄露他人文件的存在性）
    found = [f for f in found if _owner_ok(f, scope_user_id)]
    missing = set(file_ids) - {f["file_id"] for f in found}
    if missing:
        raise StoreError(f"文件不存在或已过期: {', '.join(sorted(missing))}")
    return found


def list_files(scope_user_id: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        vals = list(_files.values())
    return [_public(f) for f in vals if _owner_ok(f, scope_user_id)]


def set_role(file_id: str, role: str, scope_user_id: str | None = None) -> dict[str, Any]:
    if role not in ("tender", "bid", "attachment", "legal"):
        raise StoreError(f"非法的文件角色: {role}")
    with _lock:
        record = _files.get(file_id)
        if not record or not _owner_ok(record, scope_user_id):
            raise StoreError("文件不存在")
        record["role"] = role
        _save_files()
        return _public(record)


def set_file_type(file_id: str, file_type: str | None, scope_user_id: str | None = None) -> dict[str, Any]:
    """手动指定文件类型（系统不自动识别）；同步派生引擎角色。"""
    with _lock:
        record = _files.get(file_id)
        if not record or not _owner_ok(record, scope_user_id):
            raise StoreError("文件不存在")
        # 校验类型是否存在且后缀匹配
        ft_record = file_types.get(file_type) if file_type else None
        if file_type and not ft_record:
            raise StoreError(f"文件类型不存在: {file_type}")
        ext = record.get("ext", "")
        type_exts = ft_record.get("extensions") if ft_record else []
        if type_exts and ext and ext not in type_exts:
            raise StoreError(
                f"当前文件格式 {ext or '(无扩展名)'} 不属于类型"
                f"「{ft_record.get('name')}」（允许 {', '.join(type_exts)}）"
            )
        record["file_type"] = file_type
        record["role"] = _role_for_file_type(file_type)
        if file_type:
            record["validation"] = {
                "ok": not record.get("parse_error"),
                "level": "error" if record.get("parse_error") else "ok",
                "messages": [record["parse_error"]] if record.get("parse_error") else [],
            }
        _save_files()
        return _public(record)


def delete(file_id: str, scope_user_id: str | None = None) -> bool:
    with _lock:
        record = _files.get(file_id)
        if not record or not _owner_ok(record, scope_user_id):
            return False
        _files.pop(file_id, None)
    Path(record["path"]).unlink(missing_ok=True)
    _save_files()
    return True


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """不外传全文、章节体、本地路径与归属用户（section_count 保留供前端展示）。"""
    return {
        k: v
        for k, v in record.items()
        if k not in ("text", "path", "sections", "owner_id")
    }
