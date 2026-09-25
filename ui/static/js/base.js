// 侧栏响应式断点体系（带滞回防抖）
// 缩窄视口时：先自动折叠左侧导航栏（<=1200px），再自动折叠右侧 Agent 面板（<=880px）
// 放大视口时：在滞回阈值处（右侧 >=920px，左侧 >=1240px）分别恢复展开
var SIDEBAR_BREAKPOINTS = {
    nav: {
        collapseAt: 1200,
        expandAt: 1240
    },
    status: {
        collapseAt: 880,
        expandAt: 920
    }
};

// 首屏防闪烁（FOUC）：在 <head> 加载期间立即设置根节点状态，避免渲染时侧栏跳动
(function() {
    try {
        var w = window.innerWidth;
        // 左栏：手动锁定优先，否则按自动断点（<= 1200px 自动折叠）
        var navLocked = localStorage.getItem('ne_nav_locked') === '1' || localStorage.getItem('ne_nav_collapsed') !== null;
        var navCol = localStorage.getItem('ne_nav_collapsed');
        if (navLocked) {
            if (navCol === '1') document.documentElement.classList.add('nav-collapsed');
        } else if (w <= 1200) {
            document.documentElement.classList.add('nav-collapsed');
        }

        // 右栏：手动锁定优先，否则按自动断点（<= 880px 自动折叠）
        var statusLocked = localStorage.getItem('ne_status_locked') === '1';
        var statusCol = localStorage.getItem('ne_status_collapsed');
        if (statusLocked) {
            if (statusCol === '1') document.documentElement.classList.add('status-collapsed');
        } else if (w <= 880) {
            document.documentElement.classList.add('status-collapsed');
        }
    } catch(e) {}
})();

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

// 判断侧栏是否已被用户手动锁定偏好
function isNavLocked() {
    try {
        return localStorage.getItem('ne_nav_locked') === '1' || localStorage.getItem('ne_nav_collapsed') !== null;
    } catch(e) { return false; }
}
function isStatusLocked() {
    try {
        return localStorage.getItem('ne_status_locked') === '1';
    } catch(e) { return false; }
}

// 左侧导航栏折叠：localStorage 持久化 (ne_nav_collapsed)，折叠时在左边缘显示 ▶ 展开按钮。
// 手动点击把手即锁定用户偏好 (ne_nav_locked)，后续窗口 resize 不会强行覆盖用户意图。
function toggleNav() {
    var nav = document.getElementById('app-nav') || document.querySelector('nav');
    if (!nav) return;
    var isCollapsed = !nav.classList.contains('collapsed');
    setNavCollapsed(isCollapsed);
    try {
        localStorage.setItem('ne_nav_collapsed', isCollapsed ? '1' : '0');
        localStorage.setItem('ne_nav_locked', '1');
    } catch(e) {}
}
function setNavCollapsed(collapsed) {
    var nav = document.getElementById('app-nav') || document.querySelector('nav');
    var toggle = document.getElementById('nav-toggle');
    if (!nav) return false;
    var changed = nav.classList.contains('collapsed') !== collapsed;
    nav.classList.toggle('collapsed', collapsed);
    document.documentElement.classList.toggle('nav-collapsed', collapsed);
    if (toggle) {
        toggle.setAttribute('title', collapsed ? '展开导航' : '折叠导航');
        toggle.setAttribute('aria-label', collapsed ? '展开导航' : '折叠导航');
        toggle.setAttribute('aria-expanded', String(!collapsed));
    }
    return changed;
}
function restoreNav() {
    try {
        if (isNavLocked()) {
            setNavCollapsed(localStorage.getItem('ne_nav_collapsed') === '1');
        } else {
            // 未设置手动偏好：窄屏（<= 1200px）自动折叠先让出空间，宽屏展开
            setNavCollapsed(window.innerWidth <= SIDEBAR_BREAKPOINTS.nav.collapseAt);
        }
    } catch(e) {}
}

// 右侧状态栏折叠：localStorage 持久化，折叠时露出右侧 ◀ 展开按钮。
// 手动折叠/展开一次即锁定偏好（ne_status_locked），此后不再自动折叠。
function toggleStatusBar() {
    var bar = document.getElementById('status-bar');
    if (!bar) return;
    var isCollapsed = !bar.classList.contains('collapsed');
    setStatusCollapsed(isCollapsed);
    try {
        localStorage.setItem('ne_status_collapsed', isCollapsed ? '1' : '0');
        localStorage.setItem('ne_status_locked', '1');
    } catch(e) {}
}
function setStatusCollapsed(collapsed) {
    var bar = document.getElementById('status-bar');
    var toggle = document.getElementById('status-toggle');
    if (!bar) return false;
    var changed = bar.classList.contains('collapsed') !== collapsed;
    bar.classList.toggle('collapsed', collapsed);
    document.documentElement.classList.toggle('status-collapsed', collapsed);
    if (toggle) {
        toggle.setAttribute('title', collapsed ? '展开面板' : '折叠面板');
        toggle.setAttribute('aria-label', collapsed ? '展开面板' : '折叠面板');
        toggle.setAttribute('aria-expanded', String(!collapsed));
    }
    return changed;
}
function restoreStatusBar() {
    try {
        if (isStatusLocked()) {
            setStatusCollapsed(localStorage.getItem('ne_status_collapsed') === '1');
        } else {
            // 未锁定：窄窗口（<= 880px）默认折叠，把空间让给内容区；宽窗口默认展开
            setStatusCollapsed(window.innerWidth <= SIDEBAR_BREAKPOINTS.status.collapseAt);
        }
    } catch(e) {}
}

// 供需要时清除侧栏锁定偏好并恢复自动匹配
function resetSidebarPreferences() {
    try {
        localStorage.removeItem('ne_nav_collapsed');
        localStorage.removeItem('ne_nav_locked');
        localStorage.removeItem('ne_status_collapsed');
        localStorage.removeItem('ne_status_locked');
    } catch(e) {}
    restoreNav();
    restoreStatusBar();
}
window.resetSidebarPreferences = resetSidebarPreferences;

// 响应式宽度自适应控制器：未锁定时随窗口宽度实时折叠/展开（用户手动锁过则尊重其偏好）
var _neResizeInit = false;
var _neResizeTimer = null;
var _neRafId = null;

function _neInitSidebarResize() {
    if (_neResizeInit) return;
    _neResizeInit = true;

    window.addEventListener('resize', function(e) {
        // 忽略内部合成的 resize 事件
        if (e && e._neSynthetic) return;

        // 窗口连续拖拽调整尺寸期间临时禁用过渡动画，避免延迟追赶与抖动
        document.documentElement.classList.add('layout-resizing');
        clearTimeout(_neResizeTimer);
        _neResizeTimer = setTimeout(function() {
            document.documentElement.classList.remove('layout-resizing');
        }, 150);

        if (_neRafId) cancelAnimationFrame(_neRafId);
        _neRafId = requestAnimationFrame(function() {
            var w = window.innerWidth;
            var nav = document.getElementById('app-nav') || document.querySelector('nav');
            var bar = document.getElementById('status-bar');
            var layoutChanged = false;

            // 1. 左栏：未锁定时按宽度响应（<= 1200 折叠，>= 1240 展开）
            if (nav && !isNavLocked()) {
                var isNavCol = nav.classList.contains('collapsed');
                if (!isNavCol && w <= SIDEBAR_BREAKPOINTS.nav.collapseAt) {
                    if (setNavCollapsed(true)) layoutChanged = true;
                } else if (isNavCol && w >= SIDEBAR_BREAKPOINTS.nav.expandAt) {
                    if (setNavCollapsed(false)) layoutChanged = true;
                }
            }

            // 2. 右栏：未锁定时按宽度响应（<= 880 折叠，>= 920 展开）
            if (bar && !isStatusLocked()) {
                var isStatusCol = bar.classList.contains('collapsed');
                if (!isStatusCol && w <= SIDEBAR_BREAKPOINTS.status.collapseAt) {
                    if (setStatusCollapsed(true)) layoutChanged = true;
                } else if (isStatusCol && w >= SIDEBAR_BREAKPOINTS.status.expandAt) {
                    if (setStatusCollapsed(false)) layoutChanged = true;
                }
            }

            // 若自动折叠状态发生改变，延迟派发合成 resize 事件通知图表/阅读器重绘
            if (layoutChanged) {
                setTimeout(function() {
                    var evt = new Event('resize');
                    evt._neSynthetic = true;
                    window.dispatchEvent(evt);
                }, 220);
            }
        });
    });
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function(){
        restoreNav();
        restoreStatusBar();
        _neInitSidebarResize();
    });
} else {
    restoreNav();
    restoreStatusBar();
    _neInitSidebarResize();
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
            }, 5000);
        }
        function flashToast(msg, type) {
            try { sessionStorage.setItem('ne_toast', JSON.stringify({m: msg, t: type || 'success'})); } catch(e) {}
        }

        // 一键重启本地服务：先让后端返回当前 boot_id，再等待新进程的 boot_id，
        // 避免旧服务仍能响应时过早刷新页面。
        var _neRestartTimer = null;
        function restartPlatform() {
            var button = document.getElementById('platform-restart');
            var status = document.getElementById('platform-restart-status');
            if (!button || button.disabled) return;
            button.disabled = true;
            button.classList.remove('restart-error');
            button.textContent = '↻ 重启中…';
            if (status) status.textContent = '正在停止 Agent 与服务…';
            var startedAt = Date.now();
            var oldBootId = '';
            var timeoutMs = 90000;

            function finishError(message) {
                if (_neRestartTimer) { clearTimeout(_neRestartTimer); _neRestartTimer = null; }
                button.disabled = false;
                button.classList.add('restart-error');
                button.textContent = '↻ 重试重启';
                if (status) status.textContent = '自动重启未响应，可查看 storage/restart_service.log';
                showToast(message, 'error');
            }
            function pollHealth() {
                if (Date.now() - startedAt > timeoutMs) {
                    finishError('服务重启超时，请检查控制台或手动运行 launch.bat。');
                    return;
                }
                var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
                var timeoutId = controller ? setTimeout(function() { controller.abort(); }, 3000) : null;
                fetch('/api/system/health', {
                    cache: 'no-store',
                    signal: controller ? controller.signal : undefined
                })
                    .then(function(r) { if (!r.ok) throw new Error('health ' + r.status); return r.json(); })
                    .then(function(data) {
                        if (timeoutId) clearTimeout(timeoutId);
                        if (data && data.boot_id && oldBootId && data.boot_id !== oldBootId) {
                            window.location.reload();
                            return;
                        }
                        _neRestartTimer = setTimeout(pollHealth, 700);
                    })
                    .catch(function() {
                        if (timeoutId) clearTimeout(timeoutId);
                        // 服务切换期间连接失败是预期现象，继续等待新实例。
                        _neRestartTimer = setTimeout(pollHealth, 700);
                    });
            }

            fetch('/api/system/restart', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-NovelEngine-Action': 'restart'
                },
                body: '{}',
                cache: 'no-store'
            })
                .then(function(r) {
                    return r.json().catch(function() { return {}; }).then(function(data) {
                        if (!r.ok || !data.ok) {
                            var error = new Error(data.message || data.error || 'restart failed');
                            error.code = data.error || '';
                            throw error;
                        }
                        return data;
                    });
                })
                .then(function(data) {
                    oldBootId = data.boot_id || '';
                    if (!oldBootId) throw new Error('restart identity missing');
                    if (status) status.textContent = '等待新服务上线…';
                    pollHealth();
                })
                .catch(function(err) {
                    finishError(err && err.code === 'debug_mode_restart_unsupported'
                        ? '调试模式下不能自动重启，请先关闭 NOVEL_DEBUG。'
                        : '无法启动服务重启，请检查后端状态。');
                });
        }
        window.restartPlatform = restartPlatform;
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

// Accordion toggle（事件委托：SPA 换入新内容后依然生效，也避免重复绑定）。
// accordionSet 挂全局供页面编程展开。同步三件事：
//   ① body 的 .show；② [data-acc-hint] 的「展开/收起」文案；③ **箭头本身**。
// ③ 是此前漏的：`.sl-chev` 是静态文本 ▸，折叠区展开后箭头不变，所有手风琴都受影响。
        function accordionSet(header, open) {
            var body = header && header.nextElementSibling;
            if (!body) return;
            var show = (open === undefined) ? !body.classList.contains('show') : !!open;
            body.classList.toggle('show', show);
            header.setAttribute('aria-expanded', show ? 'true' : 'false');
            var hint = header.querySelector('[data-acc-hint]');
            if (hint) hint.textContent = show ? '收起 ▾' : '展开 ▸';
            var chev = header.querySelector('.sl-chev');
            if (chev) chev.textContent = show ? '▾' : '▸';
        }
        document.addEventListener('click', function(e) {
            var h = e.target.closest('.accordion-header');
            if (h) accordionSet(h);
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

        // SPA 导航（侧边栏点击拦截 + 各页"查看"按钮复用）
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
