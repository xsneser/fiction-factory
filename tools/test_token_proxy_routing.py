"""端到端验证 token 代理的角色分流：两个 mock 上游，两个角色各走各的。

这是「子代理模型分流」的核心断言：token_proxy 收到 `model=novelengine-route:<role>`
后，必须改写成该角色绑定的**真实供应商**的地址 / 密钥 / 模型 / 思考参数，且不能
把保留别名透传给上游。
"""
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.api_config as acmod


class _Capture:
    """记录 mock 上游收到的每一个请求。"""
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def add(self, rec):
        with self.lock:
            self.requests.append(rec)

    def last(self):
        with self.lock:
            return self.requests[-1] if self.requests else None


def _mock_upstream(capture, tag):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get('Content-Length', 0) or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
            capture.add({
                'tag': tag,
                'path': self.path,
                'auth': self.headers.get('Authorization', ''),
                'model': body.get('model'),
                'payload': body,
            })
            payload = json.dumps({
                "id": "x", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def _post(proxy_port, model, extra=None):
    import requests
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
    if extra:
        body.update(extra)
    r = requests.post(f"http://127.0.0.1:{proxy_port}/chat/completions",
                      json=body, timeout=20)
    return r


def main():
    cap_a, cap_b = _Capture(), _Capture()
    srv_a, port_a = _mock_upstream(cap_a, 'A')
    srv_b, port_b = _mock_upstream(cap_b, 'B')

    tmp = tempfile.TemporaryDirectory()
    cfg = Path(tmp.name) / "api.json"
    cfg.write_text(json.dumps({
        "version": 2,
        "default_provider": "pa",
        "providers": {
            "pa": {"id": "pa", "name": "Provider A", "enabled": True,
                   "base_url": f"http://127.0.0.1:{port_a}",
                   "api_key": "sk-key-for-A",
                   "default_model": "model-a",
                   "reasoning_wire": "deepseek",
                   "models": [{"id": "model-a", "name": "A"},
                              {"id": "model-a-heavy", "name": "A heavy"}]},
            "pb": {"id": "pb", "name": "Provider B", "enabled": True,
                   "base_url": f"http://127.0.0.1:{port_b}/v1",
                   "api_key": "sk-key-for-B",
                   "default_model": "model-b",
                   "reasoning_wire": "openai",
                   "models": [{"id": "model-b", "name": "B"}]},
        },
        "subagents": {
            "orchestrator": {"provider_id": "pa", "model": "model-a"},
            "writer": {"provider_id": "pa", "model": "model-a-heavy",
                       "max_tokens": 12345, "reasoning_effort": "high"},
            "critic": {"provider_id": "pb", "model": "model-b",
                       "reasoning_effort": "medium"},
        },
        "backend": {"provider_id": "pa", "model": "model-a"},
    }, ensure_ascii=False), encoding="utf-8")

    acmod.API_PATH_OVERRIDE = cfg
    import libraries.token_proxy as tp
    proxy_port = 58991
    tp.PROXY_PORT = proxy_port
    # 换到测试端口，避免与平台正在跑的 58082 撞车
    assert tp.ensure_proxy(), "代理起不来（端口被占用？）"
    try:
        # 1) writer → Provider A，别名被换成真实模型，思考参数按 deepseek 协议注入
        _post(proxy_port, "novelengine-route:writer")
        rec = cap_a.last()
        assert rec is not None, "Provider A 没收到请求"
        assert rec['model'] == 'model-a-heavy', rec['model']
        assert rec['auth'] == 'Bearer sk-key-for-A', rec['auth']
        assert rec['payload']['max_tokens'] == 12345, rec['payload'].get('max_tokens')
        assert rec['payload'].get('thinking') == {'type': 'enabled'}, rec['payload'].get('thinking')
        assert rec['payload'].get('reasoning_effort') == 'high', rec['payload'].get('reasoning_effort')
        assert 'novelengine-route' not in json.dumps(rec['payload']), "保留别名泄漏给上游！"
        print("✓ writer → Provider A：模型/密钥/思考参数全部按角色改写")

        # 2) critic → Provider B（另一个上游、另一个 key、openai 接线、路径带 /v1）
        _post(proxy_port, "novelengine-route:critic")
        rec = cap_b.last()
        assert rec is not None, "Provider B 没收到请求"
        assert rec['model'] == 'model-b', rec['model']
        assert rec['auth'] == 'Bearer sk-key-for-B', rec['auth']
        assert rec['path'].endswith('/chat/completions'), rec['path']
        assert rec['payload'].get('reasoning_effort') == 'medium', rec['payload']
        assert 'thinking' not in rec['payload'], "openai 接线不该发 thinking"
        print("✓ critic → Provider B：换供应商 + 换密钥 + openai 接线")

        # 3) orchestrate 角色走 Provider A 的默认模型
        _post(proxy_port, "novelengine-route:orchestrator")
        assert cap_a.last()['model'] == 'model-a', cap_a.last()['model']
        print("✓ orchestrator → Provider A 默认模型")

        # 4) 普通模型名（旧 dsh profile 会这么发）→ 按 orchestrator 路由兜底，绝不透传
        _post(proxy_port, "deepseek-v4-flash")
        rec = cap_a.last()
        assert rec['model'] == 'model-a', rec['model']
        assert rec['auth'] == 'Bearer sk-key-for-A'
        print("✓ 非别名模型名 → 兜底到编排路由（不透传上游）")

        # 5) /health 与 token-usage 暴露角色/供应商，但绝不暴露密钥
        import requests
        health = requests.get(f"http://127.0.0.1:{proxy_port}/health", timeout=10).json()
        health_raw = json.dumps(health)
        assert 'sk-key-for-A' not in health_raw and 'sk-key-for-B' not in health_raw
        assert health['last_request']['role'] == 'orchestrator', health['last_request']
        assert health['last_request']['provider_id'] == 'pa', health['last_request']
        assert health['providers_count'] == 2, health

        usage = requests.get(f"http://127.0.0.1:{proxy_port}/token-usage", timeout=10).json()
        assert 'by_route' in usage, usage.keys()
        assert usage['by_route']['writer']['model'] == 'model-a-heavy', usage['by_route']
        assert usage['by_route']['critic']['provider'] == 'pb', usage['by_route']
        print("✓ /health 与 /token-usage 按角色归账，且不含明文密钥")
    finally:
        tp.USAGE.clear()
        acmod.API_PATH_OVERRIDE = None
        srv_a.shutdown()
        srv_b.shutdown()
        tmp.cleanup()


if __name__ == "__main__":
    main()
    print("\ntoken proxy routing test passed!")
