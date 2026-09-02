/**
 * events-runner — dsh headless 事件流 runner（NovelEngine 定制）。
 *
 * 替换 @deepseek-ai/dsh-headless 的 headless-runner：同样是 one-shot 直驱 agent，
 * 但把「summarize + 只打印最终文本」换成「监听 session/event，把 tool/call、
 * tool/result 逐事件写成一行 NDJSON 推 stdout」，收尾再补 reply / done / error。
 *
 * 挂载：经 --patch 先 `disabled: true` 掉 headless-runner，再 `insert` 本插件
 * （name 用 file:/// 绝对 URL，见 agent-sidecar/events-runner.yml 模板与
 * libraries/dsh_bridge.py 运行期 overlay）。run 流程照抄
 * node_modules/@deepseek-ai/dsh-headless/lib/index.js，仅改输出层。
 */
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import z from "@deepseek-ai/schemastery";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage, isAgentLoopRequest } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";

/** 稳定插件名。 */
const name = "events-runner";
/** 核心服务 + headlessStartup（提供 task）。 */
const inject = ["agentDefaultModel", "agents", "sessions", "headlessStartup"];
const Config = z.object({ task: z.string().required() });
/** 进程 IO（测试可替换）。 */
const internals = { stdout: process.stdout, stderr: process.stderr };

/**
 * 调试模式开关（dsh_bridge 注入 NOVEL_AGENT_DEBUG=1 才开启）：LLM 请求详情
 * （提示词 system+messages、MCP 工具 schema）与响应随 llm/call 事件流出，
 * 供前端「LLM 调用」调试卡呈现。关闭时不注册监听、不发数据，零开销。
 */
const DEBUG = process.env.NOVEL_AGENT_DEBUG === "1";
/** 调试卡完整载荷目录（dsh_bridge 注入，绝对路径）：完整 request/response 写文件、SSE 只发裁剪预览。 */
const DEBUG_PROMPT_DIR = process.env.DEBUG_PROMPT_DIR || "";

/** 只转发的 session 事件类型（不推模型中间输出，见 docs/架构总览.md §三）。 */
const FORWARD = new Set(["tool/call", "tool/result"]);

/** 把一条 payload 写为一行 NDJSON。 */
function emit(io, payload) {
	io.stdout.write(JSON.stringify(payload) + "\n");
}

/**
 * 调试卡载荷裁剪（防巨大 SSE 事件拖垮管线）：把任意 JSON 里的字符串按预算截断，
 * 保留结构（模型名/工具名/最近消息/响应最终文本），裁掉长内容。长写作任务里
 * llm/call 的 request.messages 含 50KB+ 的工具结果，不裁会撑爆 stdout 管道 / SSE / 浏览器 DOM。
 */
function capText(s, n) {
	if (typeof s !== "string") return s;
	return s.length <= n ? s : s.slice(0, n) + `…[+${s.length - n} chars]`;
}
function capDeep(v, n) {
	if (Array.isArray(v)) return v.map((x) => capDeep(x, n));
	if (v && typeof v === "object") {
		const out = {};
		for (const k in v) if (Object.prototype.hasOwnProperty.call(v, k)) out[k] = capDeep(v[k], n);
		return out;
	}
	return capText(v, n);
}
/** 工具 schema 精简：只留 name + description（参数 schema 每轮不变、体积大，调试卡不需要）。 */
function capTools(tools) {
	if (!Array.isArray(tools)) return tools;
	return tools.map((t) => t && typeof t === "object" ? {
		name: t.name,
		description: capText(t.description, 300)
	} : t);
}

/** 收尾：沿用 headless 的 summarize —— 最后一个非空 assistant/message 文本 + turn 结束原因。 */
function summarize(events, firstSeq) {
	let started = false;
	let text = "";
	let reason;
	for (const event of events) {
		if (event.seq < firstSeq) continue;
		if (event.type === "turn/start") {
			started = true;
			continue;
		}
		if (!started) continue;
		if (event.type === "assistant/message") {
			const joined = event.data.message.content
				.filter((block) => block.type === "text")
				.map((block) => block.text).join("");
			if (joined !== "") text = joined;
		}
		if (event.type === "turn/end") reason = event.data.reason;
	}
	return { text, reason };
}

/** 报告失败并请求非零退出（done 行 flush 后再 exit，防丢最后事件）。 */
function fail(io, error) {
	emit(io, {
		type: "error",
		data: { message: error instanceof Error ? error.message : String(error) }
	});
	io.stderr.write(`dsh: ${error instanceof Error ? error.message : String(error)}\n`);
	io.stdout.write(JSON.stringify({ type: "done", data: { code: 1 } }) + "\n", () => io.exit(1));
}

/**
 * 跑一次 one-shot 任务；事件流在 followup 前挂监听（session.append 同步通知
 * 观察者，挂了不丢事件）。
 * @param ctx - plugin context（agent 核心服务 + launcher IO）。
 * @param task - one-shot 任务文本。
 * @param io - 进程面效果（stdout/stderr/exit）。
 */
async function run(ctx, task, io) {
	await ctx.get("loader")?.await();
	const agents = ctx.get("agents");
	const defaultModel = ctx.get("agentDefaultModel");
	const sessions = ctx.get("sessions");
	if (agents === void 0 || defaultModel === void 0 || sessions === void 0) return;
	const selection = defaultModel.currentSelection();
	const { agent } = await agents.create({
		sessionId: SessionId(`session-${randomUUID()}`),
		meta: { cwd: process.cwd() },
		agentOptions: {
			provider: selection.provider,
			model: selection.model
		},
		setup: (agentCtx) => {
			installModelSelection(agentCtx, {
				current: selection,
				assembled: void 0
			});
		}
	});
	await agent.whenIdle();
	const firstSeq = agent.session.seq;
	// 捕获 dsh agent 的 token usage，挂到 tool/call 事件：
	// usage 来源有两处——流式 assistant/chunk(type=usage) 的 chunk.usage、整段 assistant/message 的 data.usage。
	// 归因：每个 tool/call 前必有 LLM 回合，取最近一次 usage；同回合并行 tool/call 共享（不清 lastUsage，
	// 待无 usage 的 assistant/message 到达才清，防串到下一回合）。
	let lastUsage = null;
	// 调试模式：llm/stream 快照请求（提示词 + MCP 工具），assistant/message 配对响应 →
	// 每条 LLM 调用 emit 一条 llm/call。llmSeq 逐调用递增、pendingLlm 单槽（agent-loop 严格
	// 串行，一次只有一轮在跑；isAgentLoopRequest 过滤掉内部 summarize/探测调用防串号）。
	let llmSeq = 0;
	let pendingLlm = null;
	ctx.on("session/event", (session, event) => {
		if (event.seq < firstSeq) return;
		let u = null;
		if (event.type === "assistant/message") {
			u = event.data?.usage ?? null;
		} else if (event.type === "assistant/chunk" && event.data?.chunk?.type === "usage") {
			u = event.data.chunk.usage ?? null;
		}
		if (u) {
			lastUsage = {
				input: u.inputTokens,
				output: u.outputTokens,
				cache_read: u.cacheReadTokens,
				cache_write: u.cacheWriteTokens
			};
		} else if (event.type === "assistant/message") {
			lastUsage = null;   // 本回合无 usage：清掉，避免上一回合残留串到后续 tool/call
		}
		// 调试模式：assistant/message 是 LLM 回合终点，配对 llm/stream 快照 emit llm/call
		//（response = 组装后的完整 assistant 消息，含 tool-call 块与 arguments，即「返回 JSON 原文」）。
		// 载荷做有界裁剪（capDeep/capTools）：保留结构、裁长内容，防大上下文下事件体积失控。
		if (DEBUG && event.type === "assistant/message" && pendingLlm) {
			const msgs = Array.isArray(pendingLlm.messages) ? pendingLlm.messages.slice(-12) : pendingLlm.messages;
			// 完整载荷（未裁剪的 request + response）写文件供调试卡按需 fetch，SSE 仍只发裁剪预览，
			// 避免 50KB+ 工具结果把 stdout 管道 / SSE / 浏览器 DOM 撑爆；写失败则降级为预览。
			if (DEBUG_PROMPT_DIR) {
				try {
					fs.mkdirSync(DEBUG_PROMPT_DIR, { recursive: true });
					fs.writeFileSync(path.join(DEBUG_PROMPT_DIR, `${pendingLlm.seq}.json`),
						JSON.stringify({ seq: pendingLlm.seq, request: pendingLlm, response: event.data.message }));
				} catch (e) { /* 忽略：调试卡退回事件内预览 */ }
			}
			emit(io, { type: "llm/call", data: {
				seq: pendingLlm.seq,
				turn: event.data.turn,
				step: event.data.step,
				request: {
					provider: pendingLlm.provider,
					model: pendingLlm.model,
					system: capDeep(pendingLlm.system, 2000),
					messages: capDeep(msgs, 600),
					tools: capTools(pendingLlm.tools)
				},
				response: capDeep(event.data.message, 1500),
				usage: lastUsage
			}});
			pendingLlm = null;
		}
		if (!FORWARD.has(event.type)) return;
		let data = event.data;
		if (event.type === "tool/call" && lastUsage) {
			data = { ...event.data, usage: lastUsage };
		}
		emit(io, { type: event.type, data });
	});
	// 调试模式：监听 llm/stream（与 dsh-agent-loop/lib/invariant.js 同款 global 注册），
	// 只快照不改请求（options 是 deepFreeze，直接引用即可）。关闭时不注册，零开销。
	if (DEBUG) {
		ctx.on("llm/stream", (options, next) => {
			if (!isAgentLoopRequest(options)) return next();   // 排除内部 summarize/探测调用
			llmSeq += 1;
			pendingLlm = {
				seq: llmSeq,
				provider: options.provider,
				model: options.model,
				system: options.system ?? null,
				messages: options.messages ?? [],
				tools: options.tools ?? []
			};
			return next();
		}, { global: true, prepend: true });
	}
	agent.followup(createUserMessage({
		content: [{ type: "text", text: task }],
		source: { kind: "user" }
	}));
	await agent.whenIdle();
	await sessions.flush(agent.session);
	const outcome = summarize(agent.session.events, firstSeq);
	if (outcome.reason?.kind === "error") {
		emit(io, {
			type: "error",
			data: { message: `${outcome.reason.error.code}: ${outcome.reason.error.message}` }
		});
	}
	if (outcome.text) emit(io, { type: "reply", data: { text: outcome.text } });
	// stdout.write 到 pipe 是异步串行的：done 行带 callback，触发时此前 reply/done
	// 均已 flush，再 exit——避免立即 exit 丢最后事件（C4）。
	const donePayload = { type: "done", data: { code: outcome.reason?.kind === "completed" ? 0 : 1 } };
	io.stdout.write(JSON.stringify(donePayload) + "\n",
		() => io.exit(outcome.reason?.kind === "completed" ? 0 : 1));
}

/**
 * 挂载 one-shot 事件流驱动。
 * @param ctx - plugin context（核心服务 + launcher 提供的 exit）。
 * @param config - 已校验的 task 配置。
 */
function apply(ctx, config) {
	const exit = ctx.get("appExit");
	if (exit === void 0) throw new Error("events-runner: the launcher must provide ctx.appExit before the tree mounts");
	const io = {
		stdout: internals.stdout,
		stderr: internals.stderr,
		exit
	};
	run(ctx, config.task, io).catch((error) => {
		fail(io, error);
	});
}

export { Config, apply, inject, internals, name };
