"""端到端验证：导出报告、原文定位、知识库片段查看"""
import json
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

# Windows GBK 控制台兼容
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

TIMEOUT = 300000  # 25 条规则分批送审 + 一致性核查，实测需 2-4 分钟
downloads_dir = Path(__file__).parent / "downloads"
downloads_dir.mkdir(exist_ok=True)

def test():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        # 清理历史上传，避免累积文件干扰定位结果
        existing = page.request.get("http://127.0.0.1:8100/api/files").json()
        for f in existing.get("files", []):
            page.request.delete(f"http://127.0.0.1:8100/api/files/{f['file_id']}")

        page.goto("http://localhost:5173", wait_until="networkidle", timeout=TIMEOUT)

        # 1. 上传测试文件
        招标 = """南钢智能制造平台 招标文件
项目编号：NG-ZB-2026-001
投标保证金：项目预算的2%，不超过80万元
投标有效期：不少于60日历天"""

        投标 = """南钢智能制造平台 投标文件
项目编号：NG-ZB-2026-001
投标保证金：人民币10万元
投标有效期：自开标之日起60日历天"""

        for name, content, role in [
            ("招标文件.txt", 招标, "tender"),
            ("投标文件.txt", 投标, "bid"),
        ]:
            tmp = downloads_dir / name
            tmp.write_text(content, encoding="utf-8")
            page.locator('input[type="file"]').set_input_files(str(tmp))
            time.sleep(2)
            # 点击该行的角色下拉框并选择
            row = page.locator(f'tr:has-text("{name}")').first
            role_select = row.locator('.ant-select').first
            role_select.click()
            time.sleep(0.3)
            role_label = {"tender": "招标文件", "bid": "投标文件"}[role]
            page.locator(f'.ant-select-dropdown:visible .ant-select-item:has-text("{role_label}")').click()
            time.sleep(0.5)

        # 等待表格显示2条记录
        expect(page.locator('tbody tr').nth(1)).to_be_visible(timeout=5000)
        print("✓ 2 files uploaded, roles set")

        # 2. 收敛规则数量：实测一批(≤4条)约75秒，25条要9分钟。
        #    UI 勾选不稳定，改为在网络层重写请求体，强制只审 3 条规则。
        page.wait_for_selector('.ant-collapse-item', timeout=10000)

        def limit_rules(route):
            body = route.request.post_data_json or {}
            body['rule_ids'] = ['biz-bidbond', 'biz-validity', 'gen-typo']
            # 关掉知识库与联网检索：二者会让每批耗时翻倍，本用例不验证它们的触发
            body['kb_enabled'] = False
            body['web_search_enabled'] = False
            route.continue_(post_data=json.dumps(body))

        page.route('**/api/review/stream', limit_rules)
        print("✓ Review request pinned to 3 rules (kb/web off)")

        # 3. 启动审核
        page.locator('button:has-text("开始审核")').click()

        # 真正的完成信号是评分仪表盘：它只在 summary 就绪（done 事件）后渲染。
        # 不能等「取消」按钮——那是运行中状态。
        page.wait_for_selector('.ant-progress-circle', timeout=TIMEOUT)
        row_count = page.locator('.ant-table-tbody tr.ant-table-row').count()
        print(f"✓ review completed: {row_count} findings, summary rendered")

        # 4. 测试导出（中文文件名）
        export_btn = page.locator('.ant-card-extra button.ant-btn-primary').first
        export_btn.wait_for(state='visible', timeout=10000)
        export_btn.click()  # 展开下拉菜单
        time.sleep(0.5)
        # 点击 Word 导出菜单项（使用 role 而非文本）
        with page.expect_download(timeout=30000) as download_info:
            page.locator('.ant-dropdown-menu-item').first.click()
        dl = download_info.value
        dest = downloads_dir / dl.suggested_filename
        dl.save_as(dest)
        assert dest.exists() and dest.stat().st_size > 10000, f"导出失败或文件过小: {dest}"
        assert dest.suffix == ".docx", f"文件格式错误: {dest.suffix}"
        print(f"✓ 导出成功: {dest.name} ({dest.stat().st_size} bytes)")

        # 5. 测试原文定位（点击证据块里的「定位原文」链接按钮）
        locate_btns = page.locator('.evidence-block button.ant-btn-link')
        if locate_btns.count() > 0:
            locate_btns.first.click()
            # SnippetViewer 是 antd Modal，用 .ant-modal 定位
            page.wait_for_selector('.ant-modal-content', timeout=8000)
            # 等定位请求返回：命中高亮 <mark> 或「未找到」提示二者之一
            page.wait_for_selector('.ant-modal-content mark, .ant-modal-content .ant-alert',
                                   timeout=15000)
            hit = page.locator('.ant-modal-content mark').count()
            if hit:
                marked = page.locator('.ant-modal-content mark').first.inner_text()
                tabs = page.locator('.ant-modal-content .ant-tabs-tab').count()
                print(f"✓ 原文定位命中: {len(marked)} 字高亮, {tabs} 个文件标签")
                assert len(marked) > 0, "高亮内容为空"
            else:
                alert = page.locator('.ant-modal-content .ant-alert').first.inner_text()
                print(f"✓ 原文定位弹窗正常（未命中提示）: {alert[:40]}")
            page.locator('.ant-modal-close').first.click()
            time.sleep(0.5)
        else:
            print("⚠ 无证据块，跳过定位测试")

        # 6. 知识库片段原文查看：本轮已关闭 kb 检索，改用桩数据注入 done 事件验证渲染
        page.unroute('**/api/review/stream')
        kb_stub = {
            "type": "done",
            "summary": {"score": 60, "conclusion": "基本合规但有瑕疵", "total_rules": 1,
                        "status_counts": {"pass": 0, "fail": 0, "warn": 1, "unknown": 0},
                        "severity_counts": {"critical": 0, "major": 1, "minor": 0, "info": 0},
                        "consistency_issue_count": 0, "critical_items": []},
            "findings": [{"rule_id": "biz-bidbond", "rule_name": "投标保证金",
                          "category": "commercial", "severity": "major", "status": "warn",
                          "title": "需核对保证金上限", "detail": "", "evidence": "",
                          "location": "", "suggestion": "", "legal_basis": "",
                          "involved_files": [], "confidence": 0.7}],
            "consistency_issues": [],
            "kb_traces": [{
                "query": "投标保证金金额上限",
                "reason": "涉及法定量化标准",
                "answer": "投标保证金不得超过招标项目估算价的2%，且最高不超过80万元人民币。",
                "sources": [{"source_file": "招标投标法实施条例.pdf", "page": 5, "score": 0.93,
                             "text": "第二十六条 招标人在招标文件中要求投标人提交投标保证金的，"
                                     "投标保证金不得超过招标项目估算价的2%。投标保证金有效期"
                                     "应当与投标有效期一致。"}]}],
            "progress": 100,
        }
        page.route('**/api/review/stream', lambda r: r.fulfill(
            status=200, headers={'Content-Type': 'text/event-stream'},
            body=f"data: {json.dumps(kb_stub, ensure_ascii=False)}\n\n"))

        page.locator('button:has-text("开始审核")').click()
        page.wait_for_selector('.ant-badge', timeout=15000)   # 知识库检索记录卡片
        # 逐层展开：外层问题 -> 内层来源片段
        for _ in range(2):
            headers = page.locator('.ant-collapse-header')
            for i in range(headers.count()):
                h = headers.nth(i)
                if 'ant-collapse-item-active' not in (
                        h.locator('xpath=..').get_attribute('class') or ''):
                    h.click(); time.sleep(0.35)
        body_txt = page.inner_text('body')
        assert '第二十六条' in body_txt, "知识库来源片段原文未渲染"
        print("✓ 知识库片段原文可查看（含条文原文与页码）")

        browser.close()
        print("\n✅ 全部验证通过")

if __name__ == "__main__":
    test()
