# -*- coding: utf-8 -*-
"""核查：建书向导步3 故事线展示（set_outline → StoryLine 渲染）运行时验证。

对运行中的 58080 检查 /books/start：
  - 注入 set_outline 事件（用 OutlineGenerator llm_client=None 生成的真实大纲数据）
  - 断言 #wz-storyline-fieldset 显示、StoryLine Gantt 渲染（大纲/桥段条数量）
  - 无 console 错误
产物：tools/shots_ux/verify_wizard_storyline.png
"""
import sys, os, json, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from selenium import webdriver
from selenium.webdriver.edge.options import Options

BASE = "http://127.0.0.1:58080"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots_ux")
os.makedirs(OUT, exist_ok=True)

# 用真实管线生成无书大纲数据（零 LLM 成本，规则回退）
from libraries.outline_generator import OutlineGenerator  # noqa: E402
from libraries.prompt_harness import PromptHarness  # noqa: E402
from libraries.storyline import BookStoryline  # noqa: E402
from agent_tools import struct_lib, plot_lib, gag_lib, _profile_for  # noqa: E402

tl = BookStoryline(
    genre="都市", sub_genre="爽文", words_per_chapter=3000, pen_name="测试",
    basic_info={"characters": [{"name": "王小明", "role": "主角", "importance": 1,
                                "identity": "程序员", "golden_finger": "读心术"}],
                "world_building": {"description": "都市爽文开挂升级", "tags": ["都市", "爽文"],
                                   "core_conflict": "主角被系统选中，在都市中逆袭"},
                "tone": "", "target_audience": "", "pov": "第三人称", "era_language": ""},
)
profile = _profile_for(tl)
harness = PromptHarness(storyline=tl, profile=profile, gag_lib=gag_lib, plot_lib=plot_lib)
gen = OutlineGenerator(llm_client=None, structure_lib=struct_lib, plot_lib=plot_lib,
                       gag_lib=gag_lib, profile=profile, harness=harness)
for _ev in gen.generate(genre="都市", sub_genre="爽文",
                        custom_context="核心矛盾：主角被系统选中，在都市中逆袭；世界观：都市爽文开挂升级",
                        pen_name="测试", storyline=tl, skip_analyze=False, on_save=None):
    pass
d = tl.to_dict()
outline_payload = {
    "outlines": d.get("outlines", []),
    "plots": d.get("plots", []),
    "threads": d.get("threads", []),
    "themes": d.get("themes", []),
    "basic_info": d.get("basic_info", {}),
}
print(f"[data] outlines={len(outline_payload['outlines'])}, plots={len(outline_payload['plots'])}")

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

def page_errors():
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

try:
    driver.get(BASE + "/books/start")
    time.sleep(1.5)
    # 步1 填 idea/pen/tags → 进步3（StoryLine 挂载在 panel-3）
    driver.execute_script("""
        var WZ = window.WZ;
        WZ.el('wz-idea').value = '都市爽文开挂升级';
        WZ.state.idea = '都市爽文开挂升级';
        WZ.show(3);
    """)
    time.sleep(0.5)
    # 注入 set_outline 事件（模拟 agent_panel.js 命令桥：ne:command → window.onnecommand 委托）
    payload = json.dumps(outline_payload)
    driver.execute_script("""
        var payload = %s;
        window.dispatchEvent(new CustomEvent('ne:command', {detail: {
            cmd: 'set_outline', args: payload
        }}));
    """ % payload)
    time.sleep(2.0)

    fs_display = driver.execute_script(
        "return document.getElementById('wz-storyline-fieldset').style.display;")
    check("故事线 fieldset 已显示", fs_display != 'none', f"display={fs_display}")
    outline_bars = driver.execute_script(
        "return document.querySelectorAll('#wz-storyline .sl-bar-outline').length;")
    plot_bars = driver.execute_script(
        "return document.querySelectorAll('#wz-storyline .sl-bar-plot').length;")
    check("StoryLine 大纲条渲染", outline_bars >= 1, f"outline_bars={outline_bars}")
    check("StoryLine 桥段条渲染", plot_bars >= 1, f"plot_bars={plot_bars}")
    meta = driver.execute_script(
        "var m = document.querySelector('#wz-storyline .sl-meta'); return m ? m.textContent : '';")
    check("StoryLine 元信息（总字数/大纲/桥段/线程）", '大纲' in meta and '桥段' in meta, f"meta={meta.strip()[:80]}")

    driver.save_screenshot(os.path.join(OUT, "verify_wizard_storyline.png"))
    errs = page_errors()
    check("无 console 错误", len(errs) == 0, f"errs={errs[:2]}")
finally:
    driver.quit()

print("\n" + "=" * 50)
print(f"  建书向导步3 故事线核查: {'全部通过' if not FAILED else '失败: ' + '、'.join(FAILED)}")
sys.exit(1 if FAILED else 0)
