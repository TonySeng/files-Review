"""审核报告导出接口。"""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..routers import deps
from ..services import report_exporter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/export", tags=["export"])


class ExportRequest(BaseModel):
    """导出请求。"""

    findings: list[dict]
    consistency_issues: list[dict] = []
    kb_traces: list[dict] = []
    summary: dict | None = None
    rule_results: list[dict] = []  # 规则维度聚合结果（每条规则一条）
    format: str = "word"  # "word" 或 "pdf"


@router.post("/report", summary="导出审核报告（docx/pdf），返回文件流")
async def export_report(req: ExportRequest, _: dict = Depends(deps.get_caller)):
    """导出审核报告为 Word 或 PDF。需登录。"""
    if req.format not in ["word", "pdf"]:
        raise HTTPException(status_code=400, detail="格式必须是 'word' 或 'pdf'")

    try:
        data = {
            "findings": req.findings,
            "consistency_issues": req.consistency_issues,
            "kb_traces": req.kb_traces,
            "summary": req.summary or {},
            "rule_results": req.rule_results,
        }

        content = report_exporter.export_report(data, format=req.format)

        # 生成文件名（HTTP header 仅支持 latin-1，中文名需 RFC 5987 编码）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if req.format == "word":
            filename = f"审核报告_{timestamp}.docx"
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            filename = f"审核报告_{timestamp}.pdf"
            media_type = "application/pdf"

        # ASCII 回退名 + UTF-8 编码名，兼容各类客户端
        ascii_name = f"review_report_{timestamp}.{'docx' if req.format == 'word' else 'pdf'}"
        encoded_name = quote(filename)

        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{ascii_name}"; '
                    f"filename*=UTF-8''{encoded_name}"
                ),
            },
        )

    except Exception as exc:
        logger.exception("导出报告失败")
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc
