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
        var STATUS_EMPTY = '<div class="status-empty">暂无运行中的任务</div>';

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
                var card = document.querySelector('#status-tasks .status-card[data-id="' + CSS.escape(taskId || '') + '"]');
                if (card) card.remove();
                if (!document.querySelector('#status-tasks .status-card')) {
                    document.getElementById('status-tasks').innerHTML = STATUS_EMPTY;
                }
            });
        }

        function renderTaskCards(taskArray) {
            if (!taskArray || taskArray.length === 0) {
                return STATUS_EMPTY;
            }
            var html = '';
            for (var i = 0; i < taskArray.length; i++) {
                var t = taskArray[i];
                var pct = t.total > 0 ? Math.round((t.current/t.total)*100) : 0;
                var safeId = escapeHtml(t.id || '');
                var safeUrl = escapeHtml(t.url || '');
                html += '<div class="status-card" data-id="' + safeId + '">';
                // 第一行：工具名（在干什么操作）
                html += '<div class="task-name">' + escapeHtml(t.name || '任务') + '</div>';
                // 第二行：对象 + 具体阶段（对谁、干到哪）
                var detail = (t.title ? escapeHtml(t.title) + ' · ' : '') + escapeHtml(t.phase || '');
                html += '<div class="task-phase">' + detail + '</div>';
                if (t.total > 0) html += '<div class="task-progress"><div class="task-progress-fill" style="width:' + pct + '%"></div></div>';
                if (t.time) html += '<div class="task-time">' + escapeHtml(t.time) + '</div>';
                html += '<div class="task-actions" style="margin-top:6px;display:flex;gap:6px;align-items:center">';
                // 查看按钮：低调灰色小按钮，hover 提亮
                if (t.url) html += '<button onclick="navigateTo(this.dataset.url)" data-url="' + safeUrl + '" style="flex:1;background:transparent;border:1px solid #30363d;color:#8b949e;border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer;transition:all .2s" onmouseover="this.style.color=\'#f0f6fc\';this.style.borderColor=\'#58a6ff\'" onmouseout="this.style.color=\'#8b949e\';this.style.borderColor=\'#30363d\'" title="去查看">查看</button>';
                // 所有任务（运行中/完成/失败）都可以关闭：大一点更显眼，hover 变红
                html += '<button onclick="closeTask(this.dataset.id)" data-id="' + safeId + '" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:16px;line-height:1;padding:0 2px;transition:color .2s" onmouseover="this.style.color=\'#f85149\'" onmouseout="this.style.color=\'#8b949e\'" title="关闭">✕</button>';
                html += '</div>';
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
            fetch('/api/status/tasks')
                .then(function(r) { return r.json(); })
                .then(function(tasks) {
                    var el = document.getElementById('status-tasks');
                    var logArea = document.getElementById('task-log');
                    var logList = document.getElementById('task-log-list');
                    
                    // 渲染任务卡片（完整替换，保持最新的进度数据）
                    var newHtml = renderTaskCards(tasks);
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
                }).catch(function() {});
        }
        pollStatus();
        setInterval(pollStatus, 2000);
