"""Offline tests for OpenAI-compatible model discovery and settings endpoint."""
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask
import core.api_config as ac
from core.models import APIProviderConfig, ProviderModel
from core.api_config import api_settings_from_mapping
from core.provider_discovery import (
    DiscoveryError, discover_provider_models, merge_discovered_models,
    resolve_provider_endpoint,
)
import ui.web_blueprints.settings as settings_bp


class CatalogHandler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        CatalogHandler.calls.append((self.path, self.headers.get("Authorization")))
        if self.path == "/v1/models":
            body = {"object": "list", "data": [
                {"id": "model-a", "object": "model", "owned_by": "test"},
                {"id": "model-b", "object": "model"},
            ], "has_more": True, "last_id": "model-b"}
        elif self.path == "/v1/models?after=model-b":
            body = {"data": [{"id": "model-c"}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def test_discovery_and_merge():
    httpd = server()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}/v1"
        provider = APIProviderConfig(base_url=base, api_key="sk-test", default_model="model-a")
        assert resolve_provider_endpoint(provider) == base + "/models"
        strict = APIProviderConfig(base_url=f"http://127.0.0.1:{httpd.server_port}", url_strict=True)
        assert resolve_provider_endpoint(strict) == f"http://127.0.0.1:{httpd.server_port}/models"
        full = APIProviderConfig(base_url=base + "/chat/completions")
        assert resolve_provider_endpoint(full) == base + "/models"
        result = discover_provider_models(provider, timeout=3)
        assert [m.id for m in result.models] == ["model-a", "model-b", "model-c"]
        assert CatalogHandler.calls[0][1] == "Bearer sk-test"
        old = [ProviderModel(id="manual", name="My local", source="manual"),
               ProviderModel(id="model-a", name="Edited", context_window=777, source="manual")]
        merged = merge_discovered_models(old, result.models)
        assert [m.id for m in merged] == ["manual", "model-a", "model-b", "model-c"]
        assert merged[1].name == "Edited" and merged[1].context_window == 777
    finally:
        httpd.shutdown()
    print("✓ discovery pagination and non-destructive merge passed")


def test_settings_endpoint():
    httpd = server()
    old_override = ac.API_PATH_OVERRIDE
    try:
        path = Path(tempfile.mkdtemp()) / "api.json"
        ac.API_PATH_OVERRIDE = path
        path.write_text(json.dumps({"api_key": "", "base_url": "", "model": "m"}), encoding="utf-8")
        app = Flask(__name__)
        app.register_blueprint(settings_bp.bp)
        client = app.test_client()
        base = f"http://127.0.0.1:{httpd.server_port}/v1"
        response = client.post("/api/settings/models/discover", json={
            "provider_id": "draft",
            "provider": {"base_url": base, "api_key": "sk-draft", "models": []},
        })
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["count"] == 3
        assert "sk-draft" not in json.dumps(body)
    finally:
        ac.API_PATH_OVERRIDE = old_override
        httpd.shutdown()
    print("✓ settings discovery endpoint passed")


def test_refresh_endpoint_persists_catalog():
    httpd = server()
    old_override = ac.API_PATH_OVERRIDE
    try:
        path = Path(tempfile.mkdtemp()) / "api.json"
        ac.API_PATH_OVERRIDE = path
        base = f"http://127.0.0.1:{httpd.server_port}/v1"
        path.write_text(json.dumps({"api_key": "sk-test", "base_url": base,
                                    "model": "model-a"}), encoding="utf-8")
        app = Flask(__name__)
        app.register_blueprint(settings_bp.bp)
        response = app.test_client().post("/api/settings/models/refresh", json={"persist": True})
        assert response.status_code == 200, response.get_json()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["version"] == 2
        assert len(saved["providers"]["primary"]["models"]) == 3
        assert saved["model"] == "model-a"
    finally:
        ac.API_PATH_OVERRIDE = old_override
        httpd.shutdown()
    print("✓ refresh endpoint persists legacy upgrade")


def test_v2_missing_url_does_not_default():
    settings = api_settings_from_mapping({
        "version": 2, "default_provider": "p",
        "providers": {"p": {"default_model": "m", "models": [{"id": "m"}]}},
    })
    assert settings.providers["p"].base_url == ""
    print("✓ v2 missing provider URL remains empty")


def test_settings_rejects_malformed_fields():
    old_override = ac.API_PATH_OVERRIDE
    try:
        path = Path(tempfile.mkdtemp()) / "api.json"
        ac.API_PATH_OVERRIDE = path
        path.write_text("{}", encoding="utf-8")
        app = Flask(__name__)
        app.register_blueprint(settings_bp.bp)
        client = app.test_client()
        payload = {
            "default_provider": "p",
            "providers": {"p": {
                "name": "P", "base_url": "https://example.test/v1", "api_key": "sk-x",
                "models": [{"id": "m", "max_tokens": "not-a-number"}],
                "default_model": "m", "enabled": "false",
            }},
            "backend": {"provider_id": "p", "model": "m"},
        }
        response = client.post("/api/settings/save", json=payload)
        assert response.status_code == 400, response.get_json()
        assert response.get_json().get("issues")
    finally:
        ac.API_PATH_OVERRIDE = old_override
    print("✓ malformed settings return structured 400")


def test_rejects_url_credentials():
    try:
        resolve_provider_endpoint(APIProviderConfig(base_url="https://user:pass@example.test/v1"))
    except DiscoveryError as exc:
        assert exc.code == "url_credentials"
    else:
        raise AssertionError("URL credentials must be rejected")
    print("✓ URL credentials rejected")


if __name__ == "__main__":
    test_discovery_and_merge()
    test_settings_endpoint()
    test_refresh_endpoint_persists_catalog()
    test_v2_missing_url_does_not_default()
    test_settings_rejects_malformed_fields()
    test_rejects_url_credentials()
    print("\nProvider discovery tests passed!")
