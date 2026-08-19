"""NovelEngine MCP 服务器 — 从 agent_tools 注册全部工具为 MCP（stdio，供 Claude Code 等客户端驱动）。

工具本体在 `agent_tools.py`（共享注册表 TOOL_REGISTRY，37 个，MCP 面 36），本文件只做适配：
把每个工具注册为 FastMCP 工具，并在执行时落工具日志（source=mcp，供 Web 端
`/api/agent/tool-log` 展示外部调用）。MCP 与 Web 侧栏 Agent（plugins/agent_loop.py）
共用同一套工具实现；MCP 是独立进程，与 Web 通过 books/ 文件 JSON 协调。

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
        for k in ("ok", "status", "count", "plots_added", "total_plots",
                  "total_chapters", "chapter", "word_count", "phase",
                  "passed", "score", "chosen", "book_id", "deleted"):
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
    """包装 MCP 工具：执行前后落工具日志（source=mcp）。

    functools.wraps 保留 __name__/__doc__/__wrapped__，FastMCP 据此生成
    工具名、描述与 JSON Schema（签名不变）。
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        t0 = time.time()
        ok, summary = True, ""
        try:
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
            try:
                log_tool_call({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "time": time.strftime("%H:%M:%S"),
                    "run_id": f"mcp.{int(time.time())}",
                    "tool": fn.__name__,
                    "args": _mcp_args(fn, args, kwargs),
                    "ok": ok,
                    "summary": summary,
                    "duration_ms": round((time.time() - t0) * 1000),
                    "source": "mcp",
                })
            except Exception:
                pass
    return _wrapped


# 逐个注册（工具名/描述/schema 由函数签名+docstring 自动生成）。
# 只注册 mcp/both 面工具：canvas_command 标记 surface="web"（MCP 无画布语义）不暴露；
# navigate 为 both——内部走 SSE 直达、外部（MCP）经 §1.3 意图桥驱动浏览器（写 nav_intent.json）。
for _entry in TOOL_REGISTRY:
    if _entry.get("surface", "both") not in ("mcp", "both"):
        continue
    mcp.tool()(_wrap_logged(_entry["func"]))

if __name__ == "__main__":
    mcp.run()   # stdio transport
