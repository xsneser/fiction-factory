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
    # 步1 填 idea → 跳步3（StoryLine 挂载在 panel-3）
    driver.execute_script("""
        var WZ = window.WZ;
        WZ.el('wz-idea').value = '都市爽文开挂升级';
        WZ.state.idea = '都市爽文开挂升级';
        WZ._worldFilled = true;   // 抑制 show(3) 触发 fillWorld（单线程服务器会被阻塞，拖住轮询）
        WZ.show(3);
    """)
    time.sleep(0.5)
    # 常驻模块：未生成时 fieldset 显示 + 空态/生成按钮可见 + Gantt 挂载隐藏
    fs0 = driver.execute_script(
        "return document.getElementById('wz-storyline-fieldset').style.display;")
    btn0 = driver.execute_script(
        "var b=document.getElementById('wz-gen-storyline'); return b ? b.style.display : 'MISSING';")
    sl0 = driver.execute_script(
        "var s=document.getElementById('wz-storyline'); return s ? s.style.display : 'MISSING';")
    check("常驻故事线模块显示（未生成：空态+按钮）",
          fs0 != 'none' and btn0 != 'MISSING' and btn0 != 'none',
          f"fs={fs0} btn={btn0}")
    check("空态下 Gantt 挂载隐藏", sl0 == 'none', f"sl={sl0}")

    # 真实路径：drive_ui(set_outline) → nav_intent.json → 浏览器轮询(2.5s) → ne:command 分发
    from libraries.nav_intent import push_ui_command
    push_ui_command("set_outline", outline_payload)
    print("[intent] 已写入 nav_intent.json（真实 drive_ui 路径），等待浏览器轮询…")
    time.sleep(5.0)   # 轮询间隔 2.5s + 渲染余量

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
    btn1 = driver.execute_script(
        "var b=document.getElementById('wz-gen-storyline'); return b ? b.style.display : 'MISSING';")
    check("生成后生成按钮隐藏", btn1 == 'none', f"btn={btn1}")

    driver.save_screenshot(os.path.join(OUT, "verify_wizard_storyline.png"))
    errs = page_errors()
    check("无 console 错误", len(errs) == 0, f"errs={errs[:2]}")

    # 第6项：generate_outline_preview 服务端直推 set_outline（镜像 world_candidates→add_candidate）。
    # 大纲载荷常 >8KB，经 dsh 核心 tool-result-pruner（thresholdChars=8192）会被裁成 head/tail 残片、
    # 模型拿不到全量无法回传；由工具自身直推 nav_intent，浏览器 busy 中也消费（agent_panel.js）。
    # 绕开 LLM：把 _require_llm patch 成 None → OutlineGenerator 走规则回退管线（零 LLM 成本）。
    import agent_tools as _at
    from unittest import mock as _mock
    from core.json_store import read_json
    with _mock.patch.object(_at, "_require_llm", return_value=None):
        _at.generate_outline_preview(idea="都市爽文开挂升级", genre="都市", sub_genre="爽文",
                                     tags=["都市", "爽文"],
                                     core_conflict="主角被系统选中，在都市中逆袭",
                                     pen_name="测试", words_per_chapter=3000)
    intents = read_json(os.path.join(_ROOT, "storage", "nav_intent.json"), []) or []
    pushed = [i for i in intents
              if i.get("kind") == "ui_command" and i.get("cmd") == "set_outline"]
    outlines_n = len(pushed[-1]["args"]["outlines"]) if pushed else 0
    check("generate_outline_preview 服务端直推 set_outline",
          bool(pushed) and outlines_n >= 1, f"pushed={len(pushed)} outlines={outlines_n}")
    from libraries.nav_intent import take_nav_intents
    take_nav_intents()   # 排空残留意图，防下次运行浏览器误消费旧 set_outline
finally:
    driver.quit()

print("\n" + "=" * 50)
print(f"  建书向导步3 故事线核查: {'全部通过' if not FAILED else '失败: ' + '、'.join(FAILED)}")
sys.exit(1 if FAILED else 0)
