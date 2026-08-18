// 通用 HTML 转义工具：所有动态插入 innerHTML 的数据必须过一遍。
        // 放在 <head> 保证各子模板脚本执行前已可用。
        function escapeHtml(str) {
            if (str === null || str === undefined) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        // 右侧状态栏折叠：localStorage 持久化，折叠时露出右侧 ▶ 展开按钮。
        // 手动折叠/展开一次即锁定偏好（ne_status_locked），此后不再自动折叠。
        function toggleStatusBar() {
            var bar = document.getElementById('status-bar');
            var reopen = document.getElementById('status-reopen');
            if (!bar) return;
            var collapsed = bar.classList.toggle('collapsed');
            if (reopen) reopen.style.display = collapsed ? 'block' : 'none';
            try {
                localStorage.setItem('ne_status_collapsed', collapsed ? '1' : '0');
                localStorage.setItem('ne_status_locked', '1');
            } catch(e) {}
        }
        function setStatusCollapsed(collapsed) {
            var bar = document.getElementById('status-bar');
            var reopen = document.getElementById('status-reopen');
            if (!bar) return;
            bar.classList.toggle('collapsed', collapsed);
            if (reopen) reopen.style.display = collapsed ? 'block' : 'none';
        }
        function restoreStatusBar() {
            try {
                if (localStorage.getItem('ne_status_collapsed') === '1') {
                    setStatusCollapsed(true);
                }
            } catch(e) {}
        }
        // 空闲自动折叠：无运行任务且无日志时收起右栏（未手动锁定过偏好才生效），
        // 有任务出现时自动展开——右栏当前仅承载任务/日志，为未来 Harness 预留。
        function maybeAutoCollapse(tasks) {
            var bar = document.getElementById('status-bar');
            var el = document.getElementById('status-tasks');
            if (!bar || !el) return;   // 右侧已改为 Agent 聊天面板，不再自动折叠
            try {
                if (localStorage.getItem('ne_status_locked') === '1') return;
            } catch(e) {}
            var logList = document.getElementById('task-log-list');
            var hasLogs = !!(logList && logList.children.length > 0);
            var busy = !!(tasks && tasks.length > 0);
            var collapsed = bar.classList.contains('collapsed');
            if (busy && collapsed) {
                setStatusCollapsed(false);
            } else if (!busy && !hasLogs && !collapsed) {
                setStatusCollapsed(true);
            }
        }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', restoreStatusBar);
        } else {
            restoreStatusBar();
        }

// 全局 toast：showToast 即时弹出；flashToast 存 sessionStorage，配合 location.reload() 在下次加载后弹出。
// 各子模板 script 直接在 <body> 尾部执行，此时 #toast-root 已渲染，无需等 DOMContentLoaded。
        function showToast(msg, type) {
            var root = document.getElementById('toast-root');
            if (!root) { alert(msg); return; }
            var t = document.createElement('div');
            t.className = 'toast ' + (type || 'info');
            t.textContent = msg;
            root.appendChild(t);
            setTimeout(function() {
                t.classList.add('toast-out');
                setTimeout(function() { if (t.parentNode) t.parentNode.removeChild(t); }, 350);
            }, 3400);
        }
        function flashToast(msg, type) {
            try { sessionStorage.setItem('ne_toast', JSON.stringify({m: msg, t: type || 'success'})); } catch(e) {}
        }
        (function() {
            try {
                var f = sessionStorage.getItem('ne_toast');
                if (f) {
                    sessionStorage.removeItem('ne_toast');
                    var d = JSON.parse(f);
                    if (d && d.m) showToast(d.m, d.t);
                }
            } catch(e) {}
        })();

// Accordion toggle（事件委托：SPA 换入新内容后依然生效，也避免重复绑定）
        document.addEventListener('click', function(e) {
            var h = e.target.closest('.accordion-header');
            if (h && h.nextElementSibling) {
                h.nextElementSibling.classList.toggle('show');
            }
        });

        // ─── SPA 导航：拦截侧边栏链接，避免整页重刷 ───
        document.addEventListener('click', function(e) {
            var link = e.target.closest('nav a[href]');
            if (!link) return;
            // 跳过 javascript: 链接和外部链接
            var href = link.getAttribute('href');
            if (!href || href.startsWith('javascript:') || href.startsWith('http')) return;
            e.preventDefault();
            if (href === location.pathname + location.search) return;
            navigateTo(href);
        });
        // 处理浏览器后退/前进
        window.addEventListener('popstate', function() {
            location.reload();
        });

        // 运行状态轮询：所有任务卡片统一在 status-tasks 渲染，日志独立追加
        // 日志计数持久化到 sessionStorage，切换 SPA 页面不丢失
        var prevLogCount = JSON.parse(sessionStorage.getItem('novelengine_logcount') || '{}');
        window.addEventListener('beforeunload', function() {
            sessionStorage.setItem('novelengine_logcount', JSON.stringify(prevLogCount));
        });
        var STATUS_EMPTY = '<div class="status-empty">🤖 空闲 · 暂无 Agent 活动</div>';
        function clearLogs() {
            var list = document.getElementById('task-log-list');
            if (list) list.innerHTML = '';
            prevLogCount = {};
            sessionStorage.setItem('novelengine_logcount', JSON.stringify(prevLogCount));
        }
        
        function closeTask(taskId) {
            fetch('/api/status/tasks/close', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({id: taskId})
            }).then(function() {
                var card = document.querySelector('#status-tasks .agent-card[data-id="' + CSS.escape(taskId || '') + '"]');
                if (card) card.remove();
                if (!document.querySelector('#status-tasks .agent-card')) {
                    document.getElementById('status-tasks').innerHTML = STATUS_EMPTY;
                }
            });
        }

        // Agent 角色 → 徽标文案/配色映射（与后端 task_manager.agent 字段对应）
        var AGENT_META = {
            writing:       {label: '✍️ 写作',     cls: 'badge-writing'},
            outline:       {label: '📋 大纲',     cls: 'badge-outline'},
            world:         {label: '🌍 世界观',   cls: 'badge-world'},
            title:         {label: '🏷️ 书名',     cls: 'badge-title'},
            outline_agent: {label: '🤖 大纲助手', cls: 'badge-outline-agent'},
            scout:         {label: '🔍 侦察兵',   cls: 'badge-scout'},
            '':            {label: '🤖 Agent',   cls: 'badge-default'}
        };
        var AGENT_STATUS_TEXT = {
            running: 'running', done: 'done', failed: 'failed',
            cancelled: 'cancelled'
        };

        function renderAgentCards(taskArray) {
            if (!taskArray || taskArray.length === 0) {
                return STATUS_EMPTY;
            }
            var html = '';
            for (var i = 0; i < taskArray.length; i++) {
                var t = taskArray[i];
                var pct = t.total > 0 ? Math.round((t.current/t.total)*100) : 0;
                var safeId = escapeHtml(t.id || '');
                var safeUrl = escapeHtml(t.url || '');
                var meta = AGENT_META[t.agent] || AGENT_META[''];
                var statusCls = AGENT_STATUS_TEXT[t.status] || 'running';
                // 步骤（step）+ 阶段（phase_display）拼接为"当前动作"
                var stepTxt = t.step || t.phase || '';
                var subTxt = t.sub_step && t.sub_step !== stepTxt ? t.sub_step : '';
                html += '<div class="agent-card" data-id="' + safeId + '">';
                // 头：角色徽标 + 状态点 + 关闭
                html += '<div class="agent-head">';
                html += '<span class="agent-badge ' + meta.cls + '">' + meta.label + '</span>';
                html += '<span class="agent-status ' + statusCls + '" title="' + escapeHtml(t.status || '') + '"></span>';
                html += '<button onclick="closeTask(this.dataset.id)" data-id="' + safeId + '" class="agent-close" title="关闭">✕</button>';
                html += '</div>';
                // 书上下文
                if (t.book_title || t.book_id) {
                    html += '<div class="agent-book">' + escapeHtml(t.book_title || '')
                        + (t.book_id ? ' <i>' + escapeHtml(t.book_id) + '</i>' : '') + '</div>';
                } else if (t.name) {
                    html += '<div class="agent-book">' + escapeHtml(t.name) + '</div>';
                }
                // 当前步骤 / 子步骤
                if (stepTxt) html += '<div class="agent-step">' + escapeHtml(stepTxt) + '</div>';
                if (subTxt) html += '<div class="agent-substep">' + escapeHtml(subTxt) + '</div>';
                // 元信息：LLM 次数 + 进度文本
                html += '<div class="agent-meta">';
                if (t.llm_calls > 0) html += '<span class="agent-llm">📡 LLM ×' + escapeHtml(t.llm_calls) + '</span>';
                if (t.total > 0) html += '<span class="agent-progress-text">' + escapeHtml(t.current) + '/' + escapeHtml(t.total) + '</span>';
                if (t.time) html += '<span class="agent-time">' + escapeHtml(t.time) + '</span>';
                html += '</div>';
                if (t.total > 0) html += '<div class="task-progress"><div class="task-progress-fill" style="width:' + pct + '%"></div></div>';
                if (t.url) html += '<div class="agent-actions"><button onclick="navigateTo(this.dataset.url)" data-url="' + safeUrl + '" title="去查看">查看</button></div>';
                html += '</div>';
            }
            return html;
        }
        
        // SPA 导航（供侧边栏“查看”按钮复用）
        function navigateTo(target) {
            if (!target || target === location.pathname) return;
            fetch(target)
                .then(function(r) { return r.text(); })
                .then(function(html) {
                    var parser = new DOMParser();
                    var doc = parser.parseFromString(html, 'text/html');
                    var newContent = doc.querySelector('main');
                    var oldContent = document.querySelector('main');
                    if (newContent && oldContent) {
                        oldContent.innerHTML = newContent.innerHTML;
                        document.title = doc.title;
                        // 执行新内容中的脚本标签
                        oldContent.querySelectorAll('script').forEach(function(s) {
                            var newScript = document.createElement('script');
                            if (s.src) {
                                newScript.src = s.src;
                            } else {
                                newScript.textContent = s.textContent;
                            }
                            document.body.appendChild(newScript);
                            document.body.removeChild(newScript);
                        });
                        // 更新侧边栏 active 状态：以服务端渲染的 nav 为准
                        // （服务端按 request.path 精确判断，避免 /books/start 误高亮 /books）
                        var newNav = doc.querySelector('nav');
                        var oldNav = document.querySelector('nav');
                        if (newNav && oldNav) {
                            var navLinks = oldNav.querySelectorAll('a[href]');
                            for (var i = 0; i < navLinks.length; i++) {
                                var href = navLinks[i].getAttribute('href');
                                var match = newNav.querySelector('a[href="' + href + '"]');
                                navLinks[i].classList.toggle('active',
                                    !!(match && match.classList.contains('active')));
                            }
                        }
                    }
                    history.pushState(null, '', target);
                })
                .catch(function() { location.href = target; });
        }
        
        function pollStatus() {
            var el = document.getElementById('status-tasks');
            if (!el) return;   // 右侧已改为 Agent 聊天面板，无任务卡片区
            fetch('/api/status/tasks')
                .then(function(r) { return r.json(); })
                .then(function(tasks) {
                    var logArea = document.getElementById('task-log');
                    var logList = document.getElementById('task-log-list');
                    
                    // 渲染 Agent 卡片（完整替换，保持最新的进度数据）
                    var newHtml = renderAgentCards(tasks);
                    if (el.innerHTML !== newHtml) {
                        el.innerHTML = newHtml;
                    }
                    
                    // 日志只追加不清除（任务卡片和日志各自独立，符合 task-system-spec）
                    // 新任务替代旧任务时，旧日志保留供回溯
                    
                    // 日志增量追加（独立于卡片，不受卡片替换影响）
                    if (tasks && tasks.length > 0 && logArea && logList) {
                        var anyNew = false;
                        for (var ti = 0; ti < tasks.length; ti++) {
                            var t = tasks[ti];
                            if (t.logs && t.logs.length > 0) {
                                // 用 id+启动时间戳做 key：同名任务重启后日志计数不混淆
                                var logKey = t.id + ':' + (t.started_at_ts || 0);
                                var prev = prevLogCount[logKey] || 0;
                                if (t.logs.length > prev) {
                                    for (var i = prev; i < t.logs.length; i++) {
                                        var l = t.logs[i];
                                        var div = document.createElement('div');
                                        div.setAttribute('data-task-id', t.id);
                                        div.style.cssText = 'padding:2px 0;border-left:2px solid ' + (l.level==='error'?'#f85149':l.level==='success'?'#3fb950':'#30363d') + ';padding-left:6px;margin:1px 0;font-size:11px';
                                        div.textContent = l.time + ' ' + l.message;
                                        logList.appendChild(div);
                                    }
                                    prevLogCount[logKey] = t.logs.length;
                                    // 每次追加后同步到 sessionStorage
                                    sessionStorage.setItem('novelengine_logcount', JSON.stringify(prevLogCount));
                                    anyNew = true;
                                }
                            }
                        }
                        if (anyNew) {
                            logArea.style.display = 'block';
                            // 容量上限：最多保留 300 条，超出删除最旧的
                            while (logList.children.length > 300) {
                                logList.removeChild(logList.firstChild);
                            }
                            logList.scrollTop = logList.scrollHeight;
                        }
                    }
                    maybeAutoCollapse(tasks);
                }).catch(function() {});
        }
        pollStatus();
        if (document.getElementById('status-tasks')) {
            setInterval(pollStatus, 2000);
        }

/* ─── 设定卡共享解析（world_card.html 与 storyline_write_flow.html 共用）─── */
function parseFactionLines(txt) {
    /* "名:立场" 一行/逗号 → [{name, stance}] 或 [str] */
    var out = [];
    String(txt || '').split(/[\n,，、;；]+/).forEach(function(x) {
        x = String(x).trim(); if (!x) return;
        var i = x.indexOf(':');
        if (i > 0) out.push({name: x.slice(0, i).trim(), stance: x.slice(i + 1).trim()});
        else out.push(x);
    });
    return out;
}
function parseRuleLines(txt) {
    /* 每行一条规则 → [str] */
    return String(txt || '').split('\n').map(function(x){ return String(x).trim(); }).filter(Boolean);
}
function factionLinesText(factions) {
    /* [{name,stance}] 或 [str] → 文本（"名:立场" 一行） */
    return (factions || []).map(function(f){
        return (f && typeof f === 'object') ? (f.name + (f.stance ? ':' + f.stance : '')) : f;
    }).join('\n');
}
