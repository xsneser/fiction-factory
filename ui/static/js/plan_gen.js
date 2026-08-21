/* ═══════════════════════════════════════════════════════════════════
 * 规划与生成（从写作台模板内联 JS 抽出的独立文件）
 *
 * 供规划页 /books/<id>/plan 使用（写作台已不再内嵌规划/生成面板）。
 * 依赖页面提供：
 *   - window.__BOOK_STORYLINE__  （故事线 JSON，生成事件后更新）
 *   - window.__STORYLINE_ID__ / window.storylineId（书 id，storyline_outline_card 也要）
 *   - window.StoryLine（story_line.js，可选——没有 #editor-storyline 挂载则跳过刷新）
 *   - showToast / flashToast（base.js）
 *
 * 后续可与 agent skill 结合：这些动作对应 MCP 工具 generate_full_outline /
 * generate_outlines / confirm_outlines / fill_plots / fill_gags / extend_outline。
 * ═══════════════════════════════════════════════════════════════════ */

function refreshStoryline(sl) {
    if (!window.StoryLine) return;
    if (!document.getElementById('editor-storyline')) return;  // 无故事线挂载则跳过
    window.StoryLine.init('editor-storyline', sl || {}, {scrollable: true});
}
(function () {
    refreshStoryline(window.__BOOK_STORYLINE__);
})();

function _sid() {
    return window.storylineId || window.__STORYLINE_ID__ || '';
}

/* ─── 规划功能（从原规划页并入） ─── */
var gFullRunning = false;
function genFullStart() {
    if (gFullRunning) return;
    gFullRunning = true;
    var det = document.querySelector('.right-plan');
    if (det) det.open = true;   // 生成时展开规划面板，让用户看到进度
    var btn = document.getElementById('btn-gen-full');
    btn.disabled = true; btn.textContent = '⏳ 生成中...';
    var box = document.getElementById('gen-full-box');
    box.style.display = 'block';
    for (var i = 1; i <= 6; i++) {
        var el = document.getElementById('gf-' + i);
        if (el) { el.className = 'gen-full-phase'; el.querySelector('.gfp-status').textContent = ''; }
    }
    var feed = document.getElementById('gen-feed');
    if (feed) feed.innerHTML = '';
    var panel = document.getElementById('gen-panel');
    if (panel) panel.style.display = 'block';
    ['phase-outlines', 'phase-plots', 'phase-gags', 'phase-ready'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.style.display = 'none';
    });
    refreshStoryline(window.__BOOK_STORYLINE__);
    fetch('/api/storyline/' + _sid() + '/generate-full', { method: 'POST' })
        .then(function(resp) {
            var reader = resp.body.getReader();
            var decoder = new TextDecoder();
            var buf = '';
            function read() {
                return reader.read().then(function(r) {
                    if (r.done) { genFullFinish(); return; }
                    buf += decoder.decode(r.value, {stream: true});
                    var lines = buf.split('\n');
                    buf = lines.pop() || '';
                    for (var li = 0; li < lines.length; li++) {
                        var line = lines[li];
                        if (line.indexOf('data: ') !== 0) continue;
                        try { genFullHandle(JSON.parse(line.slice(6))); } catch(e) {}
                    }
                    return read();
                });
            }
            return read();
        }).catch(function(err) {
            genFullLog('❌ 连接失败: ' + err.message, 'error');
            genFullFinish();
        });
}
function genFullLog(msg, cls) {
    var feed = document.getElementById('gen-feed');
    if (!feed) return;
    var d = document.createElement('div');
    d.className = 'gf-item' + (cls ? ' ' + cls : '');
    d.textContent = msg;
    feed.appendChild(d);
    feed.scrollTop = feed.scrollHeight;
}
function genFeedAction(kind, text) { genFullLog(text, 'action action-' + kind); }
function genFeedDecision(evt) {
    var chosen = evt.chosen || {};
    var text = '';
    if (evt.kind === 'outline_choice') text = (chosen && chosen.name) || '';
    else if (evt.kind === 'plot_choice') text = (Array.isArray(chosen) ? chosen.map(function(x){ return x.name || x; }).join('、') : (chosen && chosen.name)) || '';
    else if (evt.kind === 'theme_review') text = '内涵 ' + ((chosen.themes||[]).join('、') || '—');
    else if (evt.kind === 'thread_split') text = '线程 ' + ((chosen.threads||[]).join('、') || '—') + ' · 拆分 ' + (chosen.splits||0) + ' 处';
    else if (evt.kind === 'validate') text = ((chosen.issues||[]).length) + ' 条建议';
    if (!text && evt.candidates && evt.candidates.length) {
        text = '候选 ' + evt.candidates.slice(0, 6).map(function(x){ return x.name || x; }).join('、');
    }
    genFullLog('✅ ' + (evt.step || '决策') + (text ? ' → ' + text : ''), 'decision');
}
function genFullHandle(evt) {
    var type = evt.event;
    if (type === 'phase') {
        var el = document.getElementById('gf-' + (evt.phase || 0));
        if (el) { el.className = 'gen-full-phase active'; el.querySelector('.gfp-status').textContent = evt.desc || ''; }
        genFullLog(evt.message, 'info');
    } else if (type === 'phase_done') {
        var el2 = document.getElementById('gf-' + (evt.phase || 0));
        if (el2) { el2.className = 'gen-full-phase done'; el2.querySelector('.gfp-status').textContent = '✅'; }
        genFullLog(evt.message, 'success');
    } else if (type === 'progress') {
        genFullLog(evt.message, 'info');
    } else if (type === 'outline_added') {
        var range = (evt.start_chapter && evt.end_chapter) ? '（第' + evt.start_chapter + '-' + evt.end_chapter + '章）' : '';
        genFeedAction('outline', '📋 添加大纲：『' + (evt.name || evt.message) + '』' + range);
    } else if (type === 'plot_added') {
        genFeedAction('plot', '🧩 添加桥段：『' + (evt.message || '') + '』' + (evt.outline_name ? '（大纲「' + evt.outline_name + '」）' : ''));
    } else if (type === 'outline_plots') {
        genFullLog('🧩 ' + (evt.message || '') + ' → ' + (evt.plot_count || 0) + ' 个桥段', 'info');
    } else if (type === 'theme_injected') {
        genFeedAction('gag', '💡 为『' + (evt.message || '') + '』挂载内涵 ×' + ((evt.theme_hints || []).length));
    } else if (type === 'decision') {
        genFeedDecision(evt);
    } else if (type === 'warnings') {
        genFullLog('⚠️ ' + (evt.message || ''), 'info');
    } else if (type === 'done') {
        genFullLog('🎉 ' + (evt.message || '大纲生成完成') + '，已合并到当前故事线！', 'success');
        setTimeout(function(){ location.reload(); }, 600);
        return;
    } else if (type === 'error') {
        genFullLog('❌ ' + (evt.message || '未知错误'), 'error');
        genFullFinish();
        return;
    }
    if (['outline_added','outline_plots','plot_added','theme_injected','phase_done','done'].indexOf(type) >= 0 && evt.storyline) {
        window.__BOOK_STORYLINE__ = evt.storyline;
        refreshStoryline(evt.storyline);
    }
}
function applyPhaseVisibility() {
    var tl = window.__BOOK_STORYLINE__ || {};
    var ph = tl.phase || 'config';
    function show(id, cond) { var el = document.getElementById(id); if (el) el.style.display = cond ? '' : 'none'; }
    show('phase-outlines', ['config', 'outlines'].indexOf(ph) >= 0);
    show('phase-plots', ph === 'plots');
    show('phase-gags', ph === 'gags');
    show('phase-ready', ph === 'ready');
}
function genFullFinish() {
    gFullRunning = false;
    var btn = document.getElementById('btn-gen-full');
    if (btn) { btn.disabled = false; btn.textContent = '✨ 一键生成完整大纲'; }
    var panel = document.getElementById('gen-panel');
    if (panel) panel.style.display = 'none';
    applyPhaseVisibility();
    refreshStoryline(window.__BOOK_STORYLINE__);
}
function generateOutlines() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = '生成中...';
    fetch('/api/storyline/' + _sid() + '/generate-outlines', { method: 'POST' })
        .then(r => r.json()).then(d => { if (d.ok) { flashToast('✅ 大纲序列已生成', 'success'); location.reload(); } else showToast(d.error, 'error'); })
        .catch(e => showToast('失败: ' + e.message, 'error'))
        .finally(() => { btn.disabled = false; btn.textContent = '🤖 AI 生成大纲序列'; });
}
function addOutline() {
    fetch('/api/storyline/' + _sid() + '/generate-outlines?mode=rule', { method: 'POST' })
        .then(r => r.json()).then(d => { if (d.ok) { flashToast('✅ 已添加大纲', 'success'); location.reload(); } else showToast(d.error, 'error'); })
        .catch(e => showToast('失败: ' + e.message, 'error'));
}
function extendOutline() {
    var btn = event.target;
    if (!confirm('在故事线末尾追加一段新大纲弧（含桥段），用于续写扩展。确定？')) return;
    btn.disabled = true; btn.textContent = '扩展中...';
    fetch('/api/storyline/' + _sid() + '/extend-outline', { method: 'POST' })
        .then(r => r.json()).then(d => {
            if (d.ok) { flashToast('✅ 故事线已扩展', 'success'); location.reload(); }
            else showToast(d.error || '扩展失败', 'error');
        })
        .catch(e => showToast('失败: ' + e.message, 'error'));
}
function confirmOutlines() {
    if (!confirm('确认故事线？确认后进入桥段编排阶段。')) return;
    var btn = document.getElementById('btn-confirm-outlines');
    btn.disabled = true; btn.textContent = '处理中...';
    fetch('/api/storyline/' + _sid() + '/confirm-outlines', { method: 'POST' })
        .then(r => r.json()).then(d => { if (d.ok) { flashToast('✅ 已确认故事线', 'success'); location.reload(); } else showToast(d.error, 'error'); })
        .catch(e => showToast('失败: ' + e.message, 'error'));
}
function fillPlots() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = '填充中...';
    fetch('/api/storyline/' + _sid() + '/fill-plots', { method: 'POST' })
        .then(r => r.json()).then(d => { if (d.ok) { flashToast('✅ 桥段已填充', 'success'); location.reload(); } else showToast(d.error, 'error'); })
        .catch(e => showToast('失败: ' + e.message, 'error'))
        .finally(() => { btn.disabled = false; btn.textContent = '🤖 AI 填充桥段'; });
}
function confirmPlots() {
    if (!confirm('确认桥段编排？确认后进入加料注入阶段。')) return;
    fetch('/api/storyline/' + _sid() + '/fill-gags', { method: 'POST' })
        .then(r => r.json()).then(d => { if (d.ok) { flashToast('✅ 已进入加料注入', 'success'); location.reload(); } else showToast(d.error, 'error'); });
}
function togglePlotConfirm(plotId, checked) {
    fetch('/api/storyline/' + _sid() + '/plot-confirm', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plot_id: plotId, confirmed: checked})
    });
}
function injectGags() {
    var btn = event.target;
    btn.disabled = true; btn.textContent = '注入中...';
    fetch('/api/storyline/' + _sid() + '/fill-gags', { method: 'POST' })
        .then(r => r.json()).then(d => { if (d.ok) { flashToast('✅ 内涵已挂载', 'success'); location.reload(); } else showToast(d.error, 'error'); });
}
function startWriting() {
    location.href = '/books/' + _sid() + '/continue';
}
