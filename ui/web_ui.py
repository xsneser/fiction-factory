"""
NovelEngine — 完整 Web UI v2.0 (Flask + Jinja2)
引擎集成版：新书启动 / 续写 / 管理面板
路由已按域拆分到 ui/web_blueprints/（dashboard/timeline/desk/books/libraries/tools/settings），
本文件只负责 app 创建、日志配置与蓝图注册。
"""
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

# 设置日志级别以便调试搜索
for name in ["novel-engine", "fanqie-scout", "__main__"]:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logger.addHandler(handler)

app = Flask(__name__, template_folder="templates", static_folder="static")

from ui.web_blueprints import register_blueprints
register_blueprints(app)


if __name__ == "__main__":
    os.makedirs("ui/templates", exist_ok=True)
    os.makedirs("ui/static", exist_ok=True)
    # debug 由环境变量控制：开发用 NOVEL_DEBUG=1，默认关闭（避免 reloader 干扰自动化）
    debug = os.environ.get("NOVEL_DEBUG") == "1"
    host = os.environ.get("NOVEL_HOST", "127.0.0.1")
    print(f"NovelEngine Web UI v2.0: http://localhost:58080")
    app.run(host=host, port=58080, debug=debug, use_reloader=debug)
