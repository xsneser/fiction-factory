"""渲染 /settings 页面，验证模板可编译且不含明文 key。"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.api_config as acmod
from flask import Flask
import ui.web_blueprints.settings as st


def main():
    tmp = tempfile.TemporaryDirectory()
    p = Path(tmp.name) / "api.json"
    p.write_text(json.dumps({
        "version": 2,
        "default_provider": "ds",
        "providers": {
            "ds": {"id": "ds", "name": "DeepSeek 官方", "enabled": True,
                   "base_url": "https://api.deepseek.com",
                   "api_key": "sk-should-never-appear-in-html",
                   "default_model": "deepseek-chat",
                   "models": [{"id": "deepseek-chat", "name": "V3"},
                              {"id": "deepseek-reasoner", "name": "R1"}]},
        },
        "subagents": {"writer": {"provider_id": "ds", "model": "deepseek-reasoner"}},
        "backend": {"provider_id": "ds", "model": "deepseek-chat"},
        "context_budget_tokens": 288000,
    }, ensure_ascii=False), encoding="utf-8")

    acmod.API_PATH_OVERRIDE = p
    try:
        app = Flask(__name__, template_folder=str(ROOT / "ui" / "templates"),
                    static_folder=str(ROOT / "ui" / "static"))
        app.register_blueprint(st.bp)
        r = app.test_client().get("/settings")
        assert r.status_code == 200, r.status_code
        html = r.get_data(as_text=True)

        assert "sk-should-never-appear-in-html" not in html, "明文 key 进了 HTML"
        assert "st-should-never" not in html
        assert ("系统设置" in html or "模型路由工作台" in html or "模型分流架构矩阵" in html)
        assert "st-bootstrap" in html
        assert "settings.js" in html
        assert "settings.css" in html
        # 引导 JSON 必须能被解析，且带掩码 key
        import re
        m = re.search(r'<script id="st-bootstrap" type="application/json">(.*?)</script>', html, re.S)
        assert m, "找不到 bootstrap JSON"
        boot = json.loads(m.group(1))
        assert boot["providers"]["ds"]["api_key_masked"].endswith("****")
        assert boot["providers"]["ds"]["api_key_masked"].startswith("sk-shoul")
        assert boot["providers"]["ds"]["api_key_configured"] is True
        assert "writer" in boot["subagents"]
        assert "orchestrator" in boot["subagents"], "未在配置里的角色应被补全"
        assert boot["backend"]["model"] == "deepseek-chat"

        m2 = re.search(r'<script id="st-role-meta" type="application/json">(.*?)</script>', html, re.S)
        assert m2, "找不到 role-meta JSON"
        meta = json.loads(m2.group(1))
        assert meta["titles"]["writer"] == "段落写手"
        assert "orchestrator" in meta["order"]

        # UI contract: clean, refactored selectors without clutter
        js_src = (ROOT / "ui" / "static" / "js" / "settings.js").read_text(encoding="utf-8")
        css_src = (ROOT / "ui" / "static" / "css" / "settings.css").read_text(encoding="utf-8")
        assert "st-route-controls" in js_src
        assert "st-key-badge" in js_src
        assert "st-route-preview" not in js_src, "底部的重复路由预览行未彻底删除"
        assert "st-route-preview" not in css_src, "底部的重复路由预览样式未清理"
        print("✓ /settings 渲染正常，无明文 key 泄漏")
    finally:
        acmod.API_PATH_OVERRIDE = None
        tmp.cleanup()


if __name__ == "__main__":
    main()
    print("\nsettings page render test passed!")
