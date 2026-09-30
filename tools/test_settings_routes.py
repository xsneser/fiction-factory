"""测试设置页 v2 后端接口：读取脱敏 / 保存校验 / 掩码 key 保留。"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.api_config as acmod
from flask import Flask
import ui.web_blueprints.settings as st


class _TempConfig:
    """把 api.json 重定向到临时文件（用模块级 API_PATH_OVERRIDE，全局生效）。"""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "api.json"
        self._orig = acmod.API_PATH_OVERRIDE

    def __enter__(self):
        acmod.API_PATH_OVERRIDE = self.path
        return self.path

    def __exit__(self, *a):
        acmod.API_PATH_OVERRIDE = self._orig
        self._tmp.cleanup()
        return False


def _client():
    app = Flask(__name__)
    app.register_blueprint(st.bp)
    return app.test_client()


def test_config_endpoint_masks_keys():
    with _TempConfig() as p:
        p.write_text(json.dumps({
            "version": 2,
            "default_provider": "ds",
            "providers": {
                "ds": {"id": "ds", "name": "DS", "enabled": True,
                       "base_url": "https://api.deepseek.com",
                       "api_key": "sk-super-secret-key-1234",
                       "default_model": "deepseek-chat",
                       "models": [{"id": "deepseek-chat", "name": "Chat"}]},
            },
            "subagents": {"writer": {"provider_id": "ds", "model": "deepseek-chat"}},
            "backend": {"provider_id": "ds", "model": "deepseek-chat"},
        }, ensure_ascii=False), encoding="utf-8")

        client = _client()
        r = client.get("/api/settings/config")
        assert r.status_code == 200, r.status_code
        body = r.get_json()
        assert body["ok"] is True
        raw = json.dumps(body, ensure_ascii=False)
        assert "sk-super-secret-key-1234" not in raw, "明文 key 泄漏到响应"
        prov = body["config"]["providers"]["ds"]
        assert prov["api_key_masked"].startswith("sk-super")
        assert prov["api_key_masked"].endswith("****")
        assert prov["api_key_configured"] is True
        assert "writer" in body["config"]["subagents"]
        assert "orchestrator" in body["config"]["subagents"]
        print("✓ test_config_endpoint_masks_keys passed")


def test_save_v2_roundtrip():
    with _TempConfig() as p:
        p.write_text(json.dumps({
            "api_key": "sk-legacy-old-key",
            "base_url": "https://old.example.com",
            "model": "old-model",
        }, ensure_ascii=False), encoding="utf-8")

        client = _client()
        if True:
            app = Flask(__name__)
            app.register_blueprint(st.bp)
            client = app.test_client()

            # 旧 v1 文件 → 读出来应已合成 primary provider
            body = client.get("/api/settings/config").get_json()["config"]
            assert "primary" in body["providers"], body["providers"].keys()
            assert body["providers"]["primary"]["api_key_masked"] == "sk-legac****"

            # 保存成两个供应商，其中 writer 走第二个
            payload = {
                "default_provider": "ds",
                "providers": {
                    "ds": {"id": "ds", "name": "DeepSeek", "enabled": True,
                           "base_url": "https://api.deepseek.com",
                           "api_key": "****",   # 掩码 → 保留旧 key（此处旧 key 为空则留空）
                           "default_model": "deepseek-chat",
                           "models": [{"id": "deepseek-chat", "name": "Chat"}]},
                    "relay": {"id": "relay", "name": "Relay", "enabled": True,
                              "base_url": "https://relay.example.com/v1",
                              "api_key": "sk-relay-new-key",
                              "default_model": "gpt-4o",
                              "models": [{"id": "gpt-4o", "name": "GPT-4o"}]},
                },
                "subagents": {
                    "orchestrator": {"provider_id": "ds", "model": "deepseek-chat"},
                    "writer": {"provider_id": "relay", "model": "gpt-4o"},
                    "critic": {"provider_id": "relay", "model": "gpt-4o"},
                },
                "backend": {"provider_id": "ds", "model": "deepseek-chat"},
                "context_budget_tokens": 250000,
            }
            r = client.post("/api/settings/save", json=payload)
            assert r.status_code == 200, r.get_json()
            assert r.get_json()["ok"] is True

            saved = json.loads(p.read_text(encoding="utf-8"))
            assert saved["version"] == 2
            assert saved["default_provider"] == "ds"
            assert set(saved["providers"].keys()) == {"ds", "relay"}
            # 顶层影子字段 = 默认供应商
            assert saved["api_key"] == "", saved["api_key"]
            assert saved["base_url"] == "https://api.deepseek.com"
            assert saved["model"] == "deepseek-chat"
            assert saved["context_budget_tokens"] == 250000
            # 角色路由留住
            assert saved["subagents"]["writer"]["provider_id"] == "relay"
            assert saved["subagents"]["writer"]["model"] == "gpt-4o"
            print("✓ test_save_v2_roundtrip passed")


def test_save_preserves_masked_key():
    """掩码 key 回传时必须保留库里已有的真 key，而不是存成一串星号。"""
    with _TempConfig() as p:
        p.write_text(json.dumps({
            "version": 2,
            "default_provider": "ds",
            "providers": {"ds": {"id": "ds", "name": "DS", "enabled": True,
                                 "base_url": "https://api.deepseek.com",
                                 "api_key": "sk-real-key-abcdef",
                                 "default_model": "m1",
                                 "models": [{"id": "m1", "name": "M1"}]}},
            "backend": {"provider_id": "ds", "model": "m1"},
        }, ensure_ascii=False), encoding="utf-8")

        client = _client()
        r = client.post("/api/settings/save", json={
            "default_provider": "ds",
            "providers": {"ds": {"id": "ds", "name": "DS", "enabled": True,
                                 "base_url": "https://api.deepseek.com",
                                 "api_key": "sk-real-****",   # 掩码
                                 "default_model": "m1",
                                 "models": [{"id": "m1", "name": "M1"}]}},
            "backend": {"provider_id": "ds", "model": "m1"},
        })
        assert r.status_code == 200, r.get_json()
        saved = json.loads(p.read_text(encoding="utf-8"))
        assert saved["providers"]["ds"]["api_key"] == "sk-real-key-abcdef", \
            saved["providers"]["ds"]["api_key"]
        print("✓ test_save_preserves_masked_key passed")


def test_save_rejects_bad_routes():
    with _TempConfig() as p:
        p.write_text("{}", encoding="utf-8")
        client = _client()

        base = {
            "default_provider": "ds",
            "providers": {"ds": {"id": "ds", "name": "DS", "enabled": True,
                                 "base_url": "https://api.deepseek.com",
                                 "api_key": "sk-x", "default_model": "m1",
                                 "models": [{"id": "m1", "name": "M1"}]}},
            "backend": {"provider_id": "ds", "model": "m1"},
        }

        # 1) 角色指向不存在的供应商
        bad = json.loads(json.dumps(base))
        bad["subagents"] = {"writer": {"provider_id": "nope", "model": "m1"}}
        r = client.post("/api/settings/save", json=bad)
        assert r.status_code == 400 and "不存在" in r.get_json()["error"], r.get_json()

        # 2) 角色选了不在该供应商模型列表里的模型
        bad = json.loads(json.dumps(base))
        bad["subagents"] = {"writer": {"provider_id": "ds", "model": "ghost"}}
        r = client.post("/api/settings/save", json=bad)
        assert r.status_code == 400 and "模型" in r.get_json()["error"], r.get_json()

        # 3) 供应商地址指向本地代理自身
        bad = json.loads(json.dumps(base))
        bad["providers"]["ds"]["base_url"] = "http://127.0.0.1:58082"
        r = client.post("/api/settings/save", json=bad)
        assert r.status_code == 400 and "自环" in r.get_json()["error"], r.get_json()

        # 4) 供应商 ID 用保留前缀
        bad = json.loads(json.dumps(base))
        bad["providers"]["novelengine-route:x"] = dict(bad["providers"]["ds"])
        r = client.post("/api/settings/save", json=bad)
        assert r.status_code == 400 and "前缀" in r.get_json()["error"], r.get_json()

        # 5) 默认供应商不存在
        bad = json.loads(json.dumps(base))
        bad["default_provider"] = "ghost"
        r = client.post("/api/settings/save", json=bad)
        assert r.status_code == 400, r.get_json()

        print("✓ test_save_rejects_bad_routes passed")


def test_save_accepts_legacy_flat_payload():
    with _TempConfig() as p:
        p.write_text(json.dumps({
            "api_key": "sk-old-12345",
            "base_url": "https://old.example.com",
            "model": "old-model",
        }, ensure_ascii=False), encoding="utf-8")
        client = _client()
        r = client.post("/api/settings/save", json={
            "base_url": "https://new.example.com",
            "model": "new-model",
            "context_budget_tokens": 123456,
        })
        assert r.status_code == 200, r.get_json()
        saved = json.loads(p.read_text(encoding="utf-8"))
        assert saved["base_url"] == "https://new.example.com"
        assert saved["model"] == "new-model"
        assert saved["context_budget_tokens"] == 123456
        assert saved["providers"]["primary"]["api_key"] == "sk-old-12345", "旧 key 必须保留"
        print("✓ test_save_accepts_legacy_flat_payload passed")


if __name__ == "__main__":
    test_config_endpoint_masks_keys()
    test_save_v2_roundtrip()
    test_save_preserves_masked_key()
    test_save_rejects_bad_routes()
    test_save_accepts_legacy_flat_payload()
    print("\nAll settings route tests passed!")
