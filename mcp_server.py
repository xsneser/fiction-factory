"""NovelEngine MCP 服务器 — 从 agent_tools 注册全部工具为 MCP（stdio，供 Claude Code 等客户端驱动）。

工具本体在 `agent_tools.py`（共享注册表 TOOL_REGISTRY，29 个），本文件只做适配：
把每个工具注册为 FastMCP 工具。MCP 与 Web 侧栏 Agent（plugins/agent_loop.py）
共用同一套工具实现；MCP 是独立进程，与 Web 通过 books/ 文件 JSON 协调。

用法（项目根目录）：
    claude mcp add --scope project novel-engine -- python mcp_server.py
"""
import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# FastMCP 导入路径随 SDK 版本变化：mcp 1.x 用 mcp.server.fastmcp。
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    from fastmcp import FastMCP

from agent_tools import TOOL_REGISTRY  # noqa: E402

mcp = FastMCP("novel-engine")

# 逐个注册（工具名/描述/schema 由函数签名+docstring 自动生成）
for _entry in TOOL_REGISTRY:
    mcp.tool()(_entry["func"])

if __name__ == "__main__":
    mcp.run()   # stdio transport
