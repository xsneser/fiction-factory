#!/usr/bin/env python3
"""
NovelEngine 全流程浏览器自动化测试
模拟用户点击：爬取(scout) → 提取(extract) → 大纲(generator) → 填充(写作台)

依赖: playwright (pip install playwright && playwright install chromium)
用法: python tools/e2e_flow_test.py [--headless] [--book 书名]
"""
import argparse
import json
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
    """① 爬取：搜索 → 下载。下载完成靠侧边栏任务状态确认（scout页只下载，不分析）"""
    log("═══ 步骤1: 爬取 /scout ═══")
    page.goto(BASE + "/scout")
    shot(page, "01_scout_initial")

    # 填书名
    page.fill("#book-title", book_title)
    page.fill("#chapters", str(chapters))
    shot(page, "02_scout_filled")

    # 点开始抓取
    page.click("#btn-scout")
    log("已点击「开始抓取」，等待下载...")

    # 观察侧边栏任务状态：等待"下载完成"或按钮恢复
    deadline = time.time() + 180
    download_ok = False
    while time.time() < deadline:
        # 侧边栏任务相位
        phases = page.locator(".task-phase").all_text_contents()
        if any("下载完成" in p for p in phases):
            download_ok = True
            break
        # 按钮恢复 disabled=None
        if page.locator("#btn-scout").get_attribute("disabled") is None:
            # 等 2 秒确认任务状态
            time.sleep(2)
            phases = page.locator(".task-phase").all_text_contents()
            if any("下载完成" in p for p in phases):
                download_ok = True
                break
        # 任务日志报错
        log_text = page.locator("#task-log-list div").all_text_contents()
        if any("失败" in t or "错误" in t for t in log_text):
            log(f"任务日志报错: {[t for t in log_text if '失败' in t or '错误' in t][-1][:80]}", "fail")
            break
        time.sleep(3)
    shot(page, "03_scout_done")
    log(f"爬取下载: 完成={download_ok}（任务相位: {[p for p in phases][-1][:40] if phases else '无'}）")
    return download_ok


def step_extract(page):
    """② 提取：资产库提取"""
    log("═══ 步骤2: 提取 /extract ═══")
    page.goto(BASE + "/extract")
    shot(page, "04_extract_initial")

    # 等待已下载小说列表
    wait_for(page, "#library-novels-list .card", 20000)
    novels = page.locator("#library-novels-list .card").count()
    log(f"已下载小说列表: {novels} 本")
    if novels == 0:
        log("无可提取小说，跳过提取", "skip")
        return False

    # 点击第一本小说的"提取"按钮
    first = page.locator("#library-novels-list .card").first
    btn = first.locator("button").first
    btn_text = btn.text_content() or ""
    btn.click()
    log(f"点击提取按钮: {btn_text.strip()}")
    shot(page, "05_extract_running")

    # 等待结果区出现（SSE）
    deadline = time.time() + 120
    done = False
    while time.time() < deadline:
        if page.locator("#extract-result-area").count() and page.locator("#extract-result-area").is_visible():
            # 检查是否有入库按钮（说明分析完成）
            if page.locator("#extract-result-area .btn-primary").count():
                done = True
                break
        time.sleep(3)
    shot(page, "06_extract_result")
    log(f"提取完成: {done}")
    return done


def step_generator(page, genre="都市", sub_genre="系统流"):
    """③ 大纲生成：5 阶段 LLM 管线"""
    log("═══ 步骤3: 大纲生成 /books/generator ═══")
    page.goto(BASE + "/books/generator")
    shot(page, "07_gen_initial")

    # 选流派
    page.select_option("#ggenre", genre)
    page.select_option("#gsub", sub_genre)
    shot(page, "08_gen_filled")

    # 点击生成（找到提交按钮）
    page.click("#genBtn")
    log("已点击生成，等待 5 阶段...")
    shot(page, "09_gen_running")

    # 等待阶段推进（phase-item done）或日志出现
    deadline = time.time() + 240
    phases_done = 0
    gen_stat = ""
    while time.time() < deadline:
        done_items = page.locator(".phase-item.done").count()
        if done_items > phases_done:
            phases_done = done_items
            log(f"已完成阶段: {done_items}/5")
        # 保存按钮出现 → 生成完成
        if page.locator("#gSaveBtn").is_visible():
            gen_stat = page.locator("#gStats").text_content() or ""
            break
        time.sleep(3)
    shot(page, "10_gen_done")
    log(f"大纲生成: 阶段完成 {phases_done}/5, 统计: {gen_stat[:60]}")
    return phases_done >= 5


def step_desk(page):
    """④ 填充：写作台"""
    log("═══ 步骤4: 写作台 /desk ═══")
    page.goto(BASE + "/desk")
    shot(page, "11_desk_list")

    # 检查是否有时间线
    desk_items = page.locator("a[href*='/timeline/'][href*='/desk']").count()
    log(f"写作台时间线条目: {desk_items}")
    if desk_items == 0:
        log("无时间线可进入，跳过填充", "skip")
        return False

    # 进入第一个写作台
    page.locator("a[href*='/timeline/'][href*='/desk']").first.click()
    page.wait_for_load_state("load")
    shot(page, "12_desk_entry")

    # 点击构建按钮
    build_btns = page.locator("button[id*='build'], button[onclick*='build']")
    log(f"构建按钮: {build_btns.count()} 个")
    if build_btns.count() == 0:
        # 找任何主按钮
        build_btns = page.locator(".btn-primary")
    if build_btns.count() == 0:
        log("无构建按钮，跳过", "skip")
        return False
    build_btns.first.click()
    log("已点击构建")
    shot(page, "13_desk_building")

    # 等待完成（构建按钮消失/状态显示完成）
    deadline = time.time() + 180
    done = False
    while time.time() < deadline:
        status = page.locator("#build-status, #gen-status").text_content() or ""
        if "完成" in status or "成功" in status:
            done = True
            break
        # 构建按钮重新出现
        if build_btns.first.is_visible() and page.locator("body").text_content().count("构建完成"):
            done = True
            break
        time.sleep(3)
    shot(page, "14_desk_done")
    log(f"写作台构建完成: {done}")
    return done


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--book", default="十日终焉", help="要抓取的书名")
    parser.add_argument("--steps", default="all",
                        help="运行哪些步骤: all|scout|extract|generator|desk")
    args = parser.parse_args()

    steps = ["scout", "extract", "generator", "desk"]
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
        if "generator" in steps:
            results["generator"] = step_generator(page)
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
