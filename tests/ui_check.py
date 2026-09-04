"""前端渲染检查：加载页面、截图、捕获控制台错误。"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)
URL = "http://127.0.0.1:5173/"


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on(
            "console",
            lambda msg: errors.append(f"[{msg.type}] {msg.text}")
            if msg.type == "error"
            else None,
        )
        page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))

        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2500)

        title = page.title()
        print("页面标题:", title)

        # 关键区块是否渲染
        for label in ["文档合规审核工具", "待审文件", "审核规则", "审核选项"]:
            count = page.get_by_text(label, exact=False).count()
            print(f"  区块 [{label}]: {'OK' if count else '缺失'}")

        rules = page.get_by_text("投标保证金", exact=False).count()
        print(f"  规则列表加载: {'OK' if rules else '缺失'}")

        page.screenshot(path=str(OUT / "main.png"), full_page=True)

        # 打开服务配置抽屉
        page.get_by_role("button", name="服务配置").click()
        page.wait_for_timeout(1800)
        drawer = page.get_by_text("大模型（千问 80B）", exact=False).count()
        print(f"  配置抽屉: {'OK' if drawer else '缺失'}")
        page.screenshot(path=str(OUT / "settings.png"), full_page=True)

        browser.close()

    if errors:
        print("\n控制台错误:")
        for e in errors[:10]:
            print(" ", e[:200])
        return 1
    print("\n无控制台错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
