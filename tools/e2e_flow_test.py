#!/usr/bin/env python3
"""
NovelEngine 全流程浏览器自动化测试
模拟用户点击：爬取(scout) → 提取(extract) → 写作台(desk)

依赖: playwright (pip install playwright && playwright install chromium)
用法: python tools/e2e_flow_test.py [--headless] [--book 书名]
"""
import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "http://127.0.0.1:58080"
SHOT_DIR = Path(__file__).parent / "shots"
SHOT_DIR.mkdir(exist_ok=True)

REPORT = []


def log(msg, ok=None):
    mark = {"ok": "✅", "fail": "❌", "skip": "⏭️", None: "→"}[ok]
    line = f"{mark} {msg}"
    print(line)
    REPORT.append({"msg": msg, "ok": ok})


def wait_for(page, selector, timeout=60000):
    """等待元素出现"""
    page.wait_for_selector(selector, timeout=timeout)


def wait_disappear(page, selector, timeout=120000):
    """等待元素消失（或文本变化）"""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        if not page.locator(selector).count():
            return True
        time.sleep(2)
    return False


def shot(page, name):
    path = SHOT_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    log(f"截图: {path}", "ok")


def step_scout(page, book_title, chapters=8):
    """① 侦察/提取合并页：下载+分析由侧栏 dsh agent 驱动（页内输入发任务），浏览器自动化只验证页面渲染"""
    log("═══ 步骤1: 侦察/提取 /scout ═══")
    page.goto(BASE + "/scout")
    shot(page, "01_scout_initial")

    # 验证合并页四区渲染：任务条（书名/章节数/笔名）+ 已下载书库
    wait_for(page, "#book-title", 20000)
    wait_for(page, "#scout-pen", 20000)
    wait_for(page, "#novels-list", 20000)
    log("合并页就绪：Agent 任务条 + 已下载书库渲染", "ok")
    # 下载/分析现由 agent 驱动（fetch_novel → set_review 呈现候选待确认），不在浏览器自动化范围
    log("下载/分析由 agent 驱动，跳过浏览器自动抓取", "skip")
    return True


def step_extract(page):
    """② 提取已并入 /scout：验证已下载书库列表渲染（分析由 agent 完成后经 set_review 呈现候选，用户确认入库）"""
    log("═══ 步骤2: 提取（并入 /scout） ═══")
    page.goto(BASE + "/scout")
    shot(page, "04_extract_initial")

    # 等待已下载小说列表
    wait_for(page, "#novels-list .card", 20000)
    novels = page.locator("#novels-list .card").count()
    log(f"已下载小说列表: {novels} 本")
    if novels == 0:
        log("无可提取小说，跳过提取", "skip")
        return False

    log("提取为 agent 分析 + 页面确认入库（set_review 候选卡），不在浏览器自动化范围", "skip")
    shot(page, "06_extract_list")
    return True


def step_desk(page):
    """③ 写作台：列出书籍 → 进入第一本的写作/续写"""
    log("═══ 步骤3: 写作台 /desk ═══")
    page.goto(BASE + "/desk")
    shot(page, "07_desk_list")

    continue_links = page.locator("a[href*='/continue']")
    count = continue_links.count()
    log(f"写作台书籍: {count} 本")
    if count == 0:
        log("无书可进入写作，跳过", "skip")
        return False

    continue_links.first.click()
    page.wait_for_load_state("load")
    shot(page, "08_desk_entry")

    # 写作页应有「生成下一章」按钮（不实际生成，避免高额 LLM 消耗）
    try:
        wait_for(page, "#btn-generate", 20000)
        log("写作页就绪：已找到「生成下一章」按钮", "ok")
        shot(page, "09_desk_writing")
        return True
    except PWTimeout:
        log("写作页未找到生成按钮", "fail")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--book", default="十日终焉", help="要抓取的书名")
    parser.add_argument("--steps", default="all",
                        help="运行哪些步骤: all|scout|extract|desk")
    args = parser.parse_args()

    steps = ["scout", "extract", "desk"]
    if args.steps != "all":
        steps = [s for s in args.steps.split(",") if s in steps]

    # 检测服务
    import urllib.request
    try:
        urllib.request.urlopen(BASE, timeout=5)
    except Exception:
        log(f"服务未运行: {BASE}，请先启动 python ui/web_ui.py", "fail")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(viewport={"width": 1500, "height": 950})
        page = context.new_page()

        # 监听 console 错误
        page.on("console", lambda m: None)
        page.on("pageerror", lambda e: log(f"页面JS错误: {e}", "fail"))

        results = {}
        if "scout" in steps:
            results["scout"] = step_scout(page, args.book)
        if "extract" in steps:
            results["extract"] = step_extract(page)
        if "desk" in steps:
            results["desk"] = step_desk(page)

        browser.close()

    print("\n" + "=" * 60)
    print("全流程测试报告")
    print("=" * 60)
    for r in REPORT:
        mark = {"ok": "✅", "fail": "❌", "skip": "⏭️", None: "→"}[r["ok"]]
        print(f"{mark} {r['msg']}")
    print("=" * 60)
    failed = [r for r in REPORT if r["ok"] == "fail"]
    print(f"结果: {len(failed)} 处失败 / {len(REPORT)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
