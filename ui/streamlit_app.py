"""
NovelEngine — Streamlit Web UI (完整版)
所有页面对齐 show-me-the-story v3
"""
import streamlit as st
import requests
import os
import json

API = os.environ.get("API_BASE", "http://localhost:58080")

def api(method, path, data=None, timeout=60):
    url = f"{API}{path}"
    try:
        if method == "GET": r = requests.get(url, timeout=30)
        elif method == "POST": r = requests.post(url, json=data, timeout=timeout)
        elif method == "PUT": r = requests.put(url, json=data, timeout=30)
        elif method == "DELETE": r = requests.delete(url, timeout=30)
        else: return None
        if r.status_code >= 400: st.error(f"{r.status_code}: {r.text[:200]}"); return None
        return r.json()
    except Exception as e: st.error(f"连接失败: {e}"); return None

st.set_page_config(page_title="NovelEngine", page_icon="📖", layout="wide")

# ─── Sidebar ───
st.sidebar.title("📖 NovelEngine")

projs = api("GET", "/api/projects") or []
names = [p["name"] for p in projs]
cur = api("GET", "/api/projects/current") or {}

if st.sidebar.button("🔄 刷新"):
    st.rerun()

sel = st.sidebar.selectbox("项目", [""] + names, index=names.index(cur["name"]) + 1 if cur.get("name") in names else 0)

new_name = st.sidebar.text_input("新建项目", placeholder="输入名称...")
if st.sidebar.button("创建") and new_name:
    api("POST", "/api/projects", {"name": new_name, "language": "zh"})
    st.rerun()

if sel and sel != cur.get("name"):
    api("POST", "/api/projects/select", {"name": sel})
    st.rerun()

if not sel: st.info("请选择项目"); st.stop()

# ─── Pages ───
page = st.sidebar.radio("", ["⚙️ 配置", "📋 大纲", "✍️ 写作", "🔮 伏笔", "👤 角色", "🌍 世界观", "📊 进度"])
status = api("GET", "/api/status") or {}
st.sidebar.caption(f"状态: {status.get('phase','?')} | {sel}")

# ======== 配置 ========
if page == "⚙️ 配置":
    st.header("⚙️ 配置")

    tab1, tab2 = st.tabs(["API", "故事"])

    with tab1:
        ac = api("GET", "/api/config/api") or {}
        with st.form("api_form"):
            c1, c2 = st.columns(2)
            base = c1.text_input("API地址", ac.get("base_url",""), placeholder="https://api.deepseek.com")
            model = c2.text_input("模型", ac.get("model",""), placeholder="deepseek-chat")
            key = st.text_input("API Key", type="password", placeholder="留空不修改")
            timeout = st.number_input("超时(秒)", value=ac.get("http_timeout_seconds",300))
            budget = st.number_input("上下文预算(tokens)", value=ac.get("context_budget_tokens",300000))
            if st.form_submit_button("保存"):
                d = {"base_url": base, "model": model, "http_timeout_seconds": timeout,
                     "context_budget_tokens": budget}
                if key: d["api_key"] = key
                api("PUT", "/api/config/api", d)
                st.success("✅ 已保存")

        if st.button("🧪 测试连接"):
            r = requests.post(f"{API}/api/config/api/test",
                              json={"api_key": ac.get("api_key",""), "base_url": base, "model": model},
                              timeout=15)
            if r.status_code == 200: st.success(f"✅ {r.json().get('sample','')}")
            else: st.error(f"❌ {r.text[:200]}")

    with tab2:
        sc = (api("GET", "/api/config/story") or {}).get("story", {})
        with st.form("story_form"):
            title = st.text_input("书名", sc.get("title",""))
            c1, c2 = st.columns(2)
            stype = c1.text_input("类型", sc.get("type",""), placeholder="都市异能")
            ch_count = c2.number_input("章节数", value=sc.get("chapter_count",12), min_value=3, max_value=500)
            tw = st.number_input("每章字数", value=sc.get("target_words_per_chapter",3000), min_value=500, max_value=20000)
            style = st.text_input("风格", sc.get("writing_style",""), placeholder="节奏明快")
            pov = st.text_input("视角", sc.get("writing_pov",""), placeholder="第三人称")
            syn = st.text_area("梗概", sc.get("story_synopsis",""), height=80)
            if st.form_submit_button("保存"):
                api("PUT", "/api/config/story", {"type": stype, "title": title, "chapter_count": ch_count,
                     "target_words_per_chapter": tw, "writing_style": style,
                     "writing_pov": pov, "story_synopsis": syn})
                st.success("✅ 已保存")

# ======== 大纲 ========
elif page == "📋 大纲":
    st.header("📋 大纲")
    prog = api("GET", "/api/progress") or {}
    chs = prog.get("chapters", [])
    phase = prog.get("phase", "outline")

    c1, c2, c3, c4, c5 = st.columns(5)
    if c1.button("🚀 生成", disabled=phase!="outline"): 
        with st.spinner("生成中..."): 
            if api("POST","/api/outline/generate",timeout=90): st.rerun()
    if c2.button("✅ 确认", disabled=phase!="outline" or not chs):
        api("POST","/api/outline/confirm"); st.rerun()
    if c3.button("🔄 修订", disabled=not chs or phase!="outline"):
        api("POST","/api/outline/revise",{"feedback":"请修订大纲"}, timeout=90); st.rerun()
    if c4.button("➕ 追加", disabled=not chs):
        api("POST","/api/outline/continuation",{"chapter_count":5}, timeout=90); st.rerun()
    if c5.button("🗑 删除", disabled=not chs):
        api("DELETE","/api/outline"); st.rerun()

    if chs:
        st.subheader(f"📖 {prog.get('title','(未命名)')}")
        for ch in chs:
            with st.expander(f"第{ch['num']}章 {ch['title']} [{ch.get('status','pending')}]"):
                st.text(ch.get('outline',''))
        st.download_button("📥 导出大纲", "\n".join(f"第{c['num']}章 {c['title']}\n  {c.get('outline','')}" for c in chs),
                          file_name="outline.txt")
    else:
        st.info("尚未生成大纲")

# ======== 写作 ========
elif page == "✍️ 写作":
    st.header("✍️ 章节写作")
    prog = api("GET", "/api/progress") or {}
    chs = prog.get("chapters", [])
    ci = prog.get("current_chapter_index", 0)
    phase = prog.get("phase", "")

    if phase != "writing":
        st.warning("请在大纲页确认大纲")
        st.stop()

    # 自动确认
    auto = api("GET", "/api/auto-confirm") or {}
    auto_enabled = st.toggle("🤖 自动确认模式", value=auto.get("enabled", False))
    if auto_enabled != auto.get("enabled", False):
        api("PUT", "/api/auto-confirm", {"enabled": auto_enabled})

    if ci < len(chs):
        cur_ch = chs[ci]
        detail = api("GET", f"/api/chapters/{cur_ch['num']}")

        st.subheader(f"当前: 第{cur_ch['num']}章「{cur_ch['title']}」")
        st.caption(f"大纲: {cur_ch.get('outline','(无)')}")

        c1, c2, c3 = st.columns(3)
        if c1.button("✍️ 生成本章", type="primary"):
            with st.spinner(f"生成第{cur_ch['num']}章..."):
                r = api("POST", "/api/chapters/generate", timeout=180)
                if r: st.success(f"✅ {r.get('word_count',0)}字"); st.rerun()
        if c2.button("✅ 确认", disabled=cur_ch.get('status')!='review'):
            api("POST", "/api/chapters/confirm"); st.rerun()
        if c3.button("🔄 重写", disabled=cur_ch.get('status')!='review'):
            api("POST", "/api/chapters/revise", {"feedback": "请重新写"}, timeout=180); st.rerun()

        if detail and detail.get("content"):
            with st.expander("📄 正文", expanded=True):
                st.text_area("", detail["content"], height=400, key=f"ch{cur_ch['num']}")
                st.caption(f"字数: {detail.get('word_count',0)}")
            if detail.get("summary"):
                with st.expander("📝 摘要"): st.text(detail["summary"])

    # 所有章节状态
    st.subheader("📊 全部章节")
    cols = st.columns(min(len(chs), 6))
    for i, ch in enumerate(chs):
        icon = {"pending":"⬜","writing":"🟡","review":"🔵","accepted":"✅"}.get(ch["status"],"⬜")
        cols[i % len(cols)].caption(f"{icon} Ch{ch['num']}")

# ======== 伏笔 ========
elif page == "🔮 伏笔":
    st.header("🔮 伏笔管理")

    c1, c2 = st.columns(2)
    if c1.button("💡 AI建议伏笔"):
        with st.spinner(): 
            r = api("POST", "/api/foreshadows/suggest", timeout=60)
            if r:
                st.session_state["fs_suggest"] = r.get("foreshadows", [])

    foreshadows = api("GET", "/api/foreshadows") or []

    # 显示建议
    if "fs_suggest" in st.session_state and st.session_state["fs_suggest"]:
        st.subheader("AI 建议的伏笔")
        suggestions = st.session_state["fs_suggest"]
        selected = []
        for i, s in enumerate(suggestions):
            c1, c2 = st.columns([8,2])
            c1.write(f"**{s['name']}** — {s.get('description','')[:100]}")
            c1.caption(f"埋设: Ch{s.get('plant_chapter','?')} | 回收: Ch{s.get('target_chapter','?')}")
            if c2.checkbox("选", key=f"fs_sel_{i}"):
                selected.append(s)
        if selected and st.button("确认选中"):
            api("POST", "/api/foreshadows/confirm", {"foreshadows": selected})
            del st.session_state["fs_suggest"]
            st.rerun()

    st.divider()

    # 手动创建
    with st.expander("➕ 手动创建"):
        with st.form("new_fs"):
            name = st.text_input("名称")
            desc = st.text_area("描述"); pc = st.number_input("埋设章节",min_value=1,value=1)
            tc = st.number_input("回收章节",min_value=1,value=1)
            if st.form_submit_button("创建"):
                api("POST","/api/foreshadows",{"name":name,"description":desc,"plant_chapter":pc,"target_chapter":tc})
                st.rerun()

    if foreshadows:
        for f in foreshadows:
            with st.expander(f"#{f['id']} {f['name']} [{f['status']}]"):
                st.text(f['description'])
                st.caption(f"埋设: Ch{f['plant_chapter']} | 回收: Ch{f['target_chapter']}")

    if st.button("📋 查看路线图"):
        rm = api("GET", "/api/foreshadows/roadmap")
        if rm: st.markdown(rm.get("markdown",""))

# ======== 角色 ========
elif page == "👤 角色":
    st.header("👤 角色")

    with st.expander("➕ 添加"):
        with st.form("add_c"):
            name = st.text_input("名称")
            c1,c2=st.columns(2); age=c1.text_input("年龄"); pers=c2.text_input("性格")
            appear=st.text_input("外貌"); bg=st.text_area("背景"); motiv=st.text_input("动机")
            abil=st.text_input("能力"); notes=st.text_input("备注")
            if st.form_submit_button("添加"):
                api("POST","/api/settings/characters",{"name":name,"age":age,"appearance":appear,
                    "personality":pers,"background":bg,"motivation":motiv,"abilities":abil,"notes":notes})
                st.rerun()

    s = api("GET","/api/settings") or {}
    for c in s.get("characters",[]):
        with st.expander(f"👤 {c['name']} {c.get('age','')}"):
            for k in ["appearance","personality","background","motivation","abilities","notes"]:
                if c.get(k): st.caption(f"**{k}**: {c[k]}")

# ======== 世界观 ========
elif page == "🌍 世界观":
    st.header("🌍 世界观 & 组织")

    tab1, tab2 = st.tabs(["世界观", "组织"])

    with tab1:
        with st.form("add_w"):
            n=st.text_input("名称","",key="wn"); cat=st.text_input("分类","",key="wcat")
            d=st.text_area("描述","",key="wd"); t=st.text_input("标签","",key="wt")
            if st.form_submit_button("添加"):
                api("POST","/api/settings/worldview",{"name":n,"category":cat,"description":d,"tags":t}); st.rerun()
        s = api("GET","/api/settings") or {}
        for w in s.get("worldview",[]):
            with st.expander(f"🌍 {w['name']} ({w.get('category','')})"):
                st.text(w.get("description",""))

    with tab2:
        with st.form("add_o"):
            n=st.text_input("名称","",key="on"); t=st.text_input("类型","",key="ot")
            d=st.text_area("描述","",key="od")
            if st.form_submit_button("添加"):
                api("POST","/api/settings/organizations",{"name":n,"type":t,"description":d}); st.rerun()
        s = api("GET","/api/settings") or {}
        for o in s.get("organizations",[]):
            st.markdown(f"**🏛 {o['name']}** ({o.get('type','')})")
            st.caption(o.get("description",""))

# ======== 进度 ========
elif page == "📊 进度":
    st.header("📊 项目进度")

    s = api("GET","/api/status") or {}
    col1,col2,col3,col4 = st.columns(4)
    col1.metric("阶段", s.get("phase","?"))
    col2.metric("总章节", s.get("total_chapters",0))
    col3.metric("项目", sel)

    prog = api("GET","/api/progress") or {}
    chs = prog.get("chapters",[])
    if chs:
        accepted = sum(1 for c in chs if c["status"]=="accepted")
        st.progress(accepted/len(chs), f"已确认: {accepted}/{len(chs)}")

    if st.button("🗑 重置进度", type="secondary"):
        api("DELETE","/api/progress"); st.rerun()
    if st.button("📥 导出全书 TXT"):
        r = requests.get(f"{API}/api/export/txt")
        st.download_button("下载", r.text, file_name=f"{prog.get('title','novel')}.txt")
