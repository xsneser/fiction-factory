# -*- coding: utf-8 -*-
"""核查：故事线升级（弧树嵌套 / 设局→收局连线 / 线程泳道增强 / narrative 目标 / 缺 id 容错）运行时验证（只读）。

对运行中的 58080：
  1) 在书详情页注入带完整弧树 + 设局/收局数据的 mock storyline → StoryLine.init → 断言新渲染：
     .sl-bar-outline.level-1（子弧）、svg path[stroke-dasharray]（设局→收局虚线）、
     .sl-setup-badge（设局徽标）、.sl-thread-point(.setup/.payoff)（线程点）、
     .sl-target-mark（叙事目标）、图例含「设局」。
  2) /books/book_001、/books/book_002 存量书（threads 缺 id / outline·plot id 全空）渲染无 console 错误。
产物：tools/shots_ux/upgrade_mock.png、upgrade_legacy_001.png、upgrade_legacy_002.png
"""
import sys, os, time, json
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

# mock：弧级大纲（arc1 含子弧 arc1a、interleaved + narrative_target）+ 2 对设局→收局 + 3 线程
MOCK = {
    "book_title": "《弧树测试》",
    "words_per_chapter": 3000,
    "outlines": [
        {"id": "arc1", "template_id": "", "name": "末日来临前囤物资", "start_chapter": 1, "end_chapter": 12,
         "narrative": "interleaved", "narrative_target": "期限前攒够资源", "parent_arc_id": ""},
        {"id": "arc1a", "template_id": "", "name": "变卖资产换现金", "start_chapter": 1, "end_chapter": 5, "parent_arc_id": "arc1"},
        {"id": "arc2", "template_id": "", "name": "末日爆发·生存重建", "start_chapter": 13, "end_chapter": 30, "parent_arc_id": ""},
    ],
    "plots": [
        {"id": "p1", "template_id": "", "name": "发现末日征兆", "outline_id": "arc1", "order": 1, "thread_id": "主线"},
        {"id": "p2", "template_id": "", "name": "疯狂采购", "outline_id": "arc1", "order": 2, "thread_id": "主线"},
        {"id": "p3", "template_id": "", "name": "变卖家产（设局）", "outline_id": "arc1a", "order": 1, "thread_id": "副线"},
        {"id": "p4", "template_id": "", "name": "与竞争者抢货", "outline_id": "arc1", "order": 3, "thread_id": "主线"},
        {"id": "p5", "template_id": "", "name": "末日源头阴谋（设局）", "outline_id": "arc1", "order": 4, "thread_id": "伏笔线"},
        {"id": "p6", "template_id": "", "name": "收局：阴谋揭晓", "outline_id": "arc2", "order": 1,
         "thread_id": "伏笔线", "resolves_plot_id": "p5", "resolves_name": "末日源头阴谋（设局）"},
        {"id": "p7", "template_id": "", "name": "收局：资产变现", "outline_id": "arc1a", "order": 2,
         "thread_id": "副线", "resolves_plot_id": "p3", "resolves_name": "变卖家产（设局）"},
    ],
    "threads": [
        {"id": "主线", "name": "主线", "desc": "囤积求生"},
        {"id": "副线", "name": "副线", "desc": "家人对疯狂囤货的怀疑"},
        {"id": "伏笔线", "name": "伏笔线", "desc": "末日源头阴谋"},
    ],
    "promises": [
        {"id": "pr1", "setup_plot_id": "p5", "type": "mystery", "desc": "末日源头阴谋",
         "status": "pending", "deadline_chapter": 28, "payoff_plot_id": "p6", "payoff_chapter": 28},
    ],
    "phase": "ready",
}

try:
    # ─── 1) mock 注入渲染：新特性 DOM 断言 ───
    driver.get(BASE + "/books/book_002")
    time.sleep(1.5)
    js_out = driver.execute_script("""
      var mock = %s;
      // 需要 story_line.js 已加载
      if (!window.StoryLine) return {error: 'StoryLine not loaded'};
      var mount = document.createElement('div');
      mount.id = 'upgrade-mock-mount';
      mount.style.cssText = 'height:900px; margin:10px;';
      document.body.appendChild(mount);
      window.StoryLine.init('upgrade-mock-mount', mock, {scrollable: true});
      return {
        nestedArc: document.querySelectorAll('#upgrade-mock-mount .sl-bar-outline.level-1').length,
        setupBadges: document.querySelectorAll('#upgrade-mock-mount .sl-setup-badge').length,
        payoffBadges: document.querySelectorAll('#upgrade-mock-mount .sl-payoff-badge').length,
        payoffPaths: document.querySelectorAll('#upgrade-mock-mount svg path[stroke-dasharray]').length,
        threadPoints: document.querySelectorAll('#upgrade-mock-mount .sl-thread-point').length,
        threadPointSetup: document.querySelectorAll('#upgrade-mock-mount .sl-thread-point.setup').length,
        threadPointPayoff: document.querySelectorAll('#upgrade-mock-mount .sl-thread-point.payoff').length,
        targetMarks: document.querySelectorAll('#upgrade-mock-mount .sl-target-mark').length,
        legend: (function(){var l=document.querySelector('#upgrade-mock-mount .sl-legend');return l?l.innerText:'';})(),
        arcBars: document.querySelectorAll('#upgrade-mock-mount .sl-bar-outline').length,
        plotBars: document.querySelectorAll('#upgrade-mock-mount .sl-bar-plot').length,
        threadBands: document.querySelectorAll('#upgrade-mock-mount .sl-bar-thread').length,
      };
    """ % json.dumps(MOCK, ensure_ascii=False))
    if "error" in js_out:
        check("mock_storyline_loaded", False, str(js_out.get("error")))
    else:
        check("mock_arc_bars", (js_out.get("arcBars") or 0) == 3, f"arcBars={js_out.get('arcBars')}")
        check("mock_nested_arc_level1", (js_out.get("nestedArc") or 0) >= 1, f"nestedArc={js_out.get('nestedArc')}")
        check("mock_setup_badges", (js_out.get("setupBadges") or 0) >= 2, f"setupBadges={js_out.get('setupBadges')}")
        check("mock_payoff_badges", (js_out.get("payoffBadges") or 0) >= 2, f"payoffBadges={js_out.get('payoffBadges')}")
        check("mock_payoff_paths", (js_out.get("payoffPaths") or 0) >= 2, f"payoffPaths={js_out.get('payoffPaths')}")
        check("mock_thread_points", (js_out.get("threadPoints") or 0) >= 4, f"threadPoints={js_out.get('threadPoints')}")
        check("mock_thread_point_setup", (js_out.get("threadPointSetup") or 0) >= 2, f"setup={js_out.get('threadPointSetup')}")
        check("mock_thread_point_payoff", (js_out.get("threadPointPayoff") or 0) >= 2, f"payoff={js_out.get('threadPointPayoff')}")
        check("mock_target_marks", (js_out.get("targetMarks") or 0) >= 1, f"targetMarks={js_out.get('targetMarks')}")
        check("mock_legend_setup", "设局" in (js_out.get("legend") or ""), f"legend={js_out.get('legend')!r}"[:120])
    errs = page_errors("mock")
    check("mock_no_console_errors", len(errs) == 0, "; ".join(errs[:3]))
    driver.execute_script("document.getElementById('upgrade-mock-mount').scrollIntoView({block:'start'});")
    time.sleep(0.5)
    driver.save_screenshot(os.path.join(OUT, "upgrade_mock.png"))
    print("saved upgrade_mock.png")

    # ─── 2) 存量书容错：book_001 / book_002（threads 缺 id、outline/plot id 全空） ───
    for bid, fname in (("book_001", "upgrade_legacy_001.png"), ("book_002", "upgrade_legacy_002.png")):
        driver.get(BASE + f"/books/{bid}")
        time.sleep(1.5)
        try:
            WebDriverWait(driver, 8).until(EC.element_to_be_clickable((By.ID, "sl-toggle"))).click()
            WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#detail-storyline .sl-root")))
            rendered = driver.execute_script("""
              return {
                root: !!document.querySelector('#detail-storyline .sl-root'),
                plots: document.querySelectorAll('#detail-storyline .sl-bar-plot').length,
                threads: document.querySelectorAll('#detail-storyline .sl-bar-thread').length,
              };
            """)
            check(f"{bid}_legacy_renders", rendered.get("root"), str(rendered))
        except Exception as e:
            check(f"{bid}_legacy_renders", False, str(e)[:200])
        errs = page_errors(f"{bid}_legacy")
        check(f"{bid}_legacy_no_console_errors", len(errs) == 0, "; ".join(errs[:3]))
        driver.save_screenshot(os.path.join(OUT, fname))
        print(f"saved {fname}")

except Exception as e:
    check("runner", False, f"异常: {e}")
    import traceback; traceback.print_exc()
finally:
    driver.quit()

print("\n=== 结果汇总 ===")
print("全部通过" if not FAILED else f"失败项: {FAILED}")
sys.exit(1 if FAILED else 0)
