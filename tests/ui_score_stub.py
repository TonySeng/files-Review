"""不依赖大模型，直接拦截 SSE 返回桩数据，校验评分区在 100 分与低分下都正确渲染。"""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "screenshots"
FIXTURES = Path(__file__).parent / "fixtures"
URL = "http://127.0.0.1:5173/"


def make_stream(score: int, conclusion: str) -> str:
    findings = [
        {
            "rule_id": "biz-bidbond", "rule_name": "投标保证金", "category": "commercial",
            "severity": "critical", "status": "pass" if score == 100 else "fail",
            "title": "桩数据结论", "detail": "用于校验渲染", "evidence": "原文摘录",
            "location": "投标文件 第1页", "suggestion": "", "legal_basis": "《招标投标法实施条例》第二十六条",
            "involved_files": ["投标文件.docx"], "confidence": 0.9,
        }
    ]
    summary = {
        "score": score, "conclusion": conclusion, "total_rules": 1,
        "status_counts": {"pass": 1 if score == 100 else 0, "fail": 0 if score == 100 else 1,
                          "warn": 0, "unknown": 0},
        "severity_counts": {"critical": 0 if score == 100 else 1, "major": 0, "minor": 0, "info": 0},
        "consistency_issue_count": 0, "critical_items": [],
    }
    events = [
        {"type": "stage", "stage": "rules", "message": "审核规则 投标保证金", "progress": 50},
        {"type": "finding", "finding": findings[0]},
        {"type": "done", "summary": summary, "findings": findings,
         "consistency_issues": [], "kb_traces": [], "progress": 100},
    ]
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


def check(score: int, conclusion: str, tag: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        existing = page.request.get("http://127.0.0.1:8100/api/files").json()
        for f in existing.get("files", []):
            page.request.delete(f"http://127.0.0.1:8100/api/files/{f['file_id']}")

        page.route(
            "**/api/review/stream",
            lambda route: route.fulfill(
                status=200,
                headers={"Content-Type": "text/event-stream"},
                body=make_stream(score, conclusion),
            ),
        )

        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        page.set_input_files("input[type=file]", [str(FIXTURES / "投标文件.docx")])
        page.wait_for_timeout(6000)

        page.get_by_role("button", name="开始审核").click()
        page.wait_for_timeout(3000)

        dash = page.locator(".stat-card .ant-progress-text")
        rendered = dash.inner_text() if dash.count() else "(未渲染)"
        concl = page.locator(".stat-card").inner_text().replace("\n", " ")
        print(f"[{tag}] 期望分值={score} | dashboard 显示={rendered!r} | 结论区={concl!r}")
        assert str(score) in rendered, f"评分未正确渲染: {rendered!r}"
        page.screenshot(path=str(OUT / f"score_{tag}.png"), full_page=True)
        browser.close()


if __name__ == "__main__":
    check(100, "合规", "full")
    check(42, "存在否决项风险", "low")
    print("\n评分渲染校验通过")
