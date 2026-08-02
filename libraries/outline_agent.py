"""
大纲助手（Outline Agent）— 让用户在对话框里用自然语言调整故事线配置。

用法（由 ui/web_ui.py 的 /api/timeline/<id>/agent 路由调用）：
    agent = OutlineAgent(llm, structure_lib, plot_lib, gag_lib, theme_lib)
    result = agent.handle(tl, "第一个桥段改成打脸爽文")
    # result = {"ok": True, "intent": "...", "reply": "...",
    #           "summary": ["改动摘要", ...], "timeline": tl.to_dict()}

意图（intent）：
  modify_plot  — 改某个桥段的名字/分类/结构
  add_gag      — 给某个桥段加笑点（库内匹配，未命中则存自定义）
  remove_gag   — 移除某桥段的指定笑点
  add_plot     — 新增一个桥段并挂到指定大纲
  remove_plot  — 删除某个桥段（连带清理嵌套引用）
  modify_outline — 改大纲名/章节范围/叙事手法/备注
  general      — 闲聊或无法识别，仅回复引导
"""
import json
import re
from typing import Optional

from .timeline import PlotSlot


class OutlineAgent:
    def __init__(self, llm, structure_lib=None, plot_lib=None,
                 gag_lib=None, theme_lib=None):
        self.llm = llm
        self.structures = structure_lib
        self.plots = plot_lib
        self.gags = gag_lib
        self.themes = theme_lib

    # ═══════════════════════════════════════════
    # 入口
    # ═══════════════════════════════════════════

    def handle(self, tl, message: str) -> dict:
        plots_ordered = self._ordered_plots(tl)
        ctx = self._build_context(tl, plots_ordered)
        action = self._parse(message, ctx)

        intent = action.get("intent", "general")
        reply = action.get("reply", "")
        summary = []

        if intent == "modify_plot":
            plot, err = self._resolve_plot(plots_ordered, action)
            if plot:
                summary = self._apply_modify_plot(plot, action)
            else:
                intent = "general"; reply = (reply or "") + f"（{err}）"
        elif intent == "add_gag":
            plot, err = self._resolve_plot(plots_ordered, action)
            if plot:
                summary = self._apply_add_gag(plot, action)
            else:
                intent = "general"; reply = (reply or "") + f"（{err}）"
        elif intent == "remove_gag":
            plot, err = self._resolve_plot(plots_ordered, action)
            if plot:
                summary = self._apply_remove_gag(plot, action)
            else:
                intent = "general"; reply = (reply or "") + f"（{err}）"
        elif intent == "remove_plot":
            plot, err = self._resolve_plot(plots_ordered, action)
            if plot:
                summary = self._apply_remove_plot(tl, plot, action)
            else:
                intent = "general"; reply = (reply or "") + f"（{err}）"
        elif intent == "add_plot":
            summary, _p = self._apply_add_plot(tl, action)
        elif intent == "modify_outline":
            summary = self._apply_modify_outline(tl, action)
        else:
            intent = "general"

        if intent == "general":
            reply = reply or self._help_text()
        elif not reply:
            reply = "已按你的要求调整，改动如下。"

        return {
            "ok": True,
            "intent": intent,
            "reply": reply,
            "summary": summary,
            "timeline": tl.to_dict(),
        }

    # ═══════════════════════════════════════════
    # 上下文与解析
    # ═══════════════════════════════════════════

    def _ordered_plots(self, tl) -> list:
        """按 大纲顺序→阶段→次序 排序，保证"第几个桥段"稳定可复现。"""
        outline_pos = {o.id: i for i, o in enumerate(tl.outlines)}

        def key(p):
            return (outline_pos.get(p.outline_id, 9999), p.stage_index, p.order)
        return sorted(tl.plots, key=key)

    def _build_context(self, tl, plots_ordered) -> str:
        outlines_txt = "\n".join(
            f"{i}. 大纲「{o.name}」(id={o.id}) 第{o.start_chapter}-{o.end_chapter}章"
            for i, o in enumerate(tl.outlines, 1)
        ) or "(暂无大纲)"
        plot_lines = []
        for i, p in enumerate(plots_ordered, 1):
            gags = "、".join(p.gag_ids) if p.gag_ids else "无"
            o = next((x for x in tl.outlines if x.id == p.outline_id), None)
            oname = o.name if o else "?"
            plot_lines.append(
                f"{i}. 桥段「{p.name}」(id={p.id}) 所属大纲=「{oname}」 "
                f"分类={p.category} 结构={(p.template_structure or '')[:60]} 笑点=[{gags}]"
            )
        return ("【大纲列表】\n" + outlines_txt + "\n\n【桥段列表】\n"
                + ("\n".join(plot_lines) if plot_lines else "(暂无桥段)"))

    def _parse(self, message: str, ctx: str) -> dict:
        prompt = f"""你是网文策划编辑的「大纲助手」，把用户的口语指令解析成对故事线配置的修改动作。

当前故事线配置（序号即用户口中的"第几个"）：
{ctx}

用户指令：{message}

请解析用户意图并只返回 JSON（不要多余文字）：
{{
  "intent": "modify_plot | add_gag | remove_gag | add_plot | remove_plot | modify_outline | general",
  "target_index": 数字或null,
  "target_name": "用户提到的桥段/大纲名称，用于辅助定位",
  "new_fields": {{ "name": "", "category": "", "template_structure": "", "notes": "" }},
  "gags": ["笑点描述1", "笑点描述2"],
  "remove_gag_ids": ["要移除的笑点id"],
  "new_plot": {{ "name": "", "category": "" }},
  "outline_index": 数字或null,
  "reply": "用中文向用户简洁说明你准备做什么"
}}

意图说明：
- modify_plot：用户说某桥段要改/不好/太弱，要改成XXX → target_index 指桥段序号，new_fields 填要改的字段
- add_gag：用户说给某桥段加笑点/加梗/搞笑点 → target_index 指桥段序号，gags 填要加的笑点
- remove_gag：用户说删某桥段的某笑点 → target_index 指桥段序号，remove_gag_ids 或 gags 填要删的
- add_plot：用户说加一个新桥段/补一个情节 → new_plot 填名字，outline_index 填挂载大纲序号
- remove_plot：用户说删掉某桥段 → target_index 指桥段序号
- modify_outline：用户说改大纲/故事线范围/改名 → target_index 指大纲序号，new_fields 填字段
- general：闲聊/提问/无法确定
注意：target_index 优先于 target_name。用户没说清楚目标时 target_index 用 null。"""
        try:
            from core.llm_client import extract_json
            raw = self.llm.call("你只返回 JSON。", prompt,
                                temperature=0.2, max_tokens=2048)
            data = json.loads(extract_json(raw))
            if not isinstance(data, dict):
                raise ValueError("解析结果不是对象")
            return data
        except Exception:
            return {"intent": "general",
                    "reply": "我没能理解你的指令，试试这些：\n"
                             "· 第一个桥段改成打脸爽文\n"
                             "· 给「夺宝」桥段加两个笑点\n"
                             "· 删掉第二个桥段\n"
                             "· 在「修炼篇」加一个高燃桥段\n"
                             "· 把第三段大纲改名叫「绝境翻盘」"}

    # ═══════════════════════════════════════════
    # 目标解析
    # ═══════════════════════════════════════════

    @staticmethod
    def _to_index(v):
        """把数字或数字字符串转成 1-based int；无效返回 None。"""
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return None

    def _resolve_plot(self, plots_ordered, action) -> tuple:
        ti = self._to_index(action.get("target_index"))
        if ti is not None and ti >= 1:
            idx = ti - 1
            if idx < len(plots_ordered):
                return plots_ordered[idx], ""
        name = action.get("target_name")
        if name:
            for p in plots_ordered:
                if name in p.name or p.name in name:
                    return p, ""
        return None, "找不到对应桥段"

    def _resolve_outline(self, tl, action) -> Optional[object]:
        # modify_outline 用 target_index 指大纲序号；add_plot 用 outline_index 指挂载大纲
        idx = action.get("target_index")
        if idx is None:
            idx = action.get("outline_index")
        idx = self._to_index(idx)
        if idx is not None and idx >= 1:
            n = idx - 1
            if n < len(tl.outlines):
                return tl.outlines[n]
        name = action.get("target_name")
        if name:
            for o in tl.outlines:
                if name in o.name or o.name in name:
                    return o
        return tl.outlines[-1] if tl.outlines else None

    def _next_id(self, prefix: str, existing_ids) -> str:
        max_n = 0
        for i in existing_ids:
            m = re.search(r"_(\d+)$", i or "")
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f"{prefix}_{max_n + 1:04d}"

    # ═══════════════════════════════════════════
    # 应用动作
    # ═══════════════════════════════════════════

    def _apply_modify_plot(self, plot, action) -> list:
        summary = []
        nf = action.get("new_fields") or {}
        name = (nf.get("name") or "").strip()
        if name and name != plot.name:
            summary.append(f"桥段「{plot.name}」改名 →「{name}」")
            plot.name = name
        category = (nf.get("category") or "").strip()
        if category and category != plot.category:
            summary.append(f"桥段「{plot.name}」分类 → {category}")
            plot.category = category
        structure = (nf.get("template_structure") or "").strip()
        if structure:
            summary.append(f"桥段「{plot.name}」结构已更新")
            plot.template_structure = structure
        if not summary:
            summary.append(f"桥段「{plot.name}」未发现可改字段，保持原样")
        return summary

    def _match_gag(self, text: str) -> Optional[str]:
        """把用户说的笑点描述匹配到笑点库 id；匹配不到返回 None。"""
        if not self.gags:
            return None
        if self.gags.get_by_id(text):
            return text
        for p in self.gags.patterns:
            if text == p.name or text in p.name or p.name in text:
                return p.id
        return None

    def _apply_add_gag(self, plot, action) -> list:
        summary = []
        gags = action.get("gags") or []
        if not gags:
            cands = self.gags.search(scene=plot.category) if self.gags else []
            gags = [g.name for g in cands[:2]]
        for g in gags:
            gid = self._match_gag(g)
            if gid and gid not in plot.gag_ids:
                plot.gag_ids.append(gid)
                summary.append(f"桥段「{plot.name}」新增笑点「{g}」")
            elif not gid:
                custom = self._next_id("custom", plot.gag_ids)
                plot.gag_ids.append(custom)
                summary.append(f"桥段「{plot.name}」新增自定义笑点「{g}」")
        if not summary:
            summary.append(f"桥段「{plot.name}」笑点已是最新，无需新增")
        return summary

    def _apply_remove_gag(self, plot, action) -> list:
        summary = []
        ids = action.get("remove_gag_ids") or []
        for gid in ids:
            if gid in plot.gag_ids:
                plot.gag_ids.remove(gid)
                summary.append(f"桥段「{plot.name}」移除笑点 {gid}")
        for g in (action.get("gags") or []):
            gid = self._match_gag(g)
            if gid and gid in plot.gag_ids:
                plot.gag_ids.remove(gid)
                summary.append(f"桥段「{plot.name}」移除笑点「{g}」")
        if not summary:
            summary.append(f"桥段「{plot.name}」未找到要移除的笑点")
        return summary

    def _apply_add_plot(self, tl, action) -> tuple:
        outline = self._resolve_outline(tl, action)
        if not outline:
            return ["未找到可挂载的大纲，无法新增桥段"], None
        np_info = action.get("new_plot") or {}
        name = (np_info.get("name") or "").strip() or "新桥段"
        category = (np_info.get("category") or "").strip()

        tmpl = None
        if self.plots:
            tmpl = self.plots.get_by_id(name)
            if not tmpl:
                for t in self.plots.templates:
                    if name in t.name or t.name in name:
                        tmpl = t
                        break
        pid = self._next_id("plot", [p.id for p in tl.plots])
        p = PlotSlot(
            id=pid,
            template_id=tmpl.id if tmpl else "",
            name=tmpl.name if tmpl else name,
            category=tmpl.category if tmpl else category,
            sub_category=getattr(tmpl, 'sub_category', '') if tmpl else "",
            outline_id=outline.id,
            stage_index=max(len(outline.stages) - 1, 0),
            order=len([x for x in tl.plots if x.outline_id == outline.id]),
            cover_beats=tmpl.word_range[1] // 400 if (tmpl and getattr(tmpl, 'word_range', None)) else 4,
            template_structure=tmpl.template_structure if tmpl else "",
            slots=[{"name": s.name, "default": s.default, "options": s.options}
                   for s in tmpl.slots] if tmpl else [],
        )
        tl.plots.append(p)
        return [f"新增桥段「{p.name}」已挂到大纲「{outline.name}」"], p

    def _apply_remove_plot(self, tl, plot, action) -> list:
        summary = []
        for cid in plot.children_plot_ids:
            tl.plots = [p for p in tl.plots if p.id != cid]
            summary.append(f"一并移除了子桥段 {cid}")
        if plot.parent_plot_id:
            for p in tl.plots:
                if p.id == plot.parent_plot_id and plot.id in p.children_plot_ids:
                    p.children_plot_ids.remove(plot.id)
        tl.plots = [p for p in tl.plots if p.id != plot.id]
        summary.append(f"已移除桥段「{plot.name}」")
        return summary

    def _apply_modify_outline(self, tl, action) -> list:
        o = self._resolve_outline(tl, action)
        if not o:
            return ["未找到对应大纲"]
        summary = []
        nf = action.get("new_fields") or {}
        name = (nf.get("name") or "").strip()
        if name and name != o.name:
            summary.append(f"大纲「{o.name}」改名 →「{name}」")
            o.name = name
        if nf.get("start_chapter"):
            o.start_chapter = int(nf["start_chapter"])
            summary.append(f"大纲起始章节 → 第{o.start_chapter}章")
        if nf.get("end_chapter"):
            o.end_chapter = int(nf["end_chapter"])
            summary.append(f"大纲结束章节 → 第{o.end_chapter}章")
        if nf.get("narrative"):
            o.narrative = nf["narrative"]
            summary.append(f"大纲叙事手法 → {nf['narrative']}")
        if nf.get("notes") is not None:
            o.notes = nf["notes"]
            summary.append("大纲备注已更新")
        if not summary:
            summary.append(f"大纲「{o.name}」未发现可改字段，保持原样")
        return summary

    def _help_text(self) -> str:
        return ("我可以帮你调整故事线，试试这些指令：\n"
                "· 第一个桥段改成打脸爽文\n"
                "· 给「夺宝」桥段加两个笑点\n"
                "· 删掉第二个桥段\n"
                "· 在「修炼篇」加一个高燃桥段\n"
                "· 把第三段大纲改名叫「绝境翻盘」")
