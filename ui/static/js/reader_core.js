// ═══════════════════════════════════════════════════
// 阅读器共享分页核心（外部书库阅读器 / 写作台阅读器共用）
// 用法：var reader = ReaderCore.create({...opts...})
// 职责：CSS columns 分页 + 章节内/跨章翻页 + 目录抽屉 + 位置持久化 + 键盘/滚轮/ResizeObserver
// 差异经回调注入：getChapterContent（正文来源）/ renderChapter（渲染方式）/ onChapterChange（切章副作用）
// 双容器推入动画：opts.pagesNext 提供元素即启用（写作台），否则用简单滑出+硬切（外部）
// 对外 controller：{ goToChapter, nextChapter, prevChapter, nextPage, prevPage,
//                     showPage, pageOfElement, refresh, getState, idx, chapters }
// ═══════════════════════════════════════════════════
window.ReaderCore = (function () {
    function _ms_esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    }
    // 章节标题库里存**裸标题**，前缀由展示端加（`'第'+num+'章 '+title`）。存量数据里
    // 有「第1章 天闪裂空」这种自带前缀的，直接用会渲染成「第1章 第1章 天闪裂空」——
    // 与后端 libraries/book_manager.normalize_chapter_title 同一口径。
    function _ms_bareTitle(s) {
        return String(s == null ? '' : s)
            .replace(/^\s*第\s*[0-9一二三四五六七八九十百千零两]+\s*章[\s:：·\-—]*/, '')
            .trim();
    }
    function _fmtWords(n) {
        n = parseInt(n) || 0;
        return n >= 1000 ? (Math.round(n / 1000 * 10) / 10) + 'k' : String(n);
    }

    function create(opts) {
        opts = opts || {};
        var PAGE_GAP = 24;     // 列宽=视口宽，翻页位移=列宽+间距
        var FLIP_MS = 240;     // 跨章滑出动画时长（≥ .reader-pages 的 .22s）

        var R = {
            chapters: opts.chapters || [],
            idx: 0, page: 0, totalPages: 1,
            _flipToken: 0,
            _anim: null,            // 双容器在途动画 { token, targetIdx, targetPage }
            _pendingAtEnd: false,   // 上一页跨章：目标章末页（内容加载后应用）
            el: function (id) { return document.getElementById(id); },
        };
        var viewport = R.el(opts.viewport || 'reader-viewport');
        var pages = R.el(opts.pages || 'reader-pages');
        var pagesNext = opts.pagesNext ? R.el(opts.pagesNext) : null;
        var tocList = R.el('toc-list'), tocDrawer = R.el('toc-drawer'), tocBackdrop = R.el('toc-backdrop');
        var rtPrev = R.el('rt-prev-page'), rtNext = R.el('rt-next-page'), rtPos = R.el('rt-pos');

        /* ─── CSS columns 分页 ─── */
        function _colW() { return viewport ? viewport.clientWidth : 0; }
        function _measurePages(container) {
            if (!viewport || !container) return 1;
            var H = viewport.clientHeight, colW = _colW();
            if (!H) return 1;
            container.style.columnWidth = colW + 'px';
            container.style.columnGap = PAGE_GAP + 'px';
            container.style.height = H + 'px';
            var totalW = container.scrollWidth;
            return Math.max(1, Math.round((totalW + PAGE_GAP) / (colW + PAGE_GAP)));
        }
        function paginate() {
            if (!viewport || !pages) return;
            R.totalPages = _measurePages(pages);
            R.page = Math.max(0, Math.min(R.page, R.totalPages - 1));
            showPage(R.page);
        }
        function showPage(i) {
            if (!viewport || !pages) return;
            var colW = _colW();
            R.page = Math.max(0, Math.min(i, R.totalPages - 1));
            pages.style.transform = 'translateX(' + (-R.page * (colW + PAGE_GAP)) + 'px)';
            _updatePosition();
            if (rtPrev) rtPrev.disabled = (R.page <= 0) && (R.idx <= 0);
            if (rtNext) rtNext.disabled = (R.page >= R.totalPages - 1) && (R.idx >= R.chapters.length - 1);
        }
        function _updatePosition() {
            if (rtPos) rtPos.textContent = '第 ' + (R.idx + 1) + ' / ' + R.chapters.length
                + ' 章 · 第 ' + (R.page + 1) + ' / ' + R.totalPages + ' 页';
        }
        function _updateNavState() { _updatePosition(); }

        /* ─── 瞬时切换辅助：换内容/定位时禁用 transform 过渡（防旧 transform 大幅位移动画） ─── */
        function _swapContentInstant(fn) {
            if (!pages) { fn(); return; }
            pages.style.transition = 'none';
            fn();
            void pages.offsetWidth;
            pages.style.transition = '';
        }
        function _readTransform() {
            var m = /translateX\((-?[\d.]+)px\)/.exec(pages.style.transform || '');
            return m ? parseFloat(m[1]) : 0;
        }
        /* 副容器生命周期：清空+隐藏（先关 transition 再清 transform/visibility，防残留动画飞回） */
        function _clearSecondary(s) {
            if (!s) return;
            s.style.transition = 'none';
            s.style.transform = '';
            s.style.visibility = 'hidden';
            s.innerHTML = '';
        }
        function _abortAnim() {
            R._anim = null;
            _clearSecondary(pagesNext);
        }

        /* ─── 渲染 + 取正文（经回调注入差异） ─── */
        function renderChapter(ch, content, container) {
            if (typeof opts.renderChapter === 'function') { opts.renderChapter(ch, content, container); return; }
            if (!container) return;
            if (!ch) { container.innerHTML = ''; return; }
            var num = ch.index || ch.num || '';
            var srcHtml = '';
            if (ch.source) {
                var _src = ch.source === 'fanqie' ? '番茄' : (ch.source === 'merged' ? '多源合并' : ch.source);
                srcHtml = '<div class="m-chapter-src">获取来源：' + _ms_esc(_src) + '</div>';
            }
            container.innerHTML = '<div class="m-chapter-title">第' + _ms_esc(num) + '章 ' + _ms_esc(_ms_bareTitle(ch.title)) + '</div>'
                + srcHtml
                + _ms_esc(content == null ? '' : content);
        }
        function _applyChapter(i, opts_) {
            if (!R.chapters.length) return;
            opts_ = opts_ || {};
            i = Math.max(0, Math.min(i, R.chapters.length - 1));
            R.idx = i;
            var ch = R.chapters[i];
            R.page = (opts_.page != null) ? opts_.page : 0;
            R._pendingAtEnd = !!opts_.atEnd;
            function done(content) {
                renderChapter(ch, content, pages);
                paginate();
                if (R._pendingAtEnd) showPage(R.totalPages - 1);
                renderToc();
                _updateNavState();
                persist();
                if (typeof opts.onChapterChange === 'function') opts.onChapterChange(controller);
            }
            if (typeof opts.getChapterContent === 'function') { opts.getChapterContent(ch, done); }
            else { done(ch && ch.content); }
        }
        function goToChapter(i, opts_) {
            R._flipToken++;
            _abortAnim();
            _swapContentInstant(function () { _applyChapter(i, opts_); });
        }
        function nextChapter() { if (R.idx < R.chapters.length - 1) goToChapter(R.idx + 1); }
        function prevChapter() { if (R.idx > 0) goToChapter(R.idx - 1); }

        /* ─── 跨章翻页：统一双容器推入动画（写作台同步 / 外部书库懒加载，取到正文后动画）；
           pagesNext 缺失时兜底滑出动画（旧内容滑出一页宽 → 硬切新章） ─── */
        function crossChapterFlip(dir, targetIdx, opts_) {
            if (!pages) { _applyChapter(targetIdx, opts_); return; }
            if (R._anim) return;
            targetIdx = Math.max(0, Math.min(targetIdx, R.chapters.length - 1));
            var targetCh = R.chapters[targetIdx];
            if (!targetCh) { _applyChapter(targetIdx, opts_); return; }
            var token = ++R._flipToken;
            var colW = _colW(), k = (PAGE_GAP + colW) || PAGE_GAP;
            var oldT = _readTransform();
            var forward = dir > 0;
            function loadContent(cb) {
                if (typeof opts.getChapterContent === 'function') opts.getChapterContent(targetCh, cb);
                else cb(targetCh && targetCh.content);
            }
            if (!pagesNext) {
                // 兜底：无副容器 → 旧内容滑出一页宽，然后硬切新章
                _swapContentInstant(function () { pages.style.transform = 'translateX(' + oldT + 'px)'; });
                pages.style.transform = 'translateX(' + (oldT + (forward ? -k : k)) + 'px)';
                setTimeout(function () {
                    if (token !== R._flipToken) return;
                    _swapContentInstant(function () { _applyChapter(targetIdx, opts_); });
                }, FLIP_MS);
                return;
            }
            // 先取目标章正文（写作台同步返回；外部书库懒加载/已预取），再双容器推入动画
            loadContent(function (content) {
                if (token !== R._flipToken) return;
                R._anim = { token: token, targetIdx: targetIdx };
                var sec = pagesNext;
                // ① 预渲染目标章进副容器 + 测总页数
                renderChapter(targetCh, content, sec);
                var nextTotal = _measurePages(sec);
                var secStart, secEnd, primEnd, targetPage;
                if (forward) { targetPage = 0; secStart = +k; secEnd = 0; primEnd = oldT - k; }
                else { targetPage = nextTotal - 1; secStart = -nextTotal * k; secEnd = -(nextTotal - 1) * k; primEnd = oldT + k; }
                // ② 拍1：副容器隐藏中无动画就位 → 置可见；主容器归一化当前页
                sec.style.transition = 'none';
                sec.style.transform = 'translateX(' + secStart + 'px)';
                void sec.offsetWidth;
                sec.style.transition = '';
                sec.style.visibility = 'visible';
                _swapContentInstant(function () { pages.style.transform = 'translateX(' + oldT + 'px)'; });
                // ③ 拍2：双容器同帧动画（.reader-pages 基类 .22s transition 生效）
                pages.style.transform = 'translateX(' + primEnd + 'px)';
                sec.style.transform = 'translateX(' + secEnd + 'px)';
                // ④ 拍3：动画结束 promote（token 守卫）
                setTimeout(function () {
                    if (token !== R._flipToken) { _abortAnim(); return; }
                    _promoteSecondary(sec, targetIdx, targetPage, token);
                }, FLIP_MS);
            });
        }
        function _promoteSecondary(sec, targetIdx, targetPage, token) {
            if (token !== R._flipToken) { _abortAnim(); return; }
            R._anim = null;
            _swapContentInstant(function () {
                pages.innerHTML = sec.innerHTML;   // 情节段纯 HTML span + data，无监听器，安全
                R.idx = targetIdx;
                R.page = targetPage;
                paginate();
                renderToc();
                _updateNavState();
                persist();
                if (typeof opts.onChapterChange === 'function') opts.onChapterChange(controller);
            });
            _clearSecondary(sec);
        }
        function nextPage() {
            if (R.page < R.totalPages - 1) { R._flipToken++; showPage(R.page + 1); }
            else if (R.idx < R.chapters.length - 1) crossChapterFlip(1, R.idx + 1, { page: 0 });
        }
        function prevPage() {
            if (R.page > 0) { R._flipToken++; showPage(R.page - 1); }
            else if (R.idx > 0) crossChapterFlip(-1, R.idx - 1, { atEnd: true });
        }

        /* ─── 目录抽屉 ─── */
        function renderToc() {
            if (!tocList) return;
            tocList.innerHTML = R.chapters.map(function (c, i) {
                var num = c.index || c.num || (i + 1);
                return '<button type="button" class="toc-item' + (i === R.idx ? ' is-current' : '') + '" data-idx="' + i + '">'
                    + '<span class="toc-num">' + _ms_esc(num) + '</span>'
                    + '<span class="toc-title">' + _ms_esc(c.title || ('第' + num + '章')) + '</span>'
                    + (c.draft ? '<span class="m-draft-tag">草稿</span>' : '')
                    + '<span class="toc-meta">' + _fmtWords(c.word_count) + '</span>'
                    + '</button>';
            }).join('');
        }
        function openToc() {
            if (tocDrawer) tocDrawer.classList.add('open');
            if (tocBackdrop) tocBackdrop.hidden = false;
            if (tocList) {
                var cur = tocList.querySelector('.toc-item.is-current');
                if (cur) {
                    var lr = tocList.getBoundingClientRect(), cr = cur.getBoundingClientRect();
                    tocList.scrollTop = Math.max(0, tocList.scrollTop + (cr.top - lr.top) - (lr.height - cr.height) / 2);
                }
            }
        }
        function closeToc() {
            if (tocDrawer) tocDrawer.classList.remove('open');
            if (tocBackdrop) tocBackdrop.hidden = true;
        }

        /* ─── 位置持久化 ─── */
        function persist() {
            if (!opts.persistKey) return;
            try { localStorage.setItem(opts.persistKey, JSON.stringify({ idx: R.idx, page: R.page })); } catch (e) {}
        }
        function _restore() {
            if (!opts.persistKey) return;
            try {
                var d = JSON.parse(localStorage.getItem(opts.persistKey) || 'null');
                if (d && typeof d.idx === 'number') {
                    R.idx = Math.max(0, Math.min(d.idx, R.chapters.length - 1));
                    R.page = (typeof d.page === 'number') ? d.page : 0;
                }
            } catch (e) {}
        }

        /* ─── 绑定 ─── */
        function bind(id, fn) { var e = R.el(id); if (e) e.addEventListener('click', fn); }
        bind(opts.btnToc || 'rt-toc', openToc);
        bind('rt-prev-page', prevPage);
        bind('rt-next-page', nextPage);
        bind('toc-close', closeToc);
        if (tocBackdrop) tocBackdrop.addEventListener('click', closeToc);
        if (tocList) tocList.addEventListener('click', function (e) {
            var item = e.target.closest ? e.target.closest('.toc-item') : null;
            if (item) { goToChapter(+item.getAttribute('data-idx'), { page: 0 }); closeToc(); }
        });
        document.addEventListener('keydown', function (e) {
            if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
            if (e.key === 'ArrowRight') nextPage();
            else if (e.key === 'ArrowLeft') prevPage();
            else if (e.key === ']') nextChapter();
            else if (e.key === '[') prevChapter();
        });
        if (viewport) {
            var wheelAcc = 0, WHEEL_PAGE = 80;
            viewport.addEventListener('wheel', function (e) {
                e.preventDefault();
                wheelAcc += e.deltaY;
                if (Math.abs(wheelAcc) >= WHEEL_PAGE) {
                    if (wheelAcc > 0) nextPage(); else prevPage();
                    wheelAcc = 0;
                }
            }, { passive: false });
        }
        if (window.ResizeObserver && viewport) new ResizeObserver(function () { paginate(); }).observe(viewport);

        /* 扩展用：元素位于第几列（页）——相对 pages 的 left / (列宽+间距) */
        function pageOfElement(el) {
            if (!viewport || !pages || !el) return 0;
            var colW = _colW();
            var x = el.getBoundingClientRect().left - pages.getBoundingClientRect().left;
            return Math.max(0, Math.floor(x / (colW + PAGE_GAP)));
        }

        /* 外部更新章节（写作台轮询：agent 写完情节段/章节后整体替换章节数据并重渲染当前章） */
        function setChapters(chs, targetIdx, page) {
            R._flipToken++;
            _abortAnim();
            R.chapters = Array.isArray(chs) ? chs : [];
            if (!R.chapters.length) { if (pages) pages.innerHTML = ''; renderToc(); return; }
            targetIdx = Math.max(0, Math.min(targetIdx == null ? R.idx : targetIdx, R.chapters.length - 1));
            R.idx = targetIdx;
            R.page = (typeof page === 'number') ? page : 0;
            var ch = R.chapters[R.idx];
            function done(content) {
                renderChapter(ch, content, pages);
                paginate();
                renderToc();
                _updateNavState();
                persist();
                if (typeof opts.onChapterChange === 'function') opts.onChapterChange(controller);
            }
            if (typeof opts.getChapterContent === 'function') opts.getChapterContent(ch, done);
            else done(ch && ch.content);
        }

        var controller = {
            goToChapter: goToChapter,
            nextChapter: nextChapter, prevChapter: prevChapter,
            nextPage: nextPage, prevPage: prevPage,
            showPage: showPage,
            pageOfElement: pageOfElement,
            refresh: function () { paginate(); },
            getState: function () { return { idx: R.idx, page: R.page }; },
            idx: function () { return R.idx; },
            setChapters: setChapters,
            el: function (id) { return document.getElementById(id); },
        };
        // chapters 只读 getter：setChapters 替换后始终读到最新数组
        Object.defineProperty(controller, 'chapters', {
            get: function () { return R.chapters; },
        });

        if (!R.chapters.length) return controller;
        _restore();
        _applyChapter(R.idx, { page: R.page });
        return controller;
    }

    return { create: create };
})();
