// ═══════════════════════════════════════════════════
// 外部书库阅读器（复用写作台 CSS-columns 分页机制，正文按章懒加载）
// 用法：window.initNovelReader({platform, folder, title}, [{index,title,word_count}])
// 依赖：reader.css；/api/scout/novels/chapter 单章端点
// ═══════════════════════════════════════════════════

window.initNovelReader = function (meta, chapterList) {
    function _ms_esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    }
    function _fmtWords(n) { return n >= 1000 ? (Math.round(n / 1000 * 10) / 10) + 'k' : String(n); }

    var Reader = {
        meta: meta || {},
        chapters: chapterList || [],   // [{index,title,word_count}]（正文懒加载）
        idx: 0, page: 0, totalPages: 1,
        PAGE_GAP: 24,                  // 列宽=视口宽，翻页位移=列宽+间距
        FLIP_MS: 240,                  // 跨章滑出动画时长（≥ reader.css .22s）
        _cache: {},                    // index -> 正文（已加载章缓存）
        _pendingAtEnd: false,          // 上一页跨章：目标章末页（内容加载后应用）
        _flipToken: 0,                 // 跨章动画守卫
        el: function (id) { return document.getElementById(id); }
    };

    function showLoading(msg) {
        var pages = Reader.el('reader-pages');
        if (pages) pages.innerHTML = '<div class="m-loading">' + _ms_esc(msg || '加载中…') + '</div>';
    }
    function renderChapter(ch, content) {
        var pages = Reader.el('reader-pages');
        if (!pages) return;
        if (!ch) { pages.innerHTML = ''; return; }
        var html = '<div class="m-chapter-title">第' + _ms_esc(ch.index) + '章 ' + _ms_esc(ch.title || '') + '</div>';
        html += _ms_esc(content);
        pages.innerHTML = html;
    }

    /* ─── CSS columns 分页：列宽=视口宽，列横向溢出 → scrollWidth 数总页数 ─── */
    function _colW() {
        var vp = Reader.el('reader-viewport');
        return vp ? vp.clientWidth : 0;
    }
    function paginate() {
        var vp = Reader.el('reader-viewport'), pages = Reader.el('reader-pages');
        if (!vp || !pages) return;
        var H = vp.clientHeight, GAP = Reader.PAGE_GAP, colW = _colW();
        if (!H) return;
        pages.style.columnWidth = colW + 'px';
        pages.style.columnGap = GAP + 'px';
        pages.style.height = H + 'px';
        var totalW = pages.scrollWidth;
        Reader.totalPages = Math.max(1, Math.round((totalW + GAP) / (colW + GAP)));
        Reader.page = Math.max(0, Math.min(Reader.page, Reader.totalPages - 1));
        showPage(Reader.page);
    }
    function showPage(i) {
        var vp = Reader.el('reader-viewport'), pages = Reader.el('reader-pages');
        if (!vp || !pages) return;
        var GAP = Reader.PAGE_GAP, colW = _colW();
        Reader.page = Math.max(0, Math.min(i, Reader.totalPages - 1));
        pages.style.transform = 'translateX(' + (-Reader.page * (colW + GAP)) + 'px)';
        _updatePosition();
        var pp = Reader.el('rt-prev-page'), np = Reader.el('rt-next-page');
        if (pp) pp.disabled = (Reader.page <= 0) && (Reader.idx <= 0);
        if (np) np.disabled = (Reader.page >= Reader.totalPages - 1) && (Reader.idx >= Reader.chapters.length - 1);
    }
    function _updatePosition() {
        var rt = Reader.el('rt-pos');
        if (rt) rt.textContent = '第 ' + (Reader.idx + 1) + ' / ' + Reader.chapters.length
            + ' 章 · 第 ' + (Reader.page + 1) + ' / ' + Reader.totalPages + ' 页';
    }
    function _updateNavState() { _updatePosition(); }

    /* ─── 瞬时切换辅助：换内容/定位时禁用 transform 过渡（防旧 transform 大幅位移动画） ─── */
    function _swapContentInstant(fn) {
        var pages = Reader.el('reader-pages');
        if (!pages) { fn(); return; }
        pages.style.transition = 'none';
        fn();
        void pages.offsetWidth;
        pages.style.transition = '';
    }
    function _readTransform(pages) {
        var m = /translateX\((-?[\d.]+)px\)/.exec(pages.style.transform || '');
        return m ? parseFloat(m[1]) : 0;
    }

    /* ─── 懒加载：拉取单章正文（缓存）→ 渲染+分页 ─── */
    function loadChapter(index) {
        var url = '/api/scout/novels/chapter?platform=' + encodeURIComponent(Reader.meta.platform || 'fanqie')
            + '&folder=' + encodeURIComponent(Reader.meta.folder || '') + '&chapter=' + index;
        fetch(url)
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (!d || !d.ok) { showLoading('加载失败'); return; }
                Reader._cache[index] = (d.chapter && d.chapter.content) || '';
                var cur = Reader.chapters[Reader.idx];
                if (cur && String(cur.index) === String(index)) {
                    renderChapter(d.chapter, Reader._cache[index]);
                    paginate();
                    if (Reader._pendingAtEnd) showPage(Reader.totalPages - 1);
                    else showPage(Reader.page);
                }
            })
            .catch(function () { showLoading('加载失败'); });
    }

    function _applyChapter(i, opts) {
        if (!Reader.chapters.length) return;
        opts = opts || {};
        Reader.idx = Math.max(0, Math.min(i, Reader.chapters.length - 1));
        var c = Reader.chapters[Reader.idx];
        Reader.page = (opts.page != null) ? opts.page : 0;
        Reader._pendingAtEnd = !!opts.atEnd;
        var cached = Reader._cache[c.index];
        if (cached !== undefined) { renderChapter(c, cached); paginate(); }
        else { showLoading(); loadChapter(c.index); }
        renderToc();
        _updateNavState();
        persist();
    }
    function goToChapter(i, opts) {
        Reader._flipToken++;
        _swapContentInstant(function () { _applyChapter(i, opts); });
    }
    function nextChapter() { if (Reader.idx < Reader.chapters.length - 1) goToChapter(Reader.idx + 1); }
    function prevChapter() { if (Reader.idx > 0) goToChapter(Reader.idx - 1); }

    /* 跨章翻页动画：旧内容平滑滑出一页宽 → 空白处硬切新章（懒加载时先 loading） */
    function crossChapterFlip(dir, targetIdx, opts) {
        var pages = Reader.el('reader-pages');
        if (!pages) { _applyChapter(targetIdx, opts); return; }
        var token = ++Reader._flipToken;
        var k = (Reader.PAGE_GAP + _colW()) || Reader.PAGE_GAP;
        var step = dir > 0 ? -k : k;
        var oldT = _readTransform(pages);
        _swapContentInstant(function () { pages.style.transform = 'translateX(' + oldT + 'px)'; });
        pages.style.transform = 'translateX(' + (oldT + step) + 'px)';
        setTimeout(function () {
            if (token !== Reader._flipToken) return;
            _swapContentInstant(function () { _applyChapter(targetIdx, opts); });
        }, Reader.FLIP_MS);
    }
    function nextPage() {
        if (Reader.page < Reader.totalPages - 1) { Reader._flipToken++; showPage(Reader.page + 1); }
        else if (Reader.idx < Reader.chapters.length - 1) crossChapterFlip(1, Reader.idx + 1, { page: 0 });
    }
    function prevPage() {
        if (Reader.page > 0) { Reader._flipToken++; showPage(Reader.page - 1); }
        else if (Reader.idx > 0) crossChapterFlip(-1, Reader.idx - 1, { atEnd: true });
    }

    /* ─── 目录抽屉 ─── */
    function renderToc() {
        var list = Reader.el('toc-list');
        if (!list) return;
        list.innerHTML = Reader.chapters.map(function (c, i) {
            return '<button type="button" class="toc-item' + (i === Reader.idx ? ' is-current' : '') + '" data-idx="' + i + '">'
                + '<span class="toc-num">' + _ms_esc(c.index) + '</span>'
                + '<span class="toc-title">' + _ms_esc(c.title || ('第' + c.index + '章')) + '</span>'
                + '<span class="toc-meta">' + _fmtWords(c.word_count || 0) + '</span>'
                + '</button>';
        }).join('');
    }
    function openToc() {
        var d = Reader.el('toc-drawer'), b = Reader.el('toc-backdrop');
        if (d) d.classList.add('open');
        if (b) b.hidden = false;
        var list = Reader.el('toc-list');
        if (list) { var cur = list.querySelector('.toc-item.is-current'); if (cur) cur.scrollIntoView({ block: 'nearest' }); }
    }
    function closeToc() {
        var d = Reader.el('toc-drawer'), b = Reader.el('toc-backdrop');
        if (d) d.classList.remove('open');
        if (b) b.hidden = true;
    }

    /* ─── 位置持久化 ─── */
    function _lsKey() { return 'ne_novel_reader_' + (Reader.meta.folder || 'x'); }
    function persist() {
        try { localStorage.setItem(_lsKey(), JSON.stringify({ idx: Reader.idx, page: Reader.page })); } catch (e) {}
    }
    function _restore() {
        try {
            var d = JSON.parse(localStorage.getItem(_lsKey()) || 'null');
            if (d && typeof d.idx === 'number') {
                Reader.idx = Math.max(0, Math.min(d.idx, Reader.chapters.length - 1));
                Reader.page = (typeof d.page === 'number') ? d.page : 0;
            }
        } catch (e) {}
    }

    /* ─── 初始化 ─── */
    if (!Reader.el('reader-pages') || !Reader.chapters.length) return;
    _restore();
    goToChapter(Reader.idx, { page: Reader.page });

    function bind(id, fn) { var el = Reader.el(id); if (el) el.addEventListener('click', fn); }
    bind('rt-toc', openToc);
    bind('rt-prev-page', prevPage);
    bind('rt-next-page', nextPage);
    bind('toc-close', closeToc);
    var bd = Reader.el('toc-backdrop');
    if (bd) bd.addEventListener('click', closeToc);
    var tl = Reader.el('toc-list');
    if (tl) tl.addEventListener('click', function (e) {
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
    var vp = Reader.el('reader-viewport');
    if (vp) {
        var wheelAcc = 0, WHEEL_PAGE = 80;
        vp.addEventListener('wheel', function (e) {
            e.preventDefault();
            wheelAcc += e.deltaY;
            if (Math.abs(wheelAcc) >= WHEEL_PAGE) {
                if (wheelAcc > 0) nextPage(); else prevPage();
                wheelAcc = 0;
            }
        }, { passive: false });
    }
    if (window.ResizeObserver && vp) new ResizeObserver(function () { paginate(); }).observe(vp);
};
