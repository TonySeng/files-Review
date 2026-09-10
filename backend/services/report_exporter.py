"""审核报告导出服务，支持 Word 和 PDF 格式。"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from docx import Document
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.shared import Inches, Pt, RGBColor
from docx.oxml.ns import qn

# reportlab（PDF 原生渲染，不依赖 LibreOffice / docx2pdf）
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    _CJK_FONT = "STSong-Light"  # reportlab 内置 CID 中文字体，无需外部 ttf
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
    except Exception:
        _CJK_FONT = "Helvetica"
    _HAS_REPORTLAB = True
except Exception:  # pragma: no cover - reportlab 未安装时降级
    _HAS_REPORTLAB = False
    _CJK_FONT = "Helvetica"


class ReportExporter:
    """审核报告导出器。"""

    STATUS_LABEL = {
        "pass": "✓ 无风险",
        "fail": "✗ 有风险",
        "warn": "⚠ 待复核",
        "unknown": "? 待复核",
    }

    SEVERITY_LABEL = {
        "critical": "否决项",
        "major": "重要",
        "minor": "一般",
        "info": "提示",
    }

    CATEGORY_LABEL = {
        "qualification": "资格性",
        "commercial": "商务",
        "technical": "技术",
        "format": "格式",
        "consistency": "一致性",
        "legal": "法规",
        "general_quality": "通用",
    }

    def __init__(self, data: dict[str, Any]):
        """
        Args:
            data: 审核结果数据，包含 findings, issues, kb_traces, summary, rule_results
        """
        self.data = data
        self.findings = data.get("findings", [])
        self.issues = data.get("consistency_issues", [])
        self.kb_traces = data.get("kb_traces", [])
        self.summary = data.get("summary", {})
        # 规则维度聚合结果（每条规则一条），用于「规则审核结果汇总」
        self.rule_results = data.get("rule_results", [])

    def export_word(self) -> bytes:
        """导出为 Word 文档。"""
        doc = Document()

        # 设置中文字体
        doc.styles["Normal"].font.name = "宋体"
        doc.styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

        # 标题
        title = doc.add_heading("文档合规审核报告", 0)
        title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER

        # 基本信息
        doc.add_paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        doc.add_paragraph()

        # 审核摘要
        self._add_summary_section(doc)

        # 审核结论详情
        self._add_findings_section(doc)

        # 规则维度聚合结果（每条规则一条，多文档/分段并行已合并去重）
        if self.rule_results:
            self._add_rule_results_section(doc)

        # 一致性问题
        if self.issues:
            self._add_consistency_section(doc)

        # 知识库检索记录
        if self.kb_traces:
            self._add_kb_traces_section(doc)

        # 保存到字节流
        bio = io.BytesIO()
        doc.save(bio)
        bio.seek(0)
        return bio.getvalue()

    def export_pdf(self) -> bytes:
        """导出为 PDF（原生 reportlab 渲染，不依赖 LibreOffice / docx2pdf）。"""
        if not _HAS_REPORTLAB:
            raise RuntimeError("PDF 导出需要安装 reportlab 库（pip install reportlab）")

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2 * cm,
            title="文档合规审核报告",
        )
        ss = getSampleStyleSheet()
        normal = ParagraphStyle("cn", parent=ss["Normal"], fontName=_CJK_FONT, fontSize=10, leading=14)
        h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName=_CJK_FONT, fontSize=15, leading=19)
        h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName=_CJK_FONT, fontSize=12, leading=16)
        title_style = ParagraphStyle(
            "title", parent=ss["Title"], fontName=_CJK_FONT, fontSize=20, leading=24, alignment=1
        )
        cell = ParagraphStyle("cell", parent=normal, fontSize=8.5, leading=11)
        cell_hdr = ParagraphStyle("cellh", parent=cell, textColor=colors.white)

        flow = []
        flow.append(Paragraph("文档合规审核报告", title_style))
        flow.append(Spacer(1, 6))
        flow.append(Paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal))
        flow.append(Spacer(1, 8))

        # 一、审核摘要
        flow.append(Paragraph("一、审核摘要", h1))
        summary = self.summary
        score = summary.get("score", 0)
        flow.append(Paragraph(f"综合评分：{score} 分", normal))
        flow.append(Paragraph(f"审核结论：{summary.get('conclusion', '未知')}", normal))
        counts = summary.get("status_counts") or {}
        if not counts:
            counts = {"pass": 0, "fail": 0, "warn": 0, "unknown": 0}
            for f in self.findings:
                st = f.get("status", "unknown")
                counts[st] = counts.get(st, 0) + 1
        tbl = Table(
            [
                [Paragraph("状态", cell_hdr), Paragraph("无风险", cell_hdr), Paragraph("有风险", cell_hdr), Paragraph("待复核", cell_hdr)],
                [Paragraph("数量", cell), Paragraph(str(counts.get("pass", 0)), cell), Paragraph(str(counts.get("fail", 0)), cell), Paragraph(str(counts.get("warn", 0) + counts.get("unknown", 0)), cell)],
            ],
            colWidths=[3 * cm, 4 * cm, 4 * cm, 4 * cm],
        )
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1668dc")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        flow.append(tbl)
        flow.append(Spacer(1, 6))
        critical = summary.get("critical_items") or []
        if critical:
            flow.append(Paragraph("否决项风险：" + "、".join(str(x) for x in critical), normal))
        flow.append(Spacer(1, 10))

        # 二、审核结论详情
        flow.append(Paragraph("二、审核结论详情", h1))
        if not self.findings:
            flow.append(Paragraph("无审核结论", normal))
        else:
            by_sev = {"critical": [], "major": [], "minor": [], "info": []}
            for f in self.findings:
                by_sev.setdefault(f.get("severity", "major"), []).append(f)
            for sev in ["critical", "major", "minor", "info"]:
                items = by_sev.get(sev, [])
                if not items:
                    continue
                flow.append(Paragraph(f"2.{list(by_sev.keys()).index(sev) + 1} {self.SEVERITY_LABEL.get(sev, sev)}", h2))
                for idx, f in enumerate(items, 1):
                    status = f.get("status", "unknown")
                    rule_name = f.get("rule_name", f.get("rule_id", "未知规则"))
                    title = f.get("title", "无结论")
                    flow.append(Paragraph(f"{idx}. {self.STATUS_LABEL.get(status, status)} <b>{rule_name}</b>：{title}", normal))
                    if f.get("detail"):
                        flow.append(Paragraph("判定理由：" + str(f["detail"]), normal))
                    if f.get("evidence"):
                        flow.append(Paragraph("文档证据：" + str(f["evidence"]), normal))
                    if f.get("location"):
                        flow.append(Paragraph("位置：" + str(f["location"]), normal))
                    if f.get("suggestion"):
                        flow.append(Paragraph("整改建议：" + str(f["suggestion"]), normal))
                    flow.append(Spacer(1, 4))
        flow.append(Spacer(1, 10))

        # 三、规则审核结果汇总（每条规则一条）
        if self.rule_results:
            flow.append(Paragraph("三、规则审核结果汇总", h1))
            data = [[Paragraph(x, cell_hdr) for x in ["规则", "类别", "严重级", "状态", "问题数", "问题样例"]]]
            for rr in self.rule_results:
                status = rr.get("status", "unknown")
                rule_name = rr.get("rule_name", rr.get("rule_id", "未知规则"))
                category = self.CATEGORY_LABEL.get(rr.get("category", ""), rr.get("category", ""))
                severity = self.SEVERITY_LABEL.get(rr.get("severity", ""), rr.get("severity", ""))
                issue_count = rr.get("issue_count", 0)
                samples = rr.get("samples", []) or []
                sample_text = "；".join(str(s) for s in samples[:2])
                if len(samples) > 2:
                    sample_text += "…"
                data.append([
                    Paragraph(str(rule_name), cell),
                    Paragraph(str(category), cell),
                    Paragraph(str(severity), cell),
                    Paragraph(self.STATUS_LABEL.get(status, status), cell),
                    Paragraph(str(issue_count), cell),
                    Paragraph(sample_text, cell),
                ])
            rtbl = Table(data, colWidths=[3.2 * cm, 1.8 * cm, 1.6 * cm, 2 * cm, 1.4 * cm, 4 * cm], repeatRows=1)
            rtbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1668dc")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            flow.append(rtbl)
            flow.append(Spacer(1, 10))

        # 四、一致性问题
        if self.issues:
            flow.append(Paragraph("四、一致性问题", h1))
            for idx, issue in enumerate(self.issues, 1):
                flow.append(Paragraph(f"{idx}. {issue.get('field', '未知字段')}：{issue.get('description', '')}", normal))
            flow.append(Spacer(1, 10))

        # 五、法规检索记录
        if self.kb_traces:
            flow.append(Paragraph("五、法规检索记录", h1))
            for idx, trace in enumerate(self.kb_traces, 1):
                flow.append(Paragraph(f"{idx}. 查询：{trace.get('query', '')}", normal))
                ans = trace.get("answer", "")
                if ans:
                    flow.append(Paragraph("回答：" + ans[:500] + ("..." if len(ans) > 500 else ""), normal))
                flow.append(Spacer(1, 4))

        doc.build(flow)
        return buf.getvalue()

    def _add_summary_section(self, doc: Document) -> None:
        """添加审核摘要章节。"""
        doc.add_heading("一、审核摘要", 1)

        summary = self.summary
        score = summary.get("score", 0)
        conclusion = summary.get("conclusion", "未知")

        # 综合评分
        p = doc.add_paragraph()
        p.add_run("综合评分：").bold = True
        run = p.add_run(f"{score} 分")
        if score >= 90:
            run.font.color.rgb = RGBColor(0, 128, 0)
        elif score >= 70:
            run.font.color.rgb = RGBColor(255, 140, 0)
        else:
            run.font.color.rgb = RGBColor(255, 0, 0)

        # 审核结论
        p = doc.add_paragraph()
        p.add_run("审核结论：").bold = True
        p.add_run(conclusion)

        # 统计表格
        doc.add_paragraph()
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"

        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = "状态"
        hdr_cells[1].text = "无风险"
        hdr_cells[2].text = "有风险"
        hdr_cells[3].text = "待复核"

        # 后端 summary 用嵌套的 status_counts，缺失时回退为按 findings 现算
        counts = summary.get("status_counts") or {}
        if not counts:
            counts = {"pass": 0, "fail": 0, "warn": 0, "unknown": 0}
            for f in self.findings:
                st = f.get("status", "unknown")
                counts[st] = counts.get(st, 0) + 1

        row_cells = table.add_row().cells
        row_cells[0].text = "数量"
        row_cells[1].text = str(counts.get("pass", 0))
        row_cells[2].text = str(counts.get("fail", 0))
        # 存疑 + 待确认统一为「待复核」
        row_cells[3].text = str(counts.get("warn", 0) + counts.get("unknown", 0))

        # 否决项风险单独提示
        critical_items = summary.get("critical_items") or []
        if critical_items:
            doc.add_paragraph()
            p = doc.add_paragraph()
            run = p.add_run("否决项风险：")
            run.bold = True
            run.font.color.rgb = RGBColor(0xCF, 0x13, 0x22)
            p.add_run("、".join(str(x) for x in critical_items))

        doc.add_paragraph()

    def _add_findings_section(self, doc: Document) -> None:
        """添加审核结论详情章节。"""
        doc.add_heading("二、审核结论详情", 1)

        if not self.findings:
            doc.add_paragraph("无审核结论")
            return

        # 按严重程度分组
        by_severity = {"critical": [], "major": [], "minor": [], "info": []}
        for f in self.findings:
            sev = f.get("severity", "major")
            by_severity.setdefault(sev, []).append(f)

        for severity in ["critical", "major", "minor", "info"]:
            items = by_severity.get(severity, [])
            if not items:
                continue

            doc.add_heading(
                f"2.{list(by_severity.keys()).index(severity) + 1} {self.SEVERITY_LABEL.get(severity, severity)}",
                2,
            )

            for idx, finding in enumerate(items, 1):
                self._add_finding_item(doc, idx, finding)

    def _add_finding_item(self, doc: Document, idx: int, finding: dict[str, Any]) -> None:
        """添加单个审核结论。"""
        status = finding.get("status", "unknown")
        rule_name = finding.get("rule_name", finding.get("rule_id", "未知规则"))
        title = finding.get("title", "无结论")

        # 标题
        p = doc.add_paragraph()
        p.add_run(f"{idx}. ").bold = True
        p.add_run(f"{self.STATUS_LABEL.get(status, status)} ").font.size = Pt(11)
        p.add_run(rule_name).bold = True
        p.add_run(f"：{title}")

        # 详情
        detail = finding.get("detail", "")
        if detail:
            doc.add_paragraph(f"判定理由：{detail}", style="List Bullet")

        # 证据
        evidence = finding.get("evidence", "")
        if evidence:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run("文档证据：").bold = True
            p.add_run(evidence)

        # 位置
        location = finding.get("location", "")
        if location:
            doc.add_paragraph(f"位置：{location}", style="List Bullet")

        # 建议
        suggestion = finding.get("suggestion", "")
        if suggestion:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run("整改建议：").bold = True
            p.add_run(suggestion)

        # 法规依据
        legal_basis = finding.get("legal_basis", "")
        if legal_basis:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run("法规依据：").bold = True
            p.add_run(legal_basis)

        doc.add_paragraph()

    def _add_rule_results_section(self, doc: Document) -> None:
        """添加规则审核结果汇总章节（每条规则一条）。"""
        doc.add_heading("三、规则审核结果汇总", 1)

        table = doc.add_table(rows=1, cols=6)
        table.style = "Light Grid Accent 1"
        hdr = table.rows[0].cells
        for i, h in enumerate(["规则", "类别", "严重级", "状态", "问题数", "问题样例"]):
            hdr[i].text = h

        for rr in self.rule_results:
            status = rr.get("status", "unknown")
            rule_name = rr.get("rule_name", rr.get("rule_id", "未知规则"))
            category = self.CATEGORY_LABEL.get(rr.get("category", ""), rr.get("category", ""))
            severity = self.SEVERITY_LABEL.get(rr.get("severity", ""), rr.get("severity", ""))
            issue_count = rr.get("issue_count", 0)
            samples = rr.get("samples", []) or []
            sample_text = "；".join(str(s) for s in samples[:2])
            if len(samples) > 2:
                sample_text += "…"

            cells = table.add_row().cells
            cells[0].text = str(rule_name)
            cells[1].text = str(category)
            cells[2].text = str(severity)
            cells[3].text = self.STATUS_LABEL.get(status, status)
            cells[4].text = str(issue_count)
            cells[5].text = sample_text

        doc.add_paragraph()

    def _add_consistency_section(self, doc: Document) -> None:
        """添加一致性问题章节。"""
        doc.add_heading("四、一致性问题", 1)

        for idx, issue in enumerate(self.issues, 1):
            field = issue.get("field", "未知字段")
            description = issue.get("description", "")

            p = doc.add_paragraph()
            p.add_run(f"{idx}. ").bold = True
            p.add_run(f"{field}：{description}")

    def _add_kb_traces_section(self, doc: Document) -> None:
        """添加知识库检索记录章节。"""
        doc.add_heading("五、法规检索记录", 1)

        for idx, trace in enumerate(self.kb_traces, 1):
            query = trace.get("query", "")
            answer = trace.get("answer", "")

            p = doc.add_paragraph()
            p.add_run(f"{idx}. 查询：").bold = True
            p.add_run(query)

            if answer:
                p = doc.add_paragraph(style="List Bullet")
                p.add_run("回答：").bold = True
                p.add_run(answer[:500] + ("..." if len(answer) > 500 else ""))

            doc.add_paragraph()


def export_report(data: dict[str, Any], format: str = "word") -> bytes:
    """
    导出审核报告。

    Args:
        data: 审核结果数据
        format: 导出格式 ("word" 或 "pdf")

    Returns:
        文件字节内容
    """
    exporter = ReportExporter(data)

    if format == "word":
        return exporter.export_word()
    elif format == "pdf":
        # 原生 PDF 渲染（reportlab），不依赖 LibreOffice / docx2pdf
        return exporter.export_pdf()
    else:
        raise ValueError(f"不支持的导出格式：{format}")
