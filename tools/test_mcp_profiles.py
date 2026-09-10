"""MCP profile 协议测试：确认 CLI profile 真正裁剪 list_tools。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED = {
    "write": {"prepare_plot_run", "save_plot_draft"},
    "replan": {"get_story_state", "save_outlines", "validate_storyline", "validate_world",
               "query_arc_library", "query_plots", "drive_ui"},
}


async def check(profile: str) -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params = StdioServerParameters(command=sys.executable,
                                   args=[os.path.join(root, "mcp_server.py"), "--profile", profile],
                                   cwd=root)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == EXPECTED[profile], (profile, sorted(names))
            print(f"[OK] {profile}: {len(names)} tools")


async def main() -> None:
    for profile in EXPECTED:
        await check(profile)


if __name__ == "__main__":
    asyncio.run(main())
