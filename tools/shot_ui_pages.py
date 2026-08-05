# -*- coding: utf-8 -*-
"""
截取全站关键页面截图（用于视觉模块 UX 分析）
用 Selenium + Edge headless，固定视口 1500x950（与 e2e 测试一致）。
截图输出到 tools/shots_ux/<index>_<name>.png
"""
import sys, os, time, urllib.parse
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By

BASE = "http://127.0.0.1:58080"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots_ux")
os.makedirs(OUT, exist_ok=True)

TL_ID = "tl_枫落_1785843989"
TL_ENC = urllib.parse.quote(TL_ID)

PAGES = [
    ("01_dashboard",   "/"),
    ("02_books",       "/books"),
    ("03_desk",        "/desk"),
    ("04_start_book",  "/books/start"),
    ("05_book_detail", "/books/book_001"),
    ("06_storyline_editor", f"/storyline/{TL_ENC}/edit"),
    ("07_storyline_detail", f"/storyline/{TL_ENC}/detail"),
    ("08_settings",    "/settings"),
    ("09_scout",       "/scout"),
    ("10_plots",       "/plots"),
    ("11_gags",        "/gags"),
]

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--disable-gpu")
opts.add_argument("--no-sandbox")
opts.add_argument("--window-size=1500,950")
opts.binary_location = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

driver = webdriver.Edge(options=opts)
driver.set_window_size(1500, 950)
errors = []

for name, path in PAGES:
    try:
        driver.get(BASE + path)
        time.sleep(2.2)
        # 等页面 JS 稳定：等 body 出现再补一下
        for _ in range(6):
            try:
                driver.execute_script("return document.readyState")
                break
            except Exception:
                time.sleep(0.5)
        time.sleep(1.0)
        out = os.path.join(OUT, f"{name}.png")
        driver.save_screenshot(out)
        # 收集页面 JS 错误（通过 performance entries 不明显，跳过）
        print("OK  ", out)
    except Exception as e:
        errors.append((name, str(e)))
        print("FAIL", name, e)

driver.quit()
print("\n完成. 截图:", len(PAGES) - len(errors), "失败:", len(errors))
for n, e in errors:
    print("  ", n, e)
