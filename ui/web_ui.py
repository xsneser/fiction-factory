"""
NovelEngine — 完整 Web UI v2.0 (Flask + Jinja2)
引擎集成版：新书启动 / 续写 / 管理面板
路由已按域拆分到 ui/web_blueprints/（dashboard/storyline/desk/books/libraries/tools/settings），
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


@app.after_request
def _no_html_cache(resp):
    """HTML 页不缓存：浏览器加载模板改动后总是拿到新 DOM（防旧缓存导致布局错乱，
    如步3 两栏在旧 DOM 里因缺 workspace.css 塌成一栏）。静态资源由 ?v= 版本参数控制。"""
    if resp.content_type and resp.content_type.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


if __name__ == "__main__":
    os.makedirs("ui/templates", exist_ok=True)
    os.makedirs("ui/static", exist_ok=True)
    from libraries.token_proxy import ensure_proxy
    ensure_proxy()   # 拉起本地 LLM API 代理（token 流量检测器）
    # debug 由环境变量控制：开发用 NOVEL_DEBUG=1，默认关闭（避免 reloader 干扰自动化）
    debug = os.environ.get("NOVEL_DEBUG") == "1"
    host = os.environ.get("NOVEL_HOST", "127.0.0.1")
    print(f"NovelEngine Web UI v2.0: http://localhost:58080")
    app.run(host=host, port=58080, debug=debug, use_reloader=debug)
