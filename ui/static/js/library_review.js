// ═══════════════════════════════════════════════════
// 库审查卡片 + 入库 公共逻辑（scout.html / extract.html 共用）
// 依赖：escapeHtml（base.html <head> 提供）
// 各页面的 showReviewCards 保留，负责指定结果区域并调用 renderReviewCards
// ═══════════════════════════════════════════════════

// 渲染审查卡片到指定区域（areaId）
function renderReviewCards(d, areaId) {
    var area = document.getElementById(areaId);
    if (!area) return;
    area.style.display = 'block';

    var html = '<div class="card" style="margin-bottom:16px">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center">';
    html += '<span style="font-size:18px;font-weight:600">📖 ' + escapeHtml(d.title || '') + '</span>';
    if (d.downloaded_chapters) html += '<span style="color:#8b949e">已下载 ' + escapeHtml(d.downloaded_chapters) + ' 章</span>';
    html += '</div>';
    html += '<div style="margin-top:16px;display:flex;gap:10px">';
    html += '<button class="btn-primary" onclick="ingestAll()">📦 全部入库</button>';
    html += '<button class="btn" style="background:#30363d;color:#f0f6fc" onclick="ingestSelected()">✅ 入库选中</button>';
    html += '</div></div>';

    // 分类标签 + 内容
    var cats = [
        {key:'plot', label:'🧩 桥段', items: d.plot_details || []},
        {key:'structure', label:'📋 大纲', items: d.structure_details || []},
        {key:'gag', label:'😂 笑点', items: d.gag_details || []},
        {key:'theme', label:'💡 内涵', items: d.theme_details || []},
    ];

    html += '<div class="tabs" style="margin-bottom:12px">';
    cats.forEach(function(c, i) {
        html += '<a href="javascript:;" class="review-tab ' + (i===0?'active':'') + '" data-tab="' + c.key + '" onclick="switchReviewTab(\'' + c.key + '\')">' + c.label + ' (' + c.items.length + ')</a>';
    });
    html += '</div>';

    cats.forEach(function(c) {
        html += '<div class="review-panel" id="review-' + c.key + '"' + (c.key!=='plot'?' style="display:none"':'') + '>';
        if (c.items.length === 0) {
            html += '<div class="empty" style="padding:30px"><p style="color:#484f58">无提取结果</p></div>';
        } else {
            c.items.forEach(function(item, idx) {
                html += '<label class="review-item">';
                html += '<input type="checkbox" class="review-cb" data-cat="' + c.key + '" data-idx="' + idx + '" checked>';
                html += '<div class="review-content">';
                if (c.key === 'plot') {
                    html += '<div><code>' + escapeHtml(item.category||'') + '</code> <strong>' + escapeHtml(item.name||'') + '</strong></div>';
                    html += '<div style="font-size:13px;color:#c9d1d9;margin-top:4px">' + escapeHtml(item.description||'').slice(0,120) + '</div>';
                    if (item.structure) html += '<div style="font-size:12px;color:#484f58;margin-top:2px">结构: ' + escapeHtml(item.structure).slice(0,100) + '</div>';
                } else if (c.key === 'structure') {
                    html += '<div><strong>' + escapeHtml(item.name||'') + '</strong></div>';
                    html += '<div style="font-size:13px;color:#c9d1d9;margin-top:4px">' + escapeHtml(item.description||'').slice(0,120) + '</div>';
                    if (item.stages) html += '<div style="font-size:12px;color:#484f58;margin-top:2px">阶段: ' + escapeHtml(item.stages.join(' → ')).slice(0,100) + '</div>';
                } else if (c.key === 'gag') {
                    html += '<div><code>' + escapeHtml(item.category||'') + '</code> <strong>' + escapeHtml(item.name||'') + '</strong></div>';
                    html += '<div style="font-size:13px;color:#c9d1d9;margin-top:4px">' + escapeHtml(item.pattern_description||'').slice(0,120) + '</div>';
                } else if (c.key === 'theme') {
                    html += '<div><strong>' + escapeHtml(item.name||'') + '</strong></div>';
                    html += '<div style="font-size:13px;color:#c9d1d9;margin-top:4px">' + escapeHtml(item.description||'').slice(0,120) + '</div>';
                }
                html += '</div></label>';
            });
        }
        html += '</div>';
    });

    area.innerHTML = html;
}

// 切换审查分类页签
function switchReviewTab(key) {
    document.querySelectorAll('.review-tab').forEach(function(t) { t.classList.remove('active'); });
    document.querySelectorAll('.review-panel').forEach(function(p) { p.style.display = 'none'; });
    var tab = document.querySelector('.review-tab[data-tab="' + key + '"]');
    if (tab) tab.classList.add('active');
    var panel = document.getElementById('review-' + key);
    if (panel) panel.style.display = 'block';
}

// 收集勾选的条目
function getCheckedItems() {
    var result = {plots:[], structures:[], gags:[], themes:[]};
    if (!window._lastReviewData) return result;
    var cbs = document.querySelectorAll('.review-cb:checked');
    cbs.forEach(function(cb) {
        var cat = cb.getAttribute('data-cat');
        var idx = parseInt(cb.getAttribute('data-idx'));
        if (cat === 'plot' && window._lastReviewData.plot_details[idx]) result.plots.push(window._lastReviewData.plot_details[idx]);
        if (cat === 'structure' && window._lastReviewData.structure_details[idx]) result.structures.push(window._lastReviewData.structure_details[idx]);
        if (cat === 'gag' && window._lastReviewData.gag_details[idx]) result.gags.push(window._lastReviewData.gag_details[idx]);
        if (cat === 'theme' && window._lastReviewData.theme_details[idx]) result.themes.push(window._lastReviewData.theme_details[idx]);
    });
    return result;
}

async function ingestAll() {
    var items = getCheckedItems();
    var total = items.plots.length + items.structures.length + items.gags.length + items.themes.length;
    if (total === 0) return alert('没有可入库的条目');
    await doIngest(items);
}

async function ingestSelected() {
    var items = getCheckedItems();
    var total = items.plots.length + items.structures.length + items.gags.length + items.themes.length;
    if (total === 0) return alert('请勾选要入库的条目');
    await doIngest(items);
}

async function doIngest(items) {
    var title = window._lastReviewData ? window._lastReviewData.title : '';
    var btn = document.querySelector('.btn-primary');
    var orig = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '入库中...'; }
    try {
        var r = await fetch('/api/scout/ingest', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title: title, plots: items.plots, structures: items.structures, gags: items.gags, themes: items.themes}),
        });
        var d = await r.json();
        if (d.ok) {
            // 日志统一在右侧状态栏显示（后端 task_manager 已记录）
        } else {
            alert('❌ 入库失败: ' + (d.error||''));
        }
    } catch(e) {
        alert('❌ 入库失败: ' + e.message);
    }
    if (btn) { btn.disabled = false; btn.textContent = orig; }
}
