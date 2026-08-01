"""
NovelEngine 启动脚本
用法:
  python run.py server    - 启动 API 服务器 (端口 58080)

Web 管理面板（主界面）请运行: python ui/web_ui.py（launch.sh / launch.bat 已封装）
旧的 Streamlit 实验台已废弃移除。
"""
import os
import sys
import uvicorn

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ["NOVEL_ENGINE_DIR"] = ROOT


def server():
    from main import app
    # 默认只监听本机；如需局域网访问设置环境变量 NOVEL_HOST=0.0.0.0
    host = os.environ.get("NOVEL_HOST", "127.0.0.1")
    print(f"🚀 API 服务器: http://localhost:58080")
    print(f"   项目目录: {ROOT}")
    uvicorn.run(app, host=host, port=58080)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "server"
    if cmd != "server":
        print(f"❌ 未知命令: {cmd}（仅支持 server）")
        sys.exit(1)
    server()
