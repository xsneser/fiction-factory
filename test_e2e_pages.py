#!/usr/bin/env python3
"""
NovelEngine E2E 页面一致性测试
自动启动 Web 面板 (ui/web_ui.py) → 测试所有页面路由/链接/内容完整性 → 关闭服务

用法:
  python test_e2e_pages.py            # 自动启动服务并测试
  python test_e2e_pages.py --no-start # 复用已在 localhost:58080 运行的服务
"""
import argparse
import os
import subprocess
import sys
import time
import re
from urllib.parse import urljoin

import requests

# Windows 控制台默认 GBK，直接 print 中文/emoji 会崩，强制走 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://localhost:58080"
HOST = os.environ.get("NOVEL_HOST", "127.0.0.1")
PORT = 58080

s = requests.Session()
errors = []


def check(label, condition, detail=""):
    if not condition:
        msg = f"FAIL [{label}]: {detail}"
        errors.append(msg)
        print(f"  [FAIL] {msg}")
    else:
        print(f"  [OK] {label}")


def get(path):
    r = s.get(urljoin(BASE, path), timeout=15)
    return r


def wait_server(proc=None, timeout=30):
    """等待服务就绪（web_ui.py 是 Flask 面板，无 /api/health，用首页探测）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(BASE + "/", timeout=2)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        if proc is not None and proc.poll() is not None:
            print(f"  [FATAL] 服务进程提前退出: {proc.returncode}")
            return False
        time.sleep(0.5)
    return False


def start_server():
    """启动 ui/web_ui.py 子进程"""
    root = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, NOVEL_ENGINE_DIR=root, NOVEL_HOST=HOST)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(root, "ui", "web_ui.py")],
        cwd=root, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-start", action="store_true",
                        help="不自动启动服务，复用已有 localhost:58080")
    args = parser.parse_args()

    proc = None
    if not args.no_start:
        print(f"🚀 启动 Web 面板 ({BASE}) ...")
        proc = start_server()
        if not wait_server(proc):
            print("服务启动失败")
            stop_server(proc)
            sys.exit(1)
        print("✅ 服务就绪")
    else:
        if not wait_server():
            print(f"❌ {BASE} 无服务在运行，且指定 --no-start")
            sys.exit(1)

    print("=" * 60)
    print("NovelEngine E2E Page Test")
    print("=" * 60)

    try:
        run_tests()
    finally:
        if not args.no_start:
            stop_server(proc)

    # ═══ Report ═══
    print("\n" + "=" * 60)
    if errors:
        print(f"FAILED: {len(errors)} issues found")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("ALL CHECKS PASSED")
        sys.exit(0)


def run_tests():
    # ═══ Check server alive ═══
    r = get("/")
    check("Server alive", r.status_code == 200, f"status={r.status_code}")
    if r.status_code != 200:
        print("Server not running - aborting")
        sys.exit(1)

    # ═══ Test all pages ═══
    pages = [
        ("Dashboard", "/", ["NovelEngine", "仪表盘"]),
        ("Books", "/books", ["书库", "book"]),
        ("Start New Book", "/books/start", ["启动新书", "form"]),
        ("Writing Desk", "/desk", ["写作台"]),
        ("Plots", "/plots", ["桥段库", "plot"]),
        ("Structures", "/structures", ["大纲库", "structure"]),
        ("Gags", "/gags", ["笑点库", "gag"]),
        ("Themes", "/themes", ["内涵库", "theme"]),
        ("Profiles", "/profiles", ["笔名档案", "profile"]),
        ("New Profile", "/profiles/new", ["创建笔名", "form"]),
        ("Settings", "/settings", ["设置", "api"]),
        ("Extract", "/extract", ["内容提取", "extract"]),
        ("DeAI Test", "/deai", ["去AI", "测试"]),
        ("Review Test", "/review-test", ["审查", "测试"]),
        ("Scout", "/scout", ["抓取", "scout"]),
    ]

    print("\n--- Page Routes ---")
    for name, path, keywords in pages:
        r = get(path)
        check(f"{name} ({path}) → {r.status_code}",
              r.status_code == 200,
              f"got {r.status_code}")
        if r.status_code == 200:
            for kw in keywords:
                check(f"  '{kw}' in {name}",
                      kw.lower() in r.text.lower(),
                      f"keyword '{kw}' not found")

    # ═══ 大纲生成器已内嵌到启动新书：/books/generator 应重定向 ═══
    print("\n--- Generator redirect ---")
    r = get("/books/generator")
    check("Generator → /books/start (302)",
          r.status_code == 200 and "/books/start" in r.url,
          f"final={r.url}")
    if r.status_code == 200:
        check("  '启动新书' in redirect target",
              "启动新书" in r.text,
              "keyword not found in redirect target")

    # ═══ API endpoints（web_ui.py 是 Flask 面板，无 /api/health） ═══
    print("\n--- API Endpoints ---")
    apis = [
        ("Tasks", "/api/status/tasks"),
    ]

    for name, path in apis:
        r = get(path)
        check(f"{name} ({path}) → {r.status_code}",
              r.status_code == 200,
              f"got {r.status_code}")

    # ═══ Sidebar consistency ═══
    print("\n--- Sidebar Navigation ---")
    expected_links = [
        ("/", "仪表盘"),
        ("/books/start", "启动新书"),
        ("/books", "书库"),
        ("/desk", "写作台"),
        ("/plots", "桥段库"),
        ("/structures", "大纲库"),
        ("/gags", "笑点库"),
        ("/themes", "内涵库"),
        ("/profiles", "笔名档案"),
        ("/settings", "设置"),
        ("/scout", "小说抓取"),
        ("/extract", "内容提取"),
        ("/deai", "去AI测试"),
        ("/review-test", "审查测试"),
    ]

    r = get("/")
    sidebar_links = re.findall(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', r.text)
    found_links = {href: text.strip() for href, text in sidebar_links}

    for href, expected_text in expected_links:
        check(f"Sidebar link: {expected_text}",
              href in found_links or any(expected_text in t for t in found_links.values()),
              f"link to {href} with text '{expected_text}' not in sidebar")

    # ═══ Book workflow consistency ═══
    print("\n--- Book Workflow ---")
    import glob
    book_ids = sorted(os.path.basename(p.rstrip('/\\'))
                      for p in glob.glob("books/book_*"))

    if book_ids:
        for bid in book_ids[:3]:
            r = get(f"/books/{bid}")
            check(f"Book detail ({bid})", r.status_code == 200,
                  f"got {r.status_code}")

            r = get(f"/books/{bid}/continue")
            check(f"Write flow page ({bid})", r.status_code == 200,
                  f"got {r.status_code}")

            if r.status_code == 200:
                check(f"Write flow title in page",
                      "蓝图式写作" in r.text or bid in r.text,
                      f"write flow marker not found for {bid}")
    else:
        print("  (no books found - skipping book tests)")

    # ═══ CSS/JS consistency ═══
    print("\n--- Style Consistency ---")
    css_classes = ['.card', '.btn-primary', '.tag', '.progress-bar', '.accordion']
    for page_path in ["/", "/books", "/scout"]:
        r = get(page_path)
        if r.status_code == 200:
            for cls in css_classes:
                check(f"  {page_path} has {cls}",
                      cls in r.text,
                      f"missing {cls}")


if __name__ == "__main__":
    main()
