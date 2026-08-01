"""
NovelEngine 启动脚本
用法:
  python run.py server    - 启动 API 服务器 (端口 58080)
  python run.py ui        - 启动 Streamlit UI (端口 8501)
  python run.py all       - 同时启动两者
"""
import os
import sys
import subprocess
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ["NOVEL_ENGINE_DIR"] = ROOT

def server():
    from main import app
    import uvicorn
    # 默认只监听本机；如需局域网访问设置环境变量 NOVEL_HOST=0.0.0.0
    host = os.environ.get("NOVEL_HOST", "127.0.0.1")
    print(f"🚀 API 服务器: http://localhost:58080")
    print(f"   项目目录: {ROOT}")
    uvicorn.run(app, host=host, port=58080)

def ui():
    import streamlit.web.cli as stcli
    sys.argv = ["streamlit", "run", os.path.join(ROOT, "ui", "streamlit_app.py"),
                "--server.port=8501", "--server.address=0.0.0.0"]
    stcli.main()

def all_services():
    import threading
    t = threading.Thread(target=server, daemon=True)
    t.start()
    time.sleep(2)
    ui()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "server"
    {"server": server, "ui": ui, "all": all_services}[cmd]()
