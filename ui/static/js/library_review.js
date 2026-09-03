// ═══════════════════════════════════════════════════
// 库审查卡片 + 入库 公共逻辑（仅 extract.html 提取工作台使用）
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

// 数组字段防御：LLM 可能把单值发成字符串/对象，join 前必须 Array.isArray 守卫（否则 .join 抛异常）
function _arr(v) { return Array.isArray(v) ? v : []; }
function _join(v, sep) { var a = _arr(v); return a.length ? a.join(sep) : ''; }

// 渲染审查卡片到指定区域（areaId）。防御式：任何单项渲染失败只降级该卡，不中断整批。
function renderReviewCards(d, areaId) {
    var area = document.getElementById(areaId);
    if (!area) return;

    // 书身份（书名/章数/笔名）已在提取页上方工作台展示，这里只放入库工具条，避免重复
    var html = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">';
    html += '<button class="btn-primary" onclick="ingestAll()">📦 全部入库</button>';
    html += '<button class="btn" style="background:#30363d;color:#f0f6fc" onclick="ingestSelected()">✅ 入库选中</button>';
    html += '<span style="font-size:12px;color:#8b949e">勾选下方条目后入库（AI 只负责放入暂存区，入库由你手动确认）</span>';
    html += '</div>';

    html += '<div class="tabs" style="margin-bottom:12px">';
    REVIEW_CATS.forEach(function(c, i) {
        var items = c.src(d) || [];
        html += '<a href="javascript:;" class="review-tab ' + (i===0?'active':'') + '" data-tab="' + c.key + '" onclick="switchReviewTab(\'' + c.key + '\')">' + c.label + ' (' + items.length + ')</a>';
    });
    html += '</div>';

    // 复用角色原型库卡片网格样式（char-grid / char-card）
    REVIEW_CATS.forEach(function(c) {
        var items = c.src(d) || [];
        html += '<div class="review-panel" id="review-' + c.key + '"' + (c.key!=='plot'?' style="display:none"':'') + '>';
        if (items.length === 0) {
            html += '<div class="empty" style="padding:30px"><p style="color:#484f58">无提取结果</p></div>';
        } else {
            html += '<div class="char-grid review-grid">';
            items.forEach(function(item, idx) {
                var itemHtml = '';
                try {
                    itemHtml += '<label class="char-card review-card">';
                    itemHtml += '<input type="checkbox" class="review-cb" data-cat="' + c.key + '" data-idx="' + idx + '" checked>';
                    if (c.key === 'plot') {
                        itemHtml += '<span class="cc-head"><span class="cc-title"><code class="tag blue">' + escapeHtml(item.category||'桥段') + '</code> <strong>' + escapeHtml(item.name||'') + '</strong></span></span>';
                        itemHtml += '<div class="rc-desc">' + escapeHtml(item.description||'') + '</div>';
                        if (item.structure) itemHtml += '<div class="rc-meta">结构: ' + escapeHtml(item.structure) + '</div>';
                    } else if (c.key === 'structure') {
                        itemHtml += '<span class="cc-head"><span class="cc-title"><code class="tag blue">情节弧</code> <strong>' + escapeHtml(item.name||'') + '</strong></span></span>';
                        itemHtml += '<div class="rc-desc">' + escapeHtml(item.description||'') + '</div>';
                        // stages 是对象数组（StageNode），逐层取 name 而非整对象 join（防 [object Object]）
                        var stageArr = _arr(item.stages);
                        if (stageArr.length) {
                            var stageNames = stageArr.map(function(s){ return (s && s.name) ? s.name : String(s); });
                            itemHtml += '<div class="rc-meta">阶段: ' + escapeHtml(stageNames.join(' → ')) + '</div>';
                        }
                    } else if (c.key === 'gag') {
                        itemHtml += '<span class="cc-head"><span class="cc-title"><code class="tag blue">' + escapeHtml(item.category||'笑点') + '</code> <strong>' + escapeHtml(item.name||'') + '</strong></span></span>';
                        itemHtml += '<div class="rc-desc">' + escapeHtml(item.pattern_description||item.description||'') + '</div>';
                    } else if (c.key === 'character') {
                        var arch = _join(item.archetypes, ' / ');
                        var tag0 = _arr(item.tags)[0] || '';
                        itemHtml += '<span class="cc-head"><span class="cc-title"><code class="tag blue">' + escapeHtml(arch || tag0 || '角色') + '</code> <strong>' + escapeHtml(item.name||'') + '</strong></span></span>';
                        itemHtml += '<div class="rc-desc">' + escapeHtml(item.personality||item.description||'') + '</div>';
                        var cps = _join(item.catchphrases, '、');
                        if (cps) itemHtml += '<div class="rc-meta">口癖: ' + escapeHtml(cps) + '</div>';
                    } else if (c.key === 'style') {
                        var kindTag = item.kind === 'prefer' ? '<code class="tag" style="background:#1f6feb">句式风格</code>' : '<code class="tag" style="background:#a371f7">禁止内容</code>';
                        itemHtml += '<span class="cc-head"><span class="cc-title">' + kindTag + ' <strong>' + escapeHtml(item.pattern||'') + '</strong></span></span>';
                        if (item.desc) itemHtml += '<div class="rc-desc">' + escapeHtml(item.desc) + '</div>';
                        var reps = _join(item.replacements, '、');
                        if (reps) itemHtml += '<div class="rc-meta">替换: ' + escapeHtml(reps) + '</div>';
                    }
                    itemHtml += '</label>';
                } catch (err) {
                    if (window.console) console.error('审查卡渲染失败', c.key, item, err);
                    itemHtml = '<label class="char-card review-card"><span class="cc-head"><span class="cc-title"><code class="tag" style="background:#a371f7">解析失败</code>'
                        + (item && item.name ? '<strong>' + escapeHtml(String(item.name)) + '</strong>' : '') + '</span></span>'
                        + '<div class="rc-desc" style="color:#d29922">⚠️ 该项解析失败</div></label>';
                }
                html += itemHtml;
            });
            html += '</div>';
        }
        html += '</div>';
    });

    area.style.display = 'block';
    area.innerHTML = html;
    // 渲染成功后才记为已渲染：抛错时 _lastReviewData 保持 null，pending 轮询可重试
    window._lastReviewData = d;
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
            // 入库成功：分析信息不残留，清空审查区并复位状态
            window._lastReviewData = null;
            var ra = document.getElementById('review-area');
            if (ra) { ra.style.display = 'none'; ra.innerHTML = ''; }
        } else {
            showToast('❌ 入库失败: ' + (dd.error||''), 'error');
        }
    } catch(e) {
        showToast('❌ 入库失败: ' + e.message, 'error');
    }
    if (btn) { btn.disabled = false; btn.textContent = orig; }
}
