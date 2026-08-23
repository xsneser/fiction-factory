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
import z from "@deepseek-ai/schemastery";
import { installModelSelection } from "@deepseek-ai/dsh-agent";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";

/** 稳定插件名。 */
const name = "events-runner";
/** 核心服务 + headlessStartup（提供 task）。 */
const inject = ["agentDefaultModel", "agents", "sessions", "headlessStartup"];
const Config = z.object({ task: z.string().required() });
/** 进程 IO（测试可替换）。 */
const internals = { stdout: process.stdout, stderr: process.stderr };

/** 只转发的 session 事件类型（不推模型中间输出，见 docs/架构总览.md §三）。 */
const FORWARD = new Set(["tool/call", "tool/result"]);

/** 把一条 payload 写为一行 NDJSON。 */
function emit(io, payload) {
	io.stdout.write(JSON.stringify(payload) + "\n");
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
	// 捕获 dsh agent 的 token usage（assistant/message 的 data.usage），挂到下一个 tool/call 事件
	let lastUsage = null;
	ctx.on("session/event", (session, event) => {
		if (event.seq < firstSeq) return;
		if (event.type === "assistant/message" && event.data?.usage) {
			const u = event.data.usage;
			lastUsage = {
				input: u.inputTokens,
				output: u.outputTokens,
				cache_read: u.cacheReadTokens,
				cache_write: u.cacheWriteTokens
			};
		}
		if (!FORWARD.has(event.type)) return;
		let data = event.data;
		if (event.type === "tool/call" && lastUsage) {
			data = { ...event.data, usage: lastUsage };
			lastUsage = null;
		}
		emit(io, { type: event.type, data });
	});
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
