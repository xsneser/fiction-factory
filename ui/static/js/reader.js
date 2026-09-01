// ═══════════════════════════════════════════════════
// 外部书库阅读器（薄封装 reader_core.js 共享分页核心）
// 用法：window.initNovelReader({platform, folder, title}, [{index,title,word_count}])
// 差异：正文按章懒加载 /api/scout/novels/chapter；加载某章后预取相邻章（跨章推入动画无停顿）；
//       pagesNext 启用双容器推入动画（与写作台一致）；位置持久化 ne_novel_reader_<folder>
// ═══════════════════════════════════════════════════

window.initNovelReader = function (meta, chapterList) {
    var _cache = {};   // index -> 正文（已加载/已预取章缓存）

    function loadOne(ch, cb) {
        var key = String(ch && ch.index);
        if (_cache[key] !== undefined) { cb(_cache[key]); return; }
        var url = '/api/scout/novels/chapter?platform=' + encodeURIComponent((meta && meta.platform) || 'fanqie')
            + '&folder=' + encodeURIComponent((meta && meta.folder) || '') + '&chapter=' + key;
        fetch(url)
            .then(function (r) { return r.json(); })
            .then(function (d) {
                var content = (d && d.chapter && d.chapter.content) || '';
                _cache[key] = content;
                cb(content);
            })
            .catch(function () { _cache[key] = ''; cb(''); });
    }

    // 预取相邻章（上一/下一）进缓存：用户翻到章末时下一章内容已就绪，推入动画即时播放
    function prefetchAround(ch) {
        var list = chapterList || [];
        var key = String(ch && ch.index);
        for (var i = 0; i < list.length; i++) {
            if (String(list[i].index) === key) {
                [list[i - 1], list[i + 1]].forEach(function (nb) {
                    if (nb && _cache[String(nb.index)] === undefined) loadOne(nb, function () {});
                });
                break;
            }
        }
    }

    ReaderCore.create({
        chapters: chapterList || [],
        pagesNext: 'reader-pages-next',   // 双容器推入动画（与写作台一致）
        persistKey: 'ne_novel_reader_' + ((meta && meta.folder) || 'x'),
        getChapterContent: function (ch, cb) {
            loadOne(ch, function (content) { prefetchAround(ch); cb(content); });
        },
    });
};
