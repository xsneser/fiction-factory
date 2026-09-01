// ═══════════════════════════════════════════════════
// 外部书库阅读器（薄封装 reader_core.js 共享分页核心）
// 用法：window.initNovelReader({platform, folder, title}, [{index,title,word_count}])
// 差异：正文按章懒加载 /api/scout/novels/chapter；位置持久化 ne_novel_reader_<folder>
// ═══════════════════════════════════════════════════

window.initNovelReader = function (meta, chapterList) {
    var _cache = {};   // index -> 正文（已加载章缓存）

    ReaderCore.create({
        chapters: chapterList || [],
        persistKey: 'ne_novel_reader_' + ((meta && meta.folder) || 'x'),
        getChapterContent: function (ch, cb) {
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
                .catch(function () { cb(''); });
        },
    });
};
