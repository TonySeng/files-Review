"""端到端 UI 测试：通过界面上传文件、选规则、跑审核、看结果。"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

FIXTURES = Path(__file__).parent / "fixtures"
OUT = Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)
URL = "http://127.0.0.1:5173/"

# 只留 2 条规则，控制单次测试耗时
KEEP_RULES = ["投标保证金", "跨文件信息一致性"]


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
        page.on(
            "console",
            lambda m: errors.append(f"[console] {m.text}") if m.type == "error" else None,
        )

        # 清掉历史上传，保证从干净状态开始
        page.request.get("http://127.0.0.1:8100/api/files")
        existing = page.request.get("http://127.0.0.1:8100/api/files").json()
        for f in existing.get("files", []):
            page.request.delete(f"http://127.0.0.1:8100/api/files/{f['file_id']}")

        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        # 1. 上传三个文件
        page.set_input_files(
            "input[type=file]",
            [
                str(FIXTURES / "招标文件.docx"),
                str(FIXTURES / "投标文件.docx"),
                str(FIXTURES / "报价明细表.docx"),
            ],
        )
        page.wait_for_timeout(9000)
        rows = page.locator(".ant-table-tbody tr.ant-table-row").count()
        print(f"1. 上传文件: {rows} 行")
        if rows != 3:
            print("   期望 3 行"); page.screenshot(path=str(OUT / "e2e_fail.png")); browser.close(); return 1

        # 2. 第一行标为招标文件
        page.locator(".ant-table-tbody tr.ant-table-row").first.locator(".ant-select").click()
        page.wait_for_timeout(700)
        page.get_by_title("招标文件").last.click()
        page.wait_for_timeout(1200)
        tender_ok = page.get_by_text("未标注招标文件").count() == 0
        print(f"2. 标注招标文件: {'OK' if tender_ok else '未生效'}")

        # 3. 取消全选，再勾选目标规则
        全选 = page.get_by_text("全选", exact=True)
        全选.click(); page.wait_for_timeout(400)
        if page.locator(".ant-checkbox-checked").count() > 5:
            全选.click(); page.wait_for_timeout(600)

        for name in KEEP_RULES:
            # 规则行结构：<div flex><Checkbox/><div><span>规则名</span>…</div></div>
            box = page.locator(
                f"xpath=//span[normalize-space(text())='{name}']"
                f"/ancestor::div[.//input[@type='checkbox']][1]"
                f"//input[@type='checkbox']"
            ).first
            if box.count():
                box.check()
                page.wait_for_timeout(300)
            else:
                print(f"   未找到规则复选框: {name}")
        picked = page.get_by_text("已选", exact=False).all_inner_texts()
        print(f"3. 规则选择: {[t for t in picked if '条' in t][:1]}")

        page.screenshot(path=str(OUT / "e2e_before.png"), full_page=True)

        # 4. 开始审核
        page.get_by_role("button", name="开始审核").click()
        print("4. 审核已启动…")

        done = False
        for _ in range(120):  # 最多 10 分钟
            page.wait_for_timeout(5000)
            if page.get_by_text("审核完成：", exact=False).count():
                done = True
                break
        print(f"5. 审核完成: {done}")

        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "e2e_result.png"), full_page=True)

        # 6. 结果核对
        score = page.locator(".ant-progress-text").first
        print("   得分:", score.inner_text() if score.count() else "无")
        verdicts = [
            t
            for t in page.locator(".ant-tag").all_inner_texts()
            if t in ("不合规", "通过", "存疑", "待确认")
        ]
        print("   结论:", verdicts[:6])
        kb = [t for t in page.locator(".ant-tag").all_inner_texts() if "知识库检索" in t]
        print("   知识库:", kb or "未触发")
        print("   一致性问题区块:", "OK" if page.get_by_text("一致性核查", exact=False).count() else "无")

        browser.close()

    if errors:
        print("\n控制台错误:")
        for e in errors[:8]:
            print(" ", e[:180])
        return 1
    print("\n无控制台错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
