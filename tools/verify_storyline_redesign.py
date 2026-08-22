# -*- coding: utf-8 -*-
"""核查：故事线重设计（删章节轴 + 设计令牌迁移）在真实页面上的运行时验证（只读）。

对运行中的 58080 检查两个页面：
  - /books/book_002  书详情页内嵌 Gantt（默认折叠，需点 #sl-toggle 展开后渲染）
  - /books/book_002/continue  写作台 Gantt（页面加载即渲染）

断言：Gantt 渲染成功、大纲/桥段条数量、无 .sl-chapter 节点、图例无「章节」、
字数轴存在、点击 dispatch sl:plot-click、StoryLine.highlight/setZoom 可用、无 console 错误。
产物：tools/shots_ux/verify_book_detail.png、verify_write_flow.png
"""
import sys, os, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE = "http://127.0.0.1:58080"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots_ux")
os.makedirs(OUT, exist_ok=True)

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--disable-gpu")
opts.add_argument("--no-sandbox")
opts.add_argument("--window-size=1500,950")
opts.binary_location = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
opts.set_capability("goog:loggingPrefs", {"browser": "ALL"})

driver = webdriver.Edge(options=opts)
driver.set_window_size(1500, 950)

FAILED = []

def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"[{tag}] {name}  {detail}")

def page_errors(page):
    """收集页面 console 的 SEVERE/ERROR 日志（忽略 favicon）。"""
    errs = []
    try:
        for e in driver.get_log("browser"):
            lvl = (e.get("level") or "").upper()
            msg = str(e.get("message") or "")
            if lvl in ("SEVERE", "ERROR") and "favicon" not in msg.lower():
                errs.append(msg[:300])
    except Exception:
        pass
    return errs

def assert_gantt(prefix):
    """对当前页面的 Gantt 做通用断言。prefix = 挂载点前缀，如 #detail-storyline / #editor-storyline。"""
    js = driver.execute_script(f"""
      var m = document.querySelector('{prefix} .sl-root');
      var out = {{
        root: !!m,
        outlines: document.querySelectorAll('{prefix} .sl-bar-outline').length,
        plots:    document.querySelectorAll('{prefix} .sl-bar-plot').length,
        threads:  document.querySelectorAll('{prefix} .sl-bar-thread').length,
        chapterNodes: document.querySelectorAll('{prefix} [class*="sl-chapter"]').length,
        axis:     !!document.querySelector('{prefix} .sl-axis-panel'),
        axisLabels: Array.from(document.querySelectorAll('{prefix} .sl-tick-label')).slice(0,5).map(function(n){{return n.textContent;}}),
        legendText: (function(){{var l=document.querySelector('{prefix} .sl-legend'); return l?l.innerText:'';}})()
      }};
      return out;
    """)
    check(f"{prefix} gantt_root", js.get("root"), "")
    check(f"{prefix} outlines>=2", (js.get("outlines") or 0) >= 2, f"outlines={js.get('outlines')}")
    check(f"{prefix} plots>=19", (js.get("plots") or 0) >= 19, f"plots={js.get('plots')}")
    check(f"{prefix} threads>=1", (js.get("threads") or 0) >= 1, f"threads={js.get('threads')}")
    check(f"{prefix} no_sl_chapter_nodes", (js.get("chapterNodes") or 0) == 0, f"chapterNodes={js.get('chapterNodes')}")
    check(f"{prefix} axis_panel", js.get("axis"), "")
    check(f"{prefix} legend_no_chapter", "章节" not in (js.get("legendText") or ""), f"legendText={js.get('legendText')!r}"[:120])

def smoke_interactions(prefix):
    """交互冒烟：sl:plot-click 事件 + StoryLine.highlight/setZoom。"""
    fired = driver.execute_script(f"""
      var fired = null;
      var bar = document.querySelector('{prefix} .sl-bar-plot');
      if (!bar) return 'no-bar';
      document.addEventListener('sl:plot-click', function(e){{ fired = e.detail; }});
      bar.click();
      return fired;
    """)
    check(f"{prefix} plot_click_dispatch", isinstance(fired, dict) and fired.get("plot_id"), f"fired={fired}")

    hl = driver.execute_script(f"""
      var bar = document.querySelector('{prefix} .sl-bar-plot');
      var pid = bar && bar.getAttribute('data-pid');
      if (!pid) return 'no-pid';
      window.StoryLine.highlight({{plot_id: pid}});
      return document.querySelectorAll('{prefix} .sl-bar.sl-highlight').length;
    """)
    check(f"{prefix} highlight_works", (hl or 0) >= 1, f"highlighted={hl}")

    zoom = driver.execute_script(f"""
      window.StoryLine.setZoom(2);
      var label = document.querySelector('{prefix} .sl-zoom-label');
      return label ? label.textContent : 'no-label';
    """)
    check(f"{prefix} zoom_works", zoom == "200%", f"zoomLabel={zoom}")

try:
    # ─── 1) 书详情页 ───
    driver.get(BASE + "/books/book_002")
    time.sleep(2.0)
    check("detail_page_rendered",
          driver.execute_script("return document.body && document.body.innerText.indexOf('我，游戏AI本尊') >= 0"),
          "书名出现在详情页")
    # 展开故事线 accordion（Gantt 首次展开才初始化）
    try:
        WebDriverWait(driver, 8).until(EC.element_to_be_clickable((By.ID, "sl-toggle"))).click()
        WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#detail-storyline .sl-root")))
        check("detail_sl_toggle_expand", True, "")
    except Exception as e:
        check("detail_sl_toggle_expand", False, str(e)[:200])
    time.sleep(0.8)
    assert_gantt("#detail-storyline")
    errs = page_errors("detail")
    check("detail_no_console_errors", len(errs) == 0, "; ".join(errs[:3]))
    driver.save_screenshot(os.path.join(OUT, "verify_book_detail.png"))
    print("saved verify_book_detail.png")

    # ─── 2) 写作台页 ───
    driver.get(BASE + "/books/book_002/continue")
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#editor-storyline .sl-root")))
        check("write_flow_gantt_rendered", True, "")
    except Exception as e:
        check("write_flow_gantt_rendered", False, str(e)[:200])
    time.sleep(0.8)
    assert_gantt("#editor-storyline")
    smoke_interactions("#editor-storyline")
    errs = page_errors("write_flow")
    check("write_flow_no_console_errors", len(errs) == 0, "; ".join(errs[:3]))
    driver.save_screenshot(os.path.join(OUT, "verify_write_flow.png"))
    print("saved verify_write_flow.png")

except Exception as e:
    check("runner", False, f"异常: {e}")
    import traceback; traceback.print_exc()
finally:
    driver.quit()

print("\n=== 结果汇总 ===")
print("全部通过" if not FAILED else f"失败项: {FAILED}")
sys.exit(1 if FAILED else 0)
