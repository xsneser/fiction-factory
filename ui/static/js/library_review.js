// ═══════════════════════════════════════════════════
// 库审查卡片 + 入库 公共逻辑（scout.html 侦察/提取合并页 / extract 旧页共用）
// 依赖：escapeHtml（base.html <head> 提供）
// 数据源：window._lastReviewData = agent set_review 的 args
//   { title, platform, folder, downloaded_chapters, profile_id, profile_name,
//     plots[], structures[], gags[], characters[], style_rules[] }
// 兼容旧字段：plot_details / structure_details / gag_details / character_details
// ═══════════════════════════════════════════════════

// 五分类字段映射：key(tab) → 数据源字段（兼容新旧键名）
var REVIEW_CATS = [
    { key: 'plot',       label: '🧩 桥段',   field: 'plots',       src: function(d){ return d.plots || d.plot_details || []; } },
    { key: 'structure',  label: '📋 情节弧', field: 'structures',  src: function(d){ return d.structures || d.structure_details || []; } },
    { key: 'gag',        label: '😂 笑点',   field: 'gags',        src: function(d){ return d.gags || d.gag_details || []; } },
    { key: 'character',  label: '🎭 角色',   field: 'characters',  src: function(d){ return d.characters || d.character_details || []; } },
    { key: 'style',      label: '✍️ 风格',   field: 'style_rules', src: function(d){ return d.style_rules || d.style_details || []; } },
];

// 渲染审查卡片到指定区域（areaId）
function renderReviewCards(d, areaId) {
    var area = document.getElementById(areaId);
    if (!area) return;
    area.style.display = 'block';
    window._lastReviewData = d;

    var html = '<div class="card" style="margin-bottom:16px">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center">';
    html += '<span style="font-size:18px;font-weight:600">📖 ' + escapeHtml(d.title || '') + '</span>';
    if (d.downloaded_chapters) html += '<span style="color:#8b949e">已下载 ' + escapeHtml(d.downloaded_chapters) + ' 章</span>';
    html += '</div>';
    if (d.profile_name) html += '<div style="font-size:12px;color:#8b949e;margin-top:4px">风格规则归属笔名：' + escapeHtml(d.profile_name) + '</div>';
    html += '<div style="margin-top:16px;display:flex;gap:10px">';
    html += '<button class="btn-primary" onclick="ingestAll()">📦 全部入库</button>';
    html += '<button class="btn" style="background:#30363d;color:#f0f6fc" onclick="ingestSelected()">✅ 入库选中</button>';
    html += '</div></div>';

    html += '<div class="tabs" style="margin-bottom:12px">';
    REVIEW_CATS.forEach(function(c, i) {
        var items = c.src(d) || [];
        html += '<a href="javascript:;" class="review-tab ' + (i===0?'active':'') + '" data-tab="' + c.key + '" onclick="switchReviewTab(\'' + c.key + '\')">' + c.label + ' (' + items.length + ')</a>';
    });
    html += '</div>';

    REVIEW_CATS.forEach(function(c) {
        var items = c.src(d) || [];
        html += '<div class="review-panel" id="review-' + c.key + '"' + (c.key!=='plot'?' style="display:none"':'') + '>';
        if (items.length === 0) {
            html += '<div class="empty" style="padding:30px"><p style="color:#484f58">无提取结果</p></div>';
        } else {
            items.forEach(function(item, idx) {
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
                    // stages 是对象数组（StageNode），逐层取 name 而非整对象 join（防 [object Object]）
                    if (item.stages && item.stages.length) {
                        var stageNames = item.stages.map(function(s){ return (s && s.name) ? s.name : String(s); });
                        html += '<div style="font-size:12px;color:#484f58;margin-top:2px">阶段: ' + escapeHtml(stageNames.join(' → ')).slice(0,100) + '</div>';
                    }
                } else if (c.key === 'gag') {
                    html += '<div><code>' + escapeHtml(item.category||'') + '</code> <strong>' + escapeHtml(item.name||'') + '</strong></div>';
                    html += '<div style="font-size:13px;color:#c9d1d9;margin-top:4px">' + escapeHtml(item.pattern_description||item.description||'').slice(0,120) + '</div>';
                } else if (c.key === 'character') {
                    var arch = (item.archetypes && item.archetypes.length) ? item.archetypes.join(' / ') : '';
                    html += '<div><code>' + escapeHtml(arch||item.tags&&item.tags[0]||'') + '</code> <strong>' + escapeHtml(item.name||'') + '</strong></div>';
                    html += '<div style="font-size:13px;color:#c9d1d9;margin-top:4px">' + escapeHtml(item.personality||item.description||'').slice(0,120) + '</div>';
                    if (item.catchphrases && item.catchphrases.length) html += '<div style="font-size:12px;color:#484f58;margin-top:2px">口癖: ' + escapeHtml(item.catchphrases.join('、')).slice(0,80) + '</div>';
                } else if (c.key === 'style') {
                    var kindTag = item.kind === 'prefer' ? '<code style="background:#1f6feb">句式风格</code>' : '<code style="background:#a371f7">禁止内容</code>';
                    html += '<div>' + kindTag + ' <strong>' + escapeHtml(item.pattern||'') + '</strong></div>';
                    if (item.desc) html += '<div style="font-size:13px;color:#c9d1d9;margin-top:4px">' + escapeHtml(item.desc).slice(0,120) + '</div>';
                    if (item.replacements && item.replacements.length) html += '<div style="font-size:12px;color:#484f58;margin-top:2px">替换: ' + escapeHtml(item.replacements.join('、')).slice(0,80) + '</div>';
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

// 从 _lastReviewData 取某个分类的原始条目数组
function _reviewItems(key) {
    var d = window._lastReviewData;
    if (!d) return [];
    var c = null;
    for (var i = 0; i < REVIEW_CATS.length; i++) { if (REVIEW_CATS[i].key === key) { c = REVIEW_CATS[i]; break; } }
    if (!c) return [];
    return c.src(d) || [];
}

// 收集勾选的条目
function getCheckedItems() {
    var result = {plots:[], structures:[], gags:[], characters:[], style_rules:[]};
    var cbs = document.querySelectorAll('.review-cb:checked');
    cbs.forEach(function(cb) {
        var key = cb.getAttribute('data-cat');
        var idx = parseInt(cb.getAttribute('data-idx'));
        var c = null;
        for (var i = 0; i < REVIEW_CATS.length; i++) { if (REVIEW_CATS[i].key === key) { c = REVIEW_CATS[i]; break; } }
        if (c) {
            var items = _reviewItems(key);
            if (items[idx]) result[c.field].push(items[idx]);
        }
    });
    return result;
}

// 收集全部条目（"全部入库"用，忽略勾选状态）
function getAllItems() {
    var result = {plots:[], structures:[], gags:[], characters:[], style_rules:[]};
    REVIEW_CATS.forEach(function(c) { result[c.field] = _reviewItems(c.key); });
    return result;
}

async function ingestAll() {
    var items = getAllItems();
    var total = _countItems(items);
    if (total === 0) return showToast('没有可入库的条目', 'warning');
    await doIngest(items);
}

async function ingestSelected() {
    var items = getCheckedItems();
    var total = _countItems(items);
    if (total === 0) return showToast('请勾选要入库的条目', 'warning');
    await doIngest(items);
}

function _countItems(items) {
    return (items.plots||[]).length + (items.structures||[]).length + (items.gags||[]).length
         + (items.characters||[]).length + (items.style_rules||[]).length;
}

async function doIngest(items) {
    var d = window._lastReviewData || {};
    var btn = document.querySelector('.btn-primary');
    var orig = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '入库中...'; }
    try {
        var r = await fetch('/api/scout/ingest', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                title: d.title || '',
                platform: d.platform || 'fanqie',
                folder: d.folder || '',
                profile_id: d.profile_id || '',
                plots: items.plots || [],
                structures: items.structures || [],
                gags: items.gags || [],
                characters: items.characters || [],
                style_rules: items.style_rules || [],
            }),
        });
        var dd = await r.json();
        if (dd.ok) {
            var n = _countItems(items);
            showToast('✅ 已入库 ' + n + ' 条', 'success');
            if (typeof window.refreshScoutUI === 'function') window.refreshScoutUI();
        } else {
            showToast('❌ 入库失败: ' + (dd.error||''), 'error');
        }
    } catch(e) {
        showToast('❌ 入库失败: ' + e.message, 'error');
    }
    if (btn) { btn.disabled = false; btn.textContent = orig; }
}
