"""Agent 循环 — 原生 function calling 工具调用循环（无状态，供侧栏聊天端点驱动）。

每次请求独立：messages 由浏览器持有（user/assistant），循环内临时追加
assistant(tool_calls) + tool 结果消息，不跨请求保留。emit(evt) 是注入的
SSE 事件发射回调（返回格式化后的 SSE 行字符串），生成器逐个 yield。

事件协议：
  tool_start {run_id, tool, args} / tool_result {run_id, tool, ok, summary} /
  reply {content} / navigate {url} / canvas {book_id,action,outline_id,plot_id} /
  error {message} / done
"""
import json
import time

from ui.web_blueprints.ctx import get_llm
from agent_tools import TOOL_REGISTRY
from libraries.tool_log import log_tool_call, get_tool_log, clear_tool_log  # noqa: F401

MAX_ITERS = 12


def _now_ts() -> str:
    return time.strftime("%H:%M:%S")

SYSTEM_PROMPT = """你是 NovelEngine 的内置 Agent 助手，通过 function calling 操作整个创作引擎。

【第一原则：行动优先】用户提出任何请求时，先判断哪个工具能完成它：
能完成就立即调用该工具执行，不要只给出建议或"我可以帮你"之类的菜单。
只有用户的话无法对应任何工具（闲聊/含糊/确认性对话）时，才用文字回复。

可用工具覆盖：建书/设定/世界观/书名简介/大纲/桥段/内涵/写作/审查/去AI/上架/导出/删除/导航/画布。

完整链路（创建→上架），按需调用：
  list_books / get_book_state 摸底 → create_book → save_basic_info →
  generate_world / world_candidates / confirm_world → generate_title →
  generate_full_outline（或 generate_outlines → confirm_outlines → fill_plots → fill_gags）→
  write_next_bridge / write_chapter → review_text / deai_text →
  publish_check → mark_finished → publish_book → export_book

导航规则（重要）：navigate 是「浏览器页面跳转」工具，与读取数据是两件事。
用户说"打开/跳转/去看看/进入/显示"某页面时，**必须调用 navigate 让浏览器实际切页**，
禁止只文字回复或只用读数据工具代替。
- "打开书库/看看书库页面" → navigate(url="/books")
- "打开某本书详情" → navigate(url="/books/<book_id>")
- "进写作台/开始写作/续写" → navigate(url="/books/<book_id>/continue")
- "看上架/导出" → navigate(url="/books/<book_id>/publish")
- 即使已 get_book_state 读了数据，用户要"打开页面"就还要再调 navigate。
- book_id 未知时先 list_books / get_book_state 确认再导航。
要高亮写作台故事线：先 navigate("/books/<book_id>/continue")，再
canvas_command(book_id=..., action="highlight_plot", outline_id=..., plot_id=...)。

其他规则：
- delete_book 是破坏性操作：调用前必须先向用户用自然语言确认，得到明确同意后才可调用（confirm=True）。
- 工具结果是给 Agent 看的内部信息，要精炼；给用户的回复用中文、简洁，并给出下一步建议。
- 工具报错要如实转述并给可操作建议，不要编造成功。
- 一次只做用户要求的一件事，不擅自多做。"""


def _visible_tools() -> list:
    """Web 侧栏 Agent 面可见工具：surface ∈ {web, both}。"""
    return [t for t in TOOL_REGISTRY if t.get("surface", "both") in ("web", "both")]


def _tools_schema() -> list:
    return [{"type": "function", "function": {
        "name": t["name"],
        "description": t["description"],
        "parameters": t["input_schema"],
    }} for t in _visible_tools()]


def _summary(result) -> str:
    """把工具返回的 dict/list 压成一句中文摘要（给模型/前端看的精炼版）。"""
    if isinstance(result, list):
        if not result:
            return "（空结果）"
        labels = []
        for x in result[:3]:
            if isinstance(x, dict):
                labels.append(x.get("title") or x.get("name") or x.get("book_id")
                              or str(x.get("id", "")))
            else:
                labels.append(str(x))
        tail = "…" if len(result) > 3 else ""
        return f"共 {len(result)} 项：{'、'.join(l for l in labels if l)}{tail}"
    if isinstance(result, dict):
        if result.get("error"):
            return f"失败：{result['error']}"
        pairs = []
        for k in ("ok", "status", "count", "plots_added", "total_plots",
                  "total_chapters", "chapter", "word_count", "words", "target",
                  "phase", "passed", "score", "chosen", "book_id", "deleted",
                  "event_count", "cmd"):
            if k in result and result[k] not in (None, "", False):
                pairs.append(f"{k}={result[k]}")
        text = "；".join(pairs)
        for k in ("summary", "message", "reply"):
            v = result.get(k)
            if v:
                text = (text + "｜" if text else "") + str(v)[:300]
        return text or "（已执行）"
    return str(result)[:500]


def _compact(result, limit: int = 3000) -> str:
    """把工具结果序列化后截断，供 tool 消息回填给模型（防上下文超长）。"""
    try:
        s = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        s = str(result)
    if len(s) > limit:
        return s[:limit] + f"…(截断 {len(s) - limit} 字符)"
    return s


def run_agent_loop(messages, emit, system_prompt: str = SYSTEM_PROMPT):
    """Agent 工具调用循环（生成器）。messages 为 [{role:'user'|'assistant', content}]。"""
    llm = get_llm()
    if not llm:
        yield emit({"type": "error", "message": "LLM 未配置，请先到设置页保存 API 配置"})
        yield emit({"type": "done"})
        return

    by_name = {t["name"]: t for t in _visible_tools()}
    tools_schema = _tools_schema()
    conv = ([{"role": "system", "content": system_prompt}] if system_prompt else []) \
        + list(messages)[-20:]

    for step in range(1, MAX_ITERS + 1):
        try:
            msg = llm.call_tools(conv, tools_schema, temperature=0.1, max_tokens=8192)
        except Exception as e:
            yield emit({"type": "error", "message": f"LLM 调用失败：{e}"})
            break

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            yield emit({"type": "reply", "content": msg.get("content") or "完成。"})
            yield emit({"type": "done"})
            return

        # assistant 消息必须原样回填 content+tool_calls，后续 tool 消息才能引用 tool_call_id
        conv.append({"role": "assistant", "content": msg.get("content") or None,
                     "tool_calls": tool_calls})

        for idx, tc in enumerate(tool_calls):
            run_id = f"{step}.{idx}"
            fn = tc.get("function") or {}
            fn_name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}

            entry = by_name.get(fn_name)
            if not entry:
                log_tool_call({"time": _now_ts(), "run_id": run_id, "tool": fn_name,
                               "args": args, "ok": False, "summary": "未知工具",
                               "duration_ms": 0, "source": "web"})
                yield emit({"type": "tool_result", "run_id": run_id, "tool": fn_name,
                            "ok": False, "summary": "未知工具"})
                conv.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": json.dumps({"ok": False, "error": "未知工具"},
                                                   ensure_ascii=False)})
                continue

            t0 = time.time()
            yield emit({"type": "tool_start", "run_id": run_id, "tool": fn_name, "args": args})
            try:
                result = entry["func"](**args)
                ok = True
            except Exception as e:
                result = {"error": str(e)}
                ok = False

            if isinstance(result, dict) and "__navigate__" in result:
                yield emit({"type": "navigate", "url": result["__navigate__"]})
                summary = f"已跳转 {result['__navigate__']}"
            elif isinstance(result, dict) and "__canvas__" in result:
                c = result["__canvas__"]
                yield emit({"type": "canvas", **c})
                summary = f"已发出画布指令：{c.get('action', '')}"
            else:
                summary = _summary(result)

            log_tool_call({"time": _now_ts(), "run_id": run_id, "tool": fn_name,
                           "args": args, "ok": ok, "summary": summary,
                           "duration_ms": round((time.time() - t0) * 1000),
                           "source": "web"})
            yield emit({"type": "tool_result", "run_id": run_id, "tool": fn_name,
                        "ok": ok, "summary": summary})
            conv.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": json.dumps({"ok": ok, "result": _compact(result)},
                                               ensure_ascii=False)})
    else:
        yield emit({"type": "error", "message": "Agent 迭代超过上限，已停止"})
    yield emit({"type": "done"})
