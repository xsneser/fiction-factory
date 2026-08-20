// Agent 聊天助手面板（OpenClaw 式）：侧栏对话，Agent 通过 function calling 操作引擎并导航页面。
// 后端 /api/agent/chat（SSE）。对话历史仅存 user/assistant 文本，工具步骤卡临时展示不入历史。
// 版本标记：新 JS（事件流实时工具卡）会在控制台打印 v3；旧 JS 无此输出——用于排查浏览器缓存。
console.log('[agent-panel] v3 events-stream');
(function() {
    var chat = document.getElementById('agent-chat');
    var input = document.getElementById('agent-input');
    var sendBtn = document.getElementById('agent-send');
    var clearBtn = document.getElementById('agent-clear');
    var toolsLog = document.getElementById('agent-tools-log');
    if (!chat || !input || !sendBtn) return;   // 布局缺失则静默跳过

    var HISTORY_KEY = 'ne_agent_history';
    var HISTORY_LIMIT = 40;
    var busy = false;
    var pendingTask = null;                     // busy 时排队待发任务（done 后接力）
    var currentToolRun = null;                  // 当前工具卡引用
    var toolPollTimer = null;                   // 工具日志轮询定时器
    var toolCards = {};                         // callId → 工具卡（事件流配对）
    var toolCardOrder = [];                     // 工具卡创建顺序（上限裁剪用）
    var TOOL_CARD_LIMIT = 20;                   // 对话页签工具卡上限（防 DOM 膨胀）

    function loadHistory() {
        try { return JSON.parse(sessionStorage.getItem(HISTORY_KEY) || '[]'); }
        catch (e) { return []; }
    }
    function saveHistory(h) {
        try { sessionStorage.setItem(HISTORY_KEY, JSON.stringify(h.slice(-HISTORY_LIMIT))); } catch (e) {}
    }
    var history = loadHistory();

    // ─── 渲染 ───
    function el(tag, cls, text) {
        var d = document.createElement(tag);
        if (cls) d.className = cls;
        if (text !== undefined) d.textContent = text;
        return d;
    }
    function scrollBottom() { chat.scrollTop = chat.scrollHeight; }

    function addMsg(role, text) {
        var wrap = el('div', 'agent-msg ' + (role === 'user' ? 'user' : 'assistant'));
        wrap.appendChild(el('div', 'agent-msg-label', role === 'user' ? '你' : '🤖 Agent'));
        var body = el('div', 'agent-msg-body', text || '');
        wrap.appendChild(body);
        chat.appendChild(wrap);
        scrollBottom();
        return wrap;
    }

    function addToolCard(tool, args) {
        var card = el('div', 'agent-tool-card');
        var head = el('div', 'agent-tool-head', '🔧 ' + escapeHtml(tool));
        head.title = '点击展开/收起参数';
        var detail = el('div', 'agent-tool-detail', '');
        detail.style.display = 'none';
        if (args && typeof args === 'object' && Object.keys(args).length) {
            head.onclick = function() {
                var show = detail.style.display === 'none';
                detail.style.display = show ? 'block' : 'none';
                if (show) detail.textContent = JSON.stringify(args, null, 2);
            };
        }
        var status = el('div', 'agent-tool-status', '运行中…');
        card.appendChild(head);
        card.appendChild(detail);
        card.appendChild(status);
        chat.appendChild(card);
        scrollBottom();
        return { card: card, status: status };
    }

    // 事件流工具卡：按 callId 建档，超上限裁剪最旧（防 DOM 无限膨胀）
    function addToolCardFor(name, args, callId) {
        var run = addToolCard(name, args);
        if (callId) toolCards[callId] = run;
        toolCardOrder.push(callId || ('#' + toolCardOrder.length));
        if (toolCardOrder.length > TOOL_CARD_LIMIT) {
            var old = toolCardOrder.shift();
            var oldRun = toolCards[old];
            if (oldRun && oldRun.card && oldRun.card.parentNode) oldRun.card.parentNode.removeChild(oldRun.card);
            delete toolCards[old];
        }
        return run;
    }

    // 工具卡收尾：状态文本 + ok/err 类名
    function finishToolCard(run, text) {
        if (!run || !run.status) return;
        run.status.textContent = text || '';
        run.status.className = (text && text.indexOf('✅') === 0)
            ? 'agent-tool-status ok' : 'agent-tool-status err';
    }
    // ─── 工具日志页签（右侧面板「💬 对话 / 🔧 工具日志」切换）───
    function switchAgentTab(key) {
        var chatPane = document.getElementById('agent-chat');
        document.querySelectorAll('.agent-tabs a').forEach(function(a) {
            a.classList.toggle('active', a.getAttribute('data-tab') === key);
        });
        if (chatPane) chatPane.style.display = (key === 'chat') ? '' : 'none';
        if (toolsLog) toolsLog.style.display = (key === 'chat') ? 'none' : '';
        // 输入框/发送只属于「💬 对话」页签；工具日志页签下不出现对话框
        var inputWrap = document.getElementById('agent-input-wrap');
        if (inputWrap) inputWrap.style.display = (key === 'chat') ? '' : 'none';
        if (key === 'tools') {
            loadToolLog();
            if (!toolPollTimer) toolPollTimer = setInterval(loadToolLog, 3000);
        } else {
            if (toolPollTimer) { clearInterval(toolPollTimer); toolPollTimer = null; }
        }
    }

    function escArg(args) {
        try { return JSON.stringify(args, null, 1).slice(0, 500); } catch (e) { return String(args); }
    }

    function loadToolLog() {
        if (!toolsLog) return;
        fetch('/api/agent/tool-log')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.ok) return;
                // 只展示外部 MCP 调用（source=mcp）；内嵌 agent 的 web 调用留在对话页签的工具卡片里
                var log = (d.log || []).filter(function(x) { return x.source === 'mcp'; });
                var success = log.filter(function(x) { return x.ok; }).length;
                // 超长自动清理：只渲染最近 DISPLAY_LIMIT 条，避免 DOM 无限膨胀拖垮侧栏
                var DISPLAY_LIMIT = 80;
                var show = log.slice(-DISPLAY_LIMIT);
                // 自动滚到底：渲染前记录是否在底部附近（跟随中）；新工具到达时跟随到底，
                // 不在底部（在翻旧记录）则按比例还原位置，不被 3s 轮询顶回顶部
                var h0 = toolsLog.scrollHeight;
                var ratio = h0 ? toolsLog.scrollTop / h0 : 0;
                var nearBottom = h0 - toolsLog.scrollTop - toolsLog.clientHeight < 80;
                var html = '<div style="position:sticky;top:0;z-index:1;background:#161b22;font-size:12px;color:#8b949e;padding:4px 0 8px;margin-bottom:4px">'
                    + '已暴露 <strong>' + d.tools_exposed + '</strong> 工具 · MCP 调用 <strong>' + log.length
                    + '</strong> 次 · 成功 <span style="color:#3fb950">' + success
                    + '</span> 失败 <span style="color:#f85149">' + (log.length - success) + '</span>'
                    + ' <button class="small" onclick="window.loadToolLog()">🔄 刷新</button>'
                    + ' <button class="small" onclick="window.clearToolLog()">🗑 清空</button>'
                    + (log.length > DISPLAY_LIMIT ? ' <span style="color:#8b949e">仅显示最近 ' + DISPLAY_LIMIT + ' 条（共 ' + log.length + '）</span>' : '')
                    + '</div>';
                if (!log.length) {
                    html += '<div style="font-size:12px;color:#8b949e;padding:8px 4px">暂无外部 MCP 调用记录（由 Claude Code 经 MCP 驱动时产生）。</div>';
                }
                show.forEach(function(x) {
                    html += '<div class="agent-tool-card ' + (x.ok ? 'ok' : 'err') + '">'
                        + '<div class="agent-tool-head">' + escapeHtml((x.time || '') + ' ' + (x.ok ? '✅' : '❌') + ' ' + x.tool)
                        + ' <span style="color:#8b949e;font-weight:normal">' + (x.duration_ms || 0) + 'ms</span></div>';
                    if (x.args && typeof x.args === 'object' && Object.keys(x.args).length) {
                        html += '<div class="agent-tool-detail" style="display:none">' + escArg(x.args) + '</div>';
                    }
                    html += '<div class="agent-tool-status">' + (x.ok ? '' : '❌ ') + escapeHtml(x.summary || '') + '</div></div>';
                });
                toolsLog.innerHTML = html;
                // 渲染后滚动：跟随中→贴底（新工具自动可见）；翻旧记录→按比例还原位置
                if (nearBottom) toolsLog.scrollTop = toolsLog.scrollHeight;
                else toolsLog.scrollTop = Math.round(ratio * toolsLog.scrollHeight);
                toolsLog.querySelectorAll('.agent-tool-card').forEach(function(card) {
                    var head = card.querySelector('.agent-tool-head');
                    var detail = card.querySelector('.agent-tool-detail');
                    if (head && detail) head.addEventListener('click', function() {
                        detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
                    });
                });
            })
            .catch(function() {});
    }

    function clearToolLog() {
        fetch('/api/agent/tool-log/clear', { method: 'POST' })
            .then(function() { loadToolLog(); })
            .catch(function() {});
    }

    // 供 base.html inline onclick 调用
    window.switchAgentTab = switchAgentTab;
    window.loadToolLog = loadToolLog;
    window.clearToolLog = clearToolLog;

    // ─── SSE 消费（fetch + getReader 手写解析，项目现有模式）───
    function consumeSSE(body) {
        return fetch('/api/agent/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        }).then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.body.getReader();
        }).then(function(reader) {
            var decoder = new TextDecoder();
            var buf = '';
            function pump() {
                return reader.read().then(function(res) {
                    if (res.done) return;
                    buf += decoder.decode(res.value, { stream: true });
                    var lines = buf.split('\n');
                    buf = lines.pop();
                    for (var i = 0; i < lines.length; i++) {
                        var line = lines[i].trim();
                        if (line.indexOf('data:') !== 0) continue;
                        var data = line.slice(5).trim();
                        if (!data) continue;
                        try { handleEvent(JSON.parse(data)); } catch (e) {}
                    }
                    return pump();
                });
            }
            return pump();
        });
    }

    function handleEvent(evt) {
        var t = evt.type;
        if (t === 'tool_call') {
            // dsh 核心实时推送：工具开始 → 建卡
            currentToolRun = addToolCardFor(evt.name, evt.args, evt.callId);
        } else if (t === 'tool_result') {
            // 按 callId 配对卡；配不到就忽略（绝不 fallback 到别的卡，避免污染）。
            // navigate/drive_ui 的 tool/call 也会建卡，故正常情况都配得到。
            var run = (evt.callId && toolCards[evt.callId]) ? toolCards[evt.callId] : null;
            if (evt.callId) delete toolCards[evt.callId];
            if (run) finishToolCard(run, (evt.ok ? '✅ ' : '❌ ') + (evt.summary || ''));
        } else if (t === 'navigate') {
            handleNavigate(evt.url);            // dsh 调用 navigate → 实时切页
        } else if (t === 'ui_command') {
            dispatchCommand({ cmd: evt.cmd, args: evt.args || {} });  // drive_ui → 驱动建书向导
        } else if (t === 'reply') {
            addMsg('assistant', evt.content);
            history.push({ role: 'assistant', content: evt.content });
            saveHistory(history);
        } else if (t === 'error') {
            addMsg('assistant', '⚠️ ' + (evt.message || '发生错误'));
            if (currentToolRun) finishToolCard(currentToolRun, '❌ 失败');
        } else if (t === 'done') {
            // C3：收尾所有未 resolve 的工具卡（result 缺失/滞后时兜底），清空映射
            Object.keys(toolCards).forEach(function(id) {
                finishToolCard(toolCards[id], '⚠️ 会话结束未收尾');
            });
            toolCards = {};
            toolCardOrder = [];
            if (currentToolRun) { finishToolCard(currentToolRun, '✅ 完成'); currentToolRun = null; }
            // C1：drain nav-intent 残留——会话期间 busy 守卫跳过轮询消费、而 navigate/drive_ui
            // 每次都无条件写队列，这些意图已由 SSE 事件流实时执行过；done 后轮询恢复会把
            // 残留重放（drive_ui(next) 走两步 / submit 二次建书）。取走清空，丢弃安全。
            fetch('/api/agent/nav-intents').catch(function(){});
            if (pendingTask) {
                var pt = pendingTask; pendingTask = null;
                startTask(pt);   // 接力排队任务（busy 保持 true，不清）
            } else {
                busy = false;
                setSendEnabled(true);
            }
        }
    }

    // ─── navigate 外部驱动桥（P1b）：轮询 MCP 写入的导航意图，取到即翻页/切页签 ───
    var navTimer = null;
    function pollNavIntents() {
        if (busy) return;   // dsh 会话进行中：导航/命令已由事件流实时推送，跳过轮询防双触发
        fetch('/api/agent/nav-intents')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.ok) return;
                (d.intents || []).forEach(function(it) {
                    if (it.kind === 'ui_command') { dispatchCommand(it); return; }
                    if (it.url) handleNavigate(it.url);
                    if (it.tab && (it.tab === 'chat' || it.tab === 'tools')) switchAgentTab(it.tab);
                });
            })
            .catch(function() {});
    }
    navTimer = setInterval(pollNavIntents, 2500);

    // ─── 导航（内置 agent 已删；navigate 走下方 ne:command / pollNavIntents 意图桥）───
    function handleNavigate(url) {
        if (typeof navigateTo === 'function') navigateTo(url);
        else window.location.href = url;
    }
    // 全局唯一 ne:command 监听，委托给各页面注册的单槽位 window.onnecommand（建书向导等）
    window.addEventListener('ne:command', function(e) {
        if (typeof window.onnecommand === 'function') window.onnecommand(e);
    });
    // 建书向导命令桥：等「向导 DOM 就绪」后 dispatch（≤10s，给 SPA navigate 加载留余量）。
    // 就绪信号用 #wz-idea 存在而非 __neWizardReady__ 标志——SPA navigate 不触发 unload，
    // 标志会在离开向导页后残留，导致在别的页误派发。
    function dispatchCommand(it) {
        var tries = 0;
        (function poll() {
            if (window.WZ && document.getElementById('wz-idea')) {
                window.dispatchEvent(new CustomEvent('ne:command', {
                    detail: { cmd: it.cmd, args: it.args || {} } }));
            } else if (++tries <= 100) {
                setTimeout(poll, 100);
            } else {
                if (typeof showToast === 'function')
                    showToast('建书向导未就绪（请确认已打开 /books/start 后重试）', 'error');
            }
        })();
    }

    // ─── 发送（全服务单任务：busy 时先打断旧的再排队）───
    function setSendEnabled(on) {
        sendBtn.disabled = !on;
        sendBtn.textContent = on ? '发送' : '…';
    }
    function startTask(text) {
        busy = true;
        setSendEnabled(false);
        consumeSSE({ messages: history }).catch(function(err) {
            addMsg('assistant', '⚠️ 请求失败：' + err.message);
            if (pendingTask) {
                var t = pendingTask; pendingTask = null;
                history.push({ role: 'user', content: t });
                saveHistory(history);
                startTask(t);
            } else {
                busy = false;
                setSendEnabled(true);
            }
        });
    }
    // 建书任务卡（「让 Agent 构建」按钮触发时替代用户气泡展示，任务文本仍进 history 供 SSE 取）
    function addBuildCard(text) {
        var card = el('div', 'agent-tool-card');
        card.appendChild(el('div', 'agent-tool-head', '🚀 建书任务'));
        var body = el('div', 'agent-tool-detail', text || '');
        body.style.display = 'block';
        card.appendChild(body);
        chat.appendChild(card);
        scrollBottom();
        return card;
    }
    function agentSendTask(text, opts) {
        var taskText = String(text || '').trim();
        if (!taskText) return;
        history.push({ role: 'user', content: taskText });
        saveHistory(history);
        input.value = '';
        if (opts && opts.card) {
            addBuildCard(taskText);   // 向导按钮 → 卡片，不渲染用户气泡
        } else {
            addMsg('user', taskText);
        }
        if (busy) {
            // 打断当前任务，排队新任务；当前流的 done 处理器接力 pendingTask
            pendingTask = taskText;
            fetch('/api/agent/chat/cancel', { method: 'POST' }).catch(function() {});
        } else {
            startTask(taskText);
        }
    }
    // 供向导「让 Agent 构建」按钮 / 侧栏统一调用
    window.agentSendTask = agentSendTask;

    function send() {
        var text = (input.value || '').trim();
        if (!text) return;
        agentSendTask(text);
    }

    sendBtn.addEventListener('click', send);
    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    clearBtn.addEventListener('click', function() {
        history = [];
        saveHistory(history);
        chat.innerHTML = '';
        addMsg('assistant', '对话已清空。有什么可以帮你？');
    });

    // 初始欢迎语 + 恢复会话历史
    if (!history.length) {
        addMsg('assistant', '👋 我是 NovelEngine 的 Agent，可以帮你完成从建书到上架的全部创作流程。\n试试：\n· 「创建一本都市爽文 by 枫落」\n· 「给 book_001 生成完整大纲」\n· 「续写 book_001，写下一个桥段」\n· 「打开书库看看」');
    } else {
        for (var i = 0; i < history.length; i++) {
            addMsg(history[i].role, history[i].content);
        }
    }
})();
