// Agent 聊天助手面板（OpenClaw 式）：侧栏对话，Agent 通过 function calling 操作引擎并导航页面。
// 后端 /api/agent/chat（SSE）。对话历史仅存 user/assistant 文本，工具步骤卡临时展示不入历史。
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
    var currentToolRun = null;                  // 当前工具卡引用
    var toolPollTimer = null;                   // 工具日志轮询定时器

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
    function setToolResult(card, ok, summary) {
        card.status.textContent = (ok ? '✅ ' : '❌ ') + (summary || '');
        card.card.classList.add(ok ? 'ok' : 'err');
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
                var html = '<div style="font-size:12px;color:#8b949e;margin-bottom:8px">'
                    + '已暴露 <strong>' + d.tools_exposed + '</strong> 工具 · MCP 调用 <strong>' + log.length
                    + '</strong> 次 · 成功 <span style="color:#3fb950">' + success
                    + '</span> 失败 <span style="color:#f85149">' + (log.length - success) + '</span>'
                    + ' <button class="small" onclick="window.loadToolLog()">🔄 刷新</button>'
                    + ' <button class="small" onclick="window.clearToolLog()">🗑 清空</button>'
                    + '</div>';
                if (!log.length) {
                    html += '<div style="font-size:12px;color:#8b949e;padding:8px 4px">暂无外部 MCP 调用记录（由 Claude Code 经 MCP 驱动时产生）。</div>';
                }
                log.forEach(function(x) {
                    html += '<div class="agent-tool-card ' + (x.ok ? 'ok' : 'err') + '">'
                        + '<div class="agent-tool-head">' + escapeHtml((x.time || '') + ' ' + (x.ok ? '✅' : '❌') + ' ' + x.tool)
                        + ' <span style="color:#8b949e;font-weight:normal">' + (x.duration_ms || 0) + 'ms</span></div>';
                    if (x.args && typeof x.args === 'object' && Object.keys(x.args).length) {
                        html += '<div class="agent-tool-detail" style="display:none">' + escArg(x.args) + '</div>';
                    }
                    html += '<div class="agent-tool-status">' + (x.ok ? '' : '❌ ') + escapeHtml(x.summary || '') + '</div></div>';
                });
                toolsLog.innerHTML = html;
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
        if (t === 'tool_start') {
            currentToolRun = addToolCard(evt.tool, evt.args);
        } else if (t === 'tool_result') {
            if (currentToolRun) { setToolResult(currentToolRun, evt.ok, evt.summary); currentToolRun = null; }
        } else if (t === 'reply') {
            addMsg('assistant', evt.content);
            history.push({ role: 'assistant', content: evt.content });
            saveHistory(history);
        } else if (t === 'navigate') {
            handleNavigate(evt.url);
        } else if (t === 'canvas') {
            dispatchCanvas(evt);
        } else if (t === 'error') {
            addMsg('assistant', '⚠️ ' + (evt.message || '发生错误'));
        } else if (t === 'done') {
            busy = false;
            setSendEnabled(true);
        }
    }

    // ─── navigate 外部驱动桥（P1b）：轮询 MCP 写入的导航意图，取到即翻页/切页签 ───
    var navTimer = null;
    function pollNavIntents() {
        fetch('/api/agent/nav-intents')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.ok) return;
                (d.intents || []).forEach(function(it) {
                    if (it.url) handleNavigate(it.url);
                    if (it.tab && (it.tab === 'chat' || it.tab === 'tools')) switchAgentTab(it.tab);
                });
            })
            .catch(function() {});
    }
    navTimer = setInterval(pollNavIntents, 2500);

    // ─── 导航 / 画布（Phase 7 机制）───
    // 重置画布就绪标志后再切页；写作台脚本会重新置 true
    function handleNavigate(url) {
        window.__neCanvasReady__ = false;
        if (typeof navigateTo === 'function') navigateTo(url);
        else window.location.href = url;
    }
    // 全局唯一 ne:canvas 监听，委托给写作台页注册的单槽位 window.onnecanvas
    window.addEventListener('ne:canvas', function(e) {
        if (typeof window.onnecanvas === 'function') window.onnecanvas(e);
    });
    // 轮询写作台画布就绪后 dispatch（≤5s）
    function dispatchCanvas(payload) {
        var tries = 0;
        (function poll() {
            if (window.__neCanvasReady__) {
                window.dispatchEvent(new CustomEvent('ne:canvas', { detail: payload }));
            } else if (++tries <= 50) {
                setTimeout(poll, 100);
            } else {
                if (typeof showToast === 'function') showToast('写作台画布未就绪，无法操作', 'error');
            }
        })();
    }

    // ─── 发送 ───
    function setSendEnabled(on) {
        sendBtn.disabled = !on;
        sendBtn.textContent = on ? '发送' : '…';
    }
    function send() {
        if (busy) return;
        var text = (input.value || '').trim();
        if (!text) return;
        history.push({ role: 'user', content: text });
        saveHistory(history);
        input.value = '';
        addMsg('user', text);
        busy = true;
        setSendEnabled(false);
        consumeSSE({ messages: history }).catch(function(err) {
            addMsg('assistant', '⚠️ 请求失败：' + err.message);
            busy = false;
            setSendEnabled(true);
        });
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
