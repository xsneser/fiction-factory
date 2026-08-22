"""NovelEngine MCP 服务器 — 从 agent_tools 注册全部工具为 MCP（stdio，供 Claude Code 等客户端驱动）。

工具本体在 `agent_tools.py`（共享注册表 TOOL_REGISTRY，42 个，MCP 面 40），本文件只做适配：
把每个工具注册为 FastMCP 工具，并在执行时落工具日志（source=mcp，供 Web 端
`/api/agent/tool-log` 展示外部调用）。侧栏 dsh 桥（libraries/dsh_bridge.py）经 MCP
驱动同一套工具；MCP 是独立进程，与 Web 通过 books/ 文件 JSON 协调。

用法（项目根目录）：
    claude mcp add --scope project novel-engine -- python mcp_server.py
"""
import functools
import inspect
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# FastMCP 导入路径随 SDK 版本变化：mcp 1.x 用 mcp.server.fastmcp。
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    from fastmcp import FastMCP

from agent_tools import TOOL_REGISTRY  # noqa: E402
from libraries.tool_log import log_tool_call  # noqa: E402
from libraries.loop_guard import get_loop_guard  # noqa: E402

# 工具日志 source 区分：dsh 内部调用经 runtime overlay 带 `--source dsh` 拉起
# （source=dsh，tool_log 不写 storage/tool_log.jsonl——「工具日志」页签只展示
# 外部 agent 的 source=mcp 调用，内部 dsh 不混入）；外部拉起（.mcp.json / mcp_smoke）
# 无此参数 → source=mcp。
_SOURCE = "dsh" if "--source" in sys.argv else "mcp"

mcp = FastMCP("novel-engine")


def _mcp_args(fn, args, kwargs) -> dict:
    """把位置/关键字参数按签名归并成 dict，供日志展示。"""
    try:
        names = list(inspect.signature(fn).parameters.keys())
        merged = dict(kwargs)
        for i, name in enumerate(names):
            if i < len(args):
                merged[name] = args[i]
        return merged
    except Exception:
        return dict(kwargs or {})


def _mcp_summary(result) -> str:
    """把工具返回压缩成一行日志摘要。"""
    if isinstance(result, dict):
        if result.get("error"):
            return f"失败：{result['error']}"
        # world_candidates 增量候选：摘要含 total+标题，同参连调时每次不同
        # （否则全部落「已执行」常量，LoopGuard 会把正常增量连调误判为无进展循环）
        cand = result.get("candidate")
        total = result.get("total")
        if isinstance(cand, dict) and cand.get("title") and total:
            return f"候选{total}：{cand['title']}"
        # 动态进度字段优先于 status：write_next_bridge 同参但字数/章节增长时摘要须不同，
        # 否则 status 常量会让 LoopGuard 把正常推进误判为无进展循环（桥段写作被熔断）。
        for k in ("ok", "words", "word_count", "chapter", "count", "phase",
                  "status", "plots_added", "total_plots", "total_chapters",
                  "passed", "score", "chosen", "book_id", "deleted", "cmd",
                  "issue_count", "overdue_count", "advanced_count",
                  "stalled_count", "fulfilled_count"):
            if k in result and result[k] not in (None, "", False):
                return f"{k}={result[k]}"
        for k in ("summary", "message", "reply"):
            v = result.get(k)
            if v:
                return str(v)[:300]
        return "已执行"
    if isinstance(result, list):
        return f"共 {len(result)} 项"
    return str(result)[:200]


def _wrap_logged(fn):
    """包装 MCP 工具：语义环熔断 + 执行前后落工具日志（source=mcp）。

    functools.wraps 保留 __name__/__doc__/__wrapped__，FastMCP 据此生成
    工具名、描述与 JSON Schema（签名不变）。

    LoopGuard：MCP 是外部 agent → 平台的唯一咽喉，在这里做确定性熔断
    （libraries/loop_guard.py）——同参同结果轮询 / 连续失败即报错，所有 MCP
    客户端（dsh / Claude Code）都受益。
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        t0 = time.time()
        ok, summary = True, ""
        merged_args = _mcp_args(fn, args, kwargs)
        try:
            get_loop_guard().before_call(fn.__name__, merged_args)
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and "__navigate__" in result:
                summary = f"已请求跳转 {result['__navigate__']}"
            else:
                summary = _mcp_summary(result)
            return result
        except Exception as e:
            ok = False
            summary = str(e)
            raise
        finally:
            # 熔断异常（无进展循环）直接向上抛，阻止 dsh 继续死循环
            try:
                get_loop_guard().after_call(fn.__name__, merged_args, ok, summary)
            except Exception:
                raise
            try:
                log_tool_call({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "time": time.strftime("%H:%M:%S"),
                    "run_id": f"mcp.{int(time.time())}",
                    "tool": fn.__name__,
                    "args": merged_args,
                    "ok": ok,
                    "summary": summary,
                    "duration_ms": round((time.time() - t0) * 1000),
                    "source": _SOURCE,
                })
            except Exception:
                pass
    return _wrapped


# 逐个注册（工具名/描述/schema 由函数签名+docstring 自动生成）。
# 护栏：直建/直删工具不存在于注册表——建书走「启动新书」向导 UI
# （drive_ui 驱动）、删书走书库页手动；navigate/drive_ui 经意图桥驱动浏览器/向导。
for _entry in TOOL_REGISTRY:
    mcp.tool()(_wrap_logged(_entry["func"]))

if __name__ == "__main__":
    mcp.run()   # stdio transport
