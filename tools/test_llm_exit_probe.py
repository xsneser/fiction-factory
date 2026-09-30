#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 出口预检（probe_llm_exit）离线测试 —— **零网络**，传输层用 post_fn 注入。

钉住的语义（这些是踩过坑的判据，别放宽）：
  1. `tool_call_ok` 只在**真的出现合法 ping tool_call** 时为真。「有 text/reasoning 输出」
     不等于工具调用可用——中转完全可能收下 tools 却忽略它，而 Writer/编排一步都走不动；
  2. 零 block（finish_reason=stop）= EMPTY_RESPONSE，与 dsh 适配器同判据；
  3. tool_choice 被中转拒（4xx 且报文提到该参数）才回落 prompt-only 形状，且总请求数 ≤2；
  4. 传输层失败允许有限重试，**语义失败（有输出但没调工具）不重试**（重试只是浪费）；
  5. 缓存：成功长 TTL、失败短 TTL；model/upstream 变了立刻重验；离线不放大请求；
  6. `llm_exit_info()` 只回 model/upstream，绝不回 key。

用法：python tools/test_llm_exit_probe.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.models import APIConfig                      # noqa: E402
from libraries import token_proxy as TP                # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ── 假传输：按 (stream, with_tools, tool_choice) 决定回什么 ──
SSE_EMPTY = 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
SSE_TEXT = ('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
SSE_TOOL = ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            '"function":{"name":"ping","arguments":"{\\"text\\":\\"ok\\"}"}}]},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n')
JSON_TEXT = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})


def transport(body, status=200):
    """返回 (post_fn, calls) —— calls 记录每次请求的 payload。"""
    calls = []

    def fn(url, payload, timeout):
        calls.append(payload)
        return status, body
    return fn, calls


def patch_exit_info(**over):
    """临时替换 llm_exit_info（模拟改模型/换中转）→ 返回恢复函数。

    `role` 形参必须收下：生产代码按角色探测出口（`llm_exit_info(role)`），
    假函数签名不带它就会 TypeError。
    """
    orig = TP.llm_exit_info

    def fake(role="orchestrator"):
        return {"role": role,
                "provider_id": over.get("provider_id", "p-test"),
                "provider_name": over.get("provider_name", "Test Provider"),
                "model": over.get("model", "m-test"),
                "upstream": over.get("upstream", "https://u.test")}
    TP.llm_exit_info = fake
    return lambda: setattr(TP, "llm_exit_info", orig)


# ══════════════════════════════════════════════
print("\n═══ 1. 判定：empty / text-only / tool_call / HTTP 错误 ═══")
TP.clear_probe_cache()
restore = patch_exit_info()

fn, _ = transport(SSE_EMPTY)
r = TP.probe_llm_exit(use_cache=False, retries=0, post_fn=fn)
check("零 block → 不通过且判 EMPTY_RESPONSE",
      r["ok"] is False and r["transport_ok"] is False and r["code"] == "EMPTY_RESPONSE",
      str(r))

fn, _ = transport(SSE_TEXT)
r = TP.probe_llm_exit(use_cache=False, retries=0, post_fn=fn)
check("只有 text、没有 tool_call → **判失败**（tool_call_ok=False）",
      r["ok"] is False and r["transport_ok"] is True and r["tool_call_ok"] is False
      and r["code"] == "NO_TOOL_CALL", str(r))
check("失败文案说清「没有执行工具调用」", "没有执行工具调用" in (r.get("message") or ""), r.get("message"))

fn, _ = transport(SSE_TOOL)
r = TP.probe_llm_exit(use_cache=False, retries=0, post_fn=fn)
check("合法 ping tool_call → 通过", r["ok"] is True and r["tool_call_ok"] is True, str(r))

fn, calls = transport('{"error":"boom"}', 500)
r = TP.probe_llm_exit(use_cache=False, retries=0, post_fn=fn)
check("HTTP 500 → 不通过且带状态码", r["ok"] is False and r["code"] == "HTTP_500", str(r))

# ── max_tokens：常规 ping 要小（不带 dsh 的 256000），但**不能小到烧完预算**──
check("常规 ping 的 max_tokens 小幅够用（512：64 会让推理模型烧完预算 → finish=length 假故障）",
      calls[-1]["max_tokens"] == TP._PROBE_PING_MAX_TOKENS and 512 <= calls[-1]["max_tokens"] <= 4096,
      str(calls[-1].get("max_tokens")))
check("常规 ping 请求是流式且带 stream_options",
      calls[-1].get("stream") is True and "stream_options" in calls[-1], str(calls[-1]))

print("\n═══ 2. tool_choice 不被支持时的回落 ═══")
fn, calls = transport('{"error":"unsupported parameter: tool_choice"}', 400)
r = TP.probe_llm_exit(use_cache=False, retries=0, post_fn=fn)
# 回落那一次也要给内容：换一个会回 tool_call 的假传输
fn2, calls2 = transport(SSE_TOOL)
seen = {"n": 0}


def fn_mixed(url, payload, timeout):
    seen["n"] += 1
    if "tool_choice" in payload:
        return 400, '{"error":"unsupported parameter: tool_choice"}'
    return 200, SSE_TOOL


TP.clear_probe_cache()
r = TP.probe_llm_exit(use_cache=False, retries=0, post_fn=fn_mixed)
check("tool_choice 被拒 → 回落 prompt-only 并仍可判定通过",
      r["ok"] is True and r["shape"] == "prompt_only", str(r))
check("回落路径总请求数 = 2", seen["n"] == 2, str(seen))

print("\n═══ 3. 重试：传输失败重试、语义失败不重试 ═══")
TP.clear_probe_cache()
fn, calls = transport(SSE_EMPTY)
TP.probe_llm_exit(use_cache=False, retries=2, post_fn=fn)
check("零 block（传输层失败）会重试到上限：1+2=3 次", len(calls) == 3, str(len(calls)))

TP.clear_probe_cache()
fn, calls = transport(SSE_TEXT)
TP.probe_llm_exit(use_cache=False, retries=2, post_fn=fn)
check("有输出但没调工具（语义失败）不重试：只 1 次", len(calls) == 1, str(len(calls)))

print("\n═══ 4. 缓存：成功复用、失败短缓存、配置变更立即重验 ═══")
TP.clear_probe_cache()
fn, calls = transport(SSE_TOOL)
a = TP.probe_llm_exit(post_fn=fn)
b = TP.probe_llm_exit(post_fn=fn)
check("成功结果命中缓存：第二次不再发请求", len(calls) == 1 and b["ok"] is True, str(len(calls)))

TP.clear_probe_cache()
fn, calls = transport(SSE_EMPTY)
TP.probe_llm_exit(retries=0, post_fn=fn)
TP.probe_llm_exit(retries=0, post_fn=fn)
check("失败结果也走缓存（短 TTL），不放大请求", len(calls) == 1, str(len(calls)))

TP.clear_probe_cache()
TP._PROBE_CACHE.clear()
fn, calls = transport(SSE_TOOL)
TP.probe_llm_exit(post_fn=fn)
restore()
restore = patch_exit_info(model="m-other")        # 设置页换模型 → key 变
TP.probe_llm_exit(post_fn=fn)
check("model 变更后缓存 key 变化 → 重新验证", len(calls) == 2, str(len(calls)))

print("\n═══ 5. diagnose 分层（A/B/C，C 只在 A、B 都失败时跑）═══")
TP.clear_probe_cache()


def layered(a_body, b_body, c_body):
    def fn(url, payload, timeout):
        if payload.get("tools"):
            return 200, a_body
        return (200, c_body) if not payload.get("stream") else (200, b_body)
    return fn


r = TP.probe_llm_exit(mode="diagnose", post_fn=layered(SSE_TEXT, SSE_TEXT, JSON_TEXT))
check("A 无工具调用、B 流式正常 → 判「工具调用路径异常」",
      "工具调用路径异常" in (r["layer"] or ""), str(r["layer"]))
r = TP.probe_llm_exit(mode="diagnose", post_fn=layered(SSE_EMPTY, SSE_EMPTY, JSON_TEXT))
check("A/B 都空、C 非流式正常 → 判「流式路径异常」（不是「上游整体异常」）",
      "流式路径异常" in (r["layer"] or ""), str(r["layer"]))
r = TP.probe_llm_exit(mode="diagnose", post_fn=layered(SSE_EMPTY, SSE_EMPTY, ""),)
check("A/B/C 全败 → 才说「基础连接/模型/上游异常」",
      "基础连接" in (r["layer"] or ""), str(r["layer"]))
r = TP.probe_llm_exit(mode="diagnose", post_fn=layered(SSE_TOOL, SSE_TEXT, JSON_TEXT))
check("A 通过 → 不再跑 B/C（layer 为空）", r["ok"] is True and not r["layer"], str(r))

print("\n═══ 6. 自由生成段（mode='free'）与预算判定 ═══")
TP.clear_probe_cache()
fn, calls = transport(SSE_EMPTY)
r = TP.probe_llm_exit(mode="free", use_cache=False, retries=0, post_fn=fn)
check("自由生成零 block → 不通过，且文案说清是模型/中转侧问题",
      r["ok"] is False and "自由生成" in (r.get("message") or ""), str(r.get("message")))
check("自由生成段不带 tool_choice", "tool_choice" not in calls[-1], str(calls[-1].keys()))

fn, calls = transport(SSE_TEXT)
r = TP.probe_llm_exit(mode="free", use_cache=False, retries=0, post_fn=fn)
check("自由生成有正文 → 通过（正文就是写作要的东西）", r["ok"] is True, str(r))

# 真实工具集透传 + 小预算（代表形状）
fn, calls = transport(SSE_TEXT)
tools = [{"type": "function", "function": {"name": "t1", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]
r = TP.probe_llm_exit(mode="free", tools=tools, user="写一句", use_cache=False,
                      retries=0, post_fn=fn, max_tokens=2048)
check("代表形状把真实工具集带上、不强制 tool_choice",
      len(calls[-1]["tools"]) == 1 and "tool_choice" not in calls[-1], str(calls[-1].keys()))
check("max_tokens 可显式指定（2048）", calls[-1]["max_tokens"] == 2048, str(calls[-1]["max_tokens"]))

# finish=length + 零 block = 预算被烧完（探针自身问题）→ 加大预算重试，不当成出口故障
SSE_LENGTH = ('data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\ndata: {"usage":{"prompt_tokens":10,"completion_tokens":512}}\n\ndata: [DONE]\n\n')
fn, calls = transport(SSE_LENGTH)
r = TP.probe_llm_exit(use_cache=False, retries=0, post_fn=fn)
check("length+零 block 判 BUDGET_EXHAUSTED（不是 EMPTY_RESPONSE）",
      r.get("code") == "BUDGET_EXHAUSTED", str(r.get("code")))
check("预算不足 → 自动用更大预算重试一次", len(calls) == 2 and calls[-1]["max_tokens"] > calls[0]["max_tokens"],
      f"{[c['max_tokens'] for c in calls]}")
check("预算仍不足时**不拦**写作（按通过处理 + warning）",
      r["ok"] is True and r.get("inconclusive") and r.get("warning"), str(r))

print("\n═══ 7. mode='contrast' 对照归类 ═══")
TP.clear_probe_cache()
fn, calls = transport(SSE_TOOL)
r = TP.probe_llm_exit(mode="contrast", use_cache=False, post_fn=fn)
check("三组都有输出 → 判「出口本身正常，更像间歇」",
      "间歇" in (r.get("verdict") or ""), str(r.get("verdict")))
check("对照用够用的预算（不再是 64）", all(c["max_tokens"] >= 512 for c in calls),
      str([c["max_tokens"] for c in calls]))

TP.clear_probe_cache()
seen = {"n": 0}


def fn_forced_ok_free_empty(url, payload, timeout):
    seen["n"] += 1
    return (200, SSE_TOOL) if payload.get("tool_choice") else (200, SSE_EMPTY)


r = TP.probe_llm_exit(mode="contrast", use_cache=False, post_fn=fn_forced_ok_free_empty)
check("强制工具通、自由生成空 → 判「上游只对强制工具调用出内容」",
      "只对强制工具调用出内容" in (r.get("verdict") or ""), str(r.get("verdict")))

print("\n═══ 8. 请求形状留痕（只计数，不含正文）═══")
TP.clear_request_shapes()
rec = TP.record_request_shape(
    payload={"model": "m", "messages": [{"role": "user", "content": "秘" * 50}],
             "tools": [{"type": "function"}], "stream": True, "max_tokens": 512},
    upstream="https://relay.example.com/v1/chat/completions", status=200,
    body=SSE_EMPTY, prompt_tokens=1542, completion_tokens=0)
check("形状记录含 tools/msgs_chars/max_tokens/blocks/finish/empty",
      rec["tools"] == 1 and rec["msgs_chars"] == 50 and rec["max_tokens"] == 512
      and rec["blocks"]["text"] == 0 and rec["finish"] == "stop" and rec["empty"] is True, str(rec))
check("upstream 只留 origin（不带路径）", rec["upstream"] == "https://relay.example.com", rec["upstream"])
check("**不含正文**：记录里没有任何消息内容",
      "秘" not in json.dumps(rec, ensure_ascii=False), json.dumps(rec, ensure_ascii=False))
check("有界环：只留最近 N 条", len(TP.recent_request_shapes(20)) == 1)
TP.clear_request_shapes()

print("\n═══ 9. llm_exit_info 不含敏感字段 ═══")
restore()
cfg = APIConfig(api_key="sk-secret", base_url="https://relay.example.com", model="m1",
                url_strict=False, max_tokens=0, http_timeout_seconds=60,
                context_budget_tokens=1000, verify_ssl=False)
info = TP.llm_exit_info()
# 分流之后诊断信息需要说明「哪个角色、打到哪个供应商的哪个模型」——但多出来的
# 也只是角色与供应商标识，仍然不许出现 key。
check("只回角色/供应商/模型/上游四类标识",
      set(info) <= {"role", "provider_id", "provider_name", "model", "upstream"}, str(info))
check("必含 model/upstream",
      {"model", "upstream"} <= set(info), str(info))
check("不含 key", "sk-secret" not in json.dumps(info), str(info))
check("role 可指定", TP.llm_exit_info("writer").get("role") == "writer",
      str(TP.llm_exit_info("writer")))
_ = cfg

print()
if FAILURES:
    print(f"  ❌ {len(FAILURES)} 项失败：")
    for f in FAILURES:
        print("     - " + f)
    raise SystemExit(1)
print("  ✅ LLM 出口预检语义全部通过（判定 / 回落 / 重试 / 缓存 / 分层）")
