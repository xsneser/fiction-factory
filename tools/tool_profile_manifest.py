"""输出由 TOOL_REGISTRY 派生的机器可读工具能力清单。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_tools import TOOL_REGISTRY
from libraries.agent_tool_router import manifest


if __name__ == "__main__":
    print(json.dumps({"tools": manifest(TOOL_REGISTRY)}, ensure_ascii=False, indent=2))
