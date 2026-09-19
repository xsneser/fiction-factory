"""MCP profile 协议测试：确认 CLI profile 真正裁剪 list_tools。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED = {
    "write": {"prepare_plot_run", "save_plot_draft", "prepare_plot_revision", "save_plot_revision"},
    "replan": {"get_story_state", "validate_storyline", "validate_world",
               "query_arc_library", "query_plots", "drive_ui"},
    # 建书步 3 = 5 个薄工具（工具面即能力边界；含 drive_ui 在内的旧面已移出）
    "build": {"get_build_context", "query_arc_library", "query_plots",
              "validate_build", "save_build_draft"},
    # build-candidates 会按 build_status 的 pen_selected 摘掉 query_profiles，
    # 故只断言「⊆ 该 profile 且 ⊇ 去掉 query_profiles 的那一份」（见 check_optional）
    "build-candidates": {"navigate", "drive_ui", "get_build_status", "query_profiles"},
    # 编排面：**收章**与**运行时章计划**在主 Agent 手上；判决写入点不在（Critic 独有，I1）
    "orchestrate": {"list_books", "get_book_detail", "get_book_state", "get_storyline",
                    "get_story_state", "get_build_status", "get_build_context",
                    "get_orchestration_state", "get_plot_review_context", "plot_quality_gate",
                    "accept_plot_draft", "finalize_draft_chapter", "set_chapter_plan",
                    "save_basic_info", "validate_world", "navigate", "drive_ui"},
    # Critic：读评审上下文 + 把判决写回服务端换取 receipt
    "critic": {"get_plot_review_context", "record_plot_review"},
}


async def check(profile: str) -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params = StdioServerParameters(command=sys.executable,
                                   args=[os.path.join(root, "mcp_server.py"), "--profile", profile],
                                   cwd=root)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            if profile == "build-candidates":
                # 现场 build_status 的 pen_selected 会实时摘掉 query_profiles → 断言上下界
                assert names <= EXPECTED[profile], (profile, sorted(names - EXPECTED[profile]))
                assert names >= EXPECTED[profile] - {"query_profiles"}, (profile, sorted(names))
            else:
                assert names == EXPECTED[profile], (profile, sorted(names))
            # P1：**线路上**的 drive_ui schema 必须已按 profile 裁到能力边界
            # （2026-09-10：模型看得见 set_world、调了才吃 ui_command_forbidden，
            #   于是整轮预算花在平台错误恢复上）
            if "drive_ui" in names:
                from libraries.agent_tool_router import allowed_ui_commands
                schema = next(t for t in tools if t.name == "drive_ui").inputSchema
                enum = set(schema["properties"]["cmd"].get("enum") or [])
                assert enum == allowed_ui_commands(profile), (
                    profile, sorted(enum ^ allowed_ui_commands(profile)))
                desc = next(t for t in tools if t.name == "drive_ui").description or ""
                leaked = [c for c in ("set_world", "set_outline", "set_characters")
                          if c in desc and c not in allowed_ui_commands(profile)]
                assert not leaked, (profile, leaked)
                print(f"[OK] {profile}: {len(names)} tools；drive_ui cmd enum {len(enum)} 个已裁剪")
            else:
                print(f"[OK] {profile}: {len(names)} tools")


async def main() -> None:
    for profile in EXPECTED:
        await check(profile)


if __name__ == "__main__":
    asyncio.run(main())
