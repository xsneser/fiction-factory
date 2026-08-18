// Agent 聊天助手面板（OpenClaw 式）：侧栏对话，Agent 通过 function calling 操作引擎并导航页面。
// 后端 /api/agent/chat（SSE）。对话历史仅存 user/assistant 文本，工具步骤卡临时展示不入历史。
(function() {
    var chat = document.getElementById('agent-chat');
    var input = document.getElementById('agent-input');
    var sendBtn = document.getElementById('agent-send');
    var clearBtn = document.getElementById('agent-clear');
    if (!chat || !input || !sendBtn) return;   // 布局缺失则静默跳过

    var HISTORY_KEY = 'ne_agent_history';
    var HISTORY_LIMIT = 40;
    var busy = false;
    var currentToolRun = null;                  // 当前工具卡引用

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
