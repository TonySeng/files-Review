"""审核规则配置接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
import json as _json
import re
from datetime import datetime, timezone

from ..services import rules_store
from ..services import rule_import
from ..routers import deps

router = APIRouter(prefix="/api/rulesets", tags=["rules"])


@router.get("", summary="查询规则集列表（内置 + 自定义）")
async def list_rulesets(caller: dict = Depends(deps.get_caller)):
    """返回规则集列表。管理员看全部；普通用户仅看自己拥有或共享的。"""
    return {"rulesets": rules_store.list_rulesets(deps.scope_user_id(caller))}


@router.get("/by-mode/{mode}", summary="按审核模式查询规则（bid/tender/general）")
async def get_rules_by_mode(mode: str, caller: dict = Depends(deps.get_caller)):
    """根据审核模式返回适用规则（不保存为规则集，仅返回规则列表）。"""
    if mode not in ("bid", "tender", "general"):
        raise HTTPException(status_code=400, detail="模式必须为 bid/tender/general")
    return {"mode": mode, "rules": rules_store.get_rules_by_mode(mode)}


@router.get("/consistency-elements", summary="查询一致性核查要素清单")
async def list_consistency_elements():
    """一致性核查「核心要素」库：供规则编辑界面选用，也可按规范名查同义词。

    返回 elements（每项含 name / synonyms / group / note）与去重后的 groups。
    规则通过 structured.consistency_elements 声明自己要比对的要素，
    未声明时引擎会按规则文本回查本库推断。
    """
    elements = rules_store.list_consistency_library()
    groups: list[str] = []
    for e in elements:
        g = str(e.get("group") or "未分组")
        if g not in groups:
            groups.append(g)
    return {"elements": elements, "groups": groups}


@router.post("/consistency-preview", summary="预览一致性核查要素入口配置")
async def preview_consistency(payload: dict, caller: dict = Depends(deps.get_caller)):
    """预览一组规则聚合出的一致性核查规格（要素 + 核查要点 + 来源规则）。

    支持两种入参：
    - {"rule_ids": [...]}：按 id 解析（内置 + 自定义规则集，覆盖规则组场景）
    - {"rules": [...]}：直接传规则对象

    用于规则集/规则组编辑时实时展示「这套规则会跨文件比对哪些核心要素」。
    """
    scope = deps.scope_user_id(caller)
    rules = payload.get("rules")
    if not isinstance(rules, list):
        rules = rules_store.get_rules_by_ids(payload.get("rule_ids") or [], scope)
    return rules_store.collect_consistency_spec(rules)


@router.get("/template", summary="下载规则导入模板（xlsx）")
async def download_template(format: str = "csv"):
    """下载规则导入模板（CSV 或 XLSX）。"""
    fmt = (format or "csv").lower()
    if fmt == "xlsx":
        content = rule_import.build_template_xlsx()
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=rules_template.xlsx"},
        )
    # 默认 CSV（含 BOM，便于 Excel 直接打开）
    return Response(
        content=rule_import.build_template_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=rules_template.csv"},
    )


@router.post("/import", summary="批量导入规则（xlsx 上传）")
async def import_rules(
    file: UploadFile,
    ruleset_name: str | None = Form(None),
    append_to: str | None = Form(None),
    caller: dict = Depends(deps.get_caller),
):
    """批量导入规则：解析 CSV/XLSX/XLS，写入（或追加到）自定义规则集。

    - append_to 为自定义规则集 id 时，规则追加到该集合；
    - 否则（或 append_to 为内置集合）创建新的自定义规则集。
    返回导入条数、跳过条数与错误明细。
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")

    # JSON 规则集（全量保真）走独立分支：按原始结构保存，保留 id/structured/mode 等
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    # 自定义资源归属创建者本人（含管理员）；仅内置预置才系统共享
    owner = caller.get("user_id")
    if ext == "json":
        try:
            sets, errors = rule_import.parse_rulesets_json(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not sets:
            detail = "没有可导入的有效规则集：" + "；".join(errors[:5])
            raise HTTPException(status_code=400, detail=detail)
        saved_ids: list[str] = []
        for rs in sets:
            saved = rules_store.save_ruleset(
                {
                    "id": rs.get("id"),
                    "name": rs["name"],
                    "description": rs.get("description", ""),
                    "builtin": rs.get("builtin", False),
                    "mode": rs.get("mode"),
                    "rules": rs["rules"],
                },
                user_id=owner,
            )
            saved_ids.append(saved["id"])
        return {
            "format": "json",
            "rulesets": saved_ids,
            "imported_sets": len(saved_ids),
            "skipped": len(errors),
            "errors": errors,
        }

    try:
        rules, errors = rule_import.parse_rules_file(file.filename or "rules", data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not rules:
        detail = "没有可导入的有效规则：" + "；".join(errors[:5])
        raise HTTPException(status_code=400, detail=detail)

    # 分组：若行内含「规则集名称」列则按名称分组建集（导出文件回导场景），
    # 否则沿用原有单集逻辑（尊重 append_to / ruleset_name 表单参数）。
    from collections import defaultdict

    grouped: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for r in rules:
        gname = (r.get("ruleset_name") or "").strip() or None
        grouped[gname].append(r)

    if len(grouped) == 1 and None in grouped:
        # —— 旧式单集导入（与改造前完全一致）——
        existing = rules_store.get_ruleset(append_to, owner) if append_to else None
        if existing and not existing.get("builtin"):
            merged = [dict(r, builtin=False) for r in existing.get("rules", [])] + rules
            saved = rules_store.save_ruleset(
                {
                    "id": existing["id"],
                    "name": existing["name"],
                    "description": existing.get("description", ""),
                    "rules": merged,
                },
                user_id=owner,
            )
        else:
            name = (ruleset_name or "").strip() or f"导入的规则集（{(file.filename or 'rules').rsplit('.', 1)[0]}）"
            saved = rules_store.save_ruleset(
                {
                    "name": name,
                    "description": f"批量导入 {len(rules)} 条规则",
                    "rules": rules,
                },
                user_id=owner,
            )
        return {
            "ruleset_id": saved["id"],
            "ruleset_name": saved["name"],
            "imported": len(rules),
            "skipped": len(errors),
            "errors": errors,
        }

    # —— 多集分组导入（导出文件带「规则集名称」列）——
    summary: list[dict[str, Any]] = []
    total = 0
    fallback_name = (ruleset_name or "").strip() or f"导入的规则集（{(file.filename or 'rules').rsplit('.', 1)[0]}）"
    for gname, grules in grouped.items():
        target_name = gname or fallback_name
        existing = None
        if gname:
            existing = next(
                (rs for rs in rules_store.list_rulesets(owner)
                 if rs.get("name") == gname and not rs.get("builtin")),
                None,
            )
        if existing:
            merged = [dict(r, builtin=False) for r in existing.get("rules", [])] + grules
            saved = rules_store.save_ruleset(
                {
                    "id": existing["id"],
                    "name": existing["name"],
                    "description": existing.get("description", ""),
                    "rules": merged,
                },
                user_id=owner,
            )
        else:
            saved = rules_store.save_ruleset(
                {
                    "name": target_name,
                    "description": f"批量导入 {len(grules)} 条规则",
                    "rules": grules,
                },
                user_id=owner,
            )
        summary.append({"ruleset_id": saved["id"], "ruleset_name": saved["name"], "imported": len(grules)})
        total += len(grules)

    return {
        "format": ext,
        "imported": total,
        "skipped": len(errors),
        "errors": errors,
        "rulesets": summary,
        "ruleset_id": summary[0]["ruleset_id"],
        "ruleset_name": summary[0]["ruleset_name"],
    }


@router.get("/export", summary="导出全部规则为 xlsx 文件")
async def export_rulesets(
    ruleset_id: str | None = Query(None, description="导出指定规则集；为空则导出当前用户全部可访问规则集"),
    format: str = Query("xlsx", description="导出格式：json（全量保真）/ csv / xlsx（可编辑后回导）"),
    caller: dict = Depends(deps.get_caller),
):
    """导出规则集。

    - format=json：全量保真 JSON（保留 id / builtin / mode / structured / doc_types 等所有字段），
      可被 POST /api/rulesets/import 的 JSON 分支原样重新导入。
    - format=csv / xlsx：表格格式，列结构与「导入模板」完全一致（并增加「规则集名称」列），
      可在 Excel / WPS 中直接编辑后用 POST /api/rulesets/import 回导。注意表格格式仅保留
      可编辑字段（名称/类别/级别/说明/要点/法规依据/启用），不保留 id / structured 等内部结构。
    """
    scope = deps.scope_user_id(caller)
    if ruleset_id:
        rs = rules_store.get_ruleset(ruleset_id, scope)
        if not rs:
            raise HTTPException(status_code=404, detail="规则集不存在或无权访问")
        sets = [rs]
        base = f"ruleset_{ruleset_id}"
    else:
        sets = rules_store.list_rulesets(scope)
        base = f"rulesets_all_{datetime.now():%Y%m%d_%H%M%S}"

    fmt = (format or "xlsx").lower()
    if fmt == "json":
        payload = {
            "schema": "bidding-review/rulesets@1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "rulesets": sets,
        }
        content = _json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        media_type = "application/json"
        ext = "json"
    elif fmt == "csv":
        content = rule_import.export_rules_csv(sets)
        media_type = "text/csv; charset=utf-8"
        ext = "csv"
    elif fmt in ("xlsx", "xls"):
        content = rule_import.export_rules_xlsx(sets)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    else:
        raise HTTPException(status_code=400, detail="format 仅支持 json / csv / xlsx")

    safe = re.sub(r"[^\w-]", "_", str(base)).strip("_")[:80] or "rulesets"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename={safe}.{ext}"},
    )


@router.get("/{ruleset_id}", summary="查询单个规则集详情（含规则明细）")
async def get_ruleset(ruleset_id: str, caller: dict = Depends(deps.get_caller)):
    try:
        rs = rules_store.get_ruleset(ruleset_id, deps.scope_user_id(caller))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规则集不存在") from exc
    if not rs:
        raise HTTPException(status_code=404, detail="规则集不存在或无权访问")
    return rs


@router.post("", summary="新建自定义规则集")
async def save_ruleset(payload: dict, caller: dict = Depends(deps.get_caller)):
    """新建或更新规则集（内置规则集不可改，会另存为副本）。

    自定义资源归属创建者本人（含管理员）；仅内置预置（builtin-default）才系统共享，
    其他用户不可见。
    """
    owner = caller.get("user_id")
    try:
        return rules_store.save_ruleset(payload, user_id=owner)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{ruleset_id}", summary="删除自定义规则集")
async def delete_ruleset(ruleset_id: str, caller: dict = Depends(deps.get_caller)):
    owner = deps.scope_user_id(caller)  # 管理员 None 可删任意；用户仅自己的
    if not rules_store.delete_ruleset(ruleset_id, owner):
        raise HTTPException(status_code=400, detail="规则集不存在/内置/非所属，不可删除")
    return {"deleted": True}
