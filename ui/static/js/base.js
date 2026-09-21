// 首屏防闪烁（FOUC）：在 <head> 加载期间立即设置根节点状态，避免渲染时侧栏跳动
(function() {
    try {
        var navCol = localStorage.getItem('ne_nav_collapsed');
        if (navCol === '1') {
            document.documentElement.classList.add('nav-collapsed');
        } else if (navCol === null && window.innerWidth < 760) {
            document.documentElement.classList.add('nav-collapsed');
        }
        var statusLocked = localStorage.getItem('ne_status_locked');
        var statusCol = localStorage.getItem('ne_status_collapsed');
        if (statusLocked === '1' && statusCol === '1') {
            document.documentElement.classList.add('status-collapsed');
        } else if (statusLocked !== '1' && window.innerWidth < 1280) {
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

// 左侧导航栏折叠：localStorage 持久化 (ne_nav_collapsed)，折叠时在左边缘显示 ▶ 展开按钮。
function toggleNav() {
    var nav = document.getElementById('app-nav') || document.querySelector('nav');
    if (!nav) return;
    var isCollapsed = !nav.classList.contains('collapsed');
    setNavCollapsed(isCollapsed);
    try {
        localStorage.setItem('ne_nav_collapsed', isCollapsed ? '1' : '0');
    } catch(e) {}
    setTimeout(function() {
        window.dispatchEvent(new Event('resize'));
    }, 220);
}
function setNavCollapsed(collapsed) {
    var nav = document.getElementById('app-nav') || document.querySelector('nav');
    var reopen = document.getElementById('nav-reopen');
    var toggle = document.getElementById('nav-toggle');
    if (!nav) return;
    nav.classList.toggle('collapsed', collapsed);
    document.documentElement.classList.toggle('nav-collapsed', collapsed);
    if (reopen) reopen.style.display = collapsed ? 'block' : 'none';
    if (toggle) toggle.style.display = collapsed ? 'none' : 'block';
}
function restoreNav() {
    try {
        var collapsed = localStorage.getItem('ne_nav_collapsed');
        if (collapsed !== null) {
            setNavCollapsed(collapsed === '1');
        } else {
            // 未设置偏好：窄屏（<760px）默认折叠，宽屏默认展开
            setNavCollapsed(window.innerWidth < 760);
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
    setTimeout(function() {
        window.dispatchEvent(new Event('resize'));
    }, 220);
}
function setStatusCollapsed(collapsed) {
    var bar = document.getElementById('status-bar');
    var reopen = document.getElementById('status-reopen');
    var toggle = document.getElementById('status-toggle');
    if (!bar) return;
    bar.classList.toggle('collapsed', collapsed);
    document.documentElement.classList.toggle('status-collapsed', collapsed);
    if (reopen) reopen.style.display = collapsed ? 'block' : 'none';
    if (toggle) toggle.style.display = collapsed ? 'none' : 'block';
}
function restoreStatusBar() {
    try {
        var locked = localStorage.getItem('ne_status_locked');
        var collapsed = localStorage.getItem('ne_status_collapsed');
        if (locked === '1') {
            setStatusCollapsed(collapsed === '1');
        } else {
            // 未锁定：窄窗口（<1280px）默认折叠，把空间让给内容区；宽窗口默认展开
            setStatusCollapsed(window.innerWidth < 1280);
        }
    } catch(e) {}
}
// 未锁定时随窗口宽度实时折叠/展开（用户手动锁过则尊重其偏好）
var _neResizeInit = false;
function _neInitSidebarResize() {
    if (_neResizeInit) return;
    _neResizeInit = true;
    window.addEventListener('resize', function() {
        try {
            // 左侧若未显式保存偏好，窄屏自动折叠
            if (localStorage.getItem('ne_nav_collapsed') === null) {
                setNavCollapsed(window.innerWidth < 760);
            }
            if (localStorage.getItem('ne_status_locked') === '1') return;
            var bar = document.getElementById('status-bar');
            if (!bar) return;
            setStatusCollapsed(window.innerWidth < 1280);
        } catch(e) {}
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
