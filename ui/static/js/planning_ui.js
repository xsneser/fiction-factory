(function () {
  'use strict';
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function fmt(v) { v = Number(v || 0); return v >= 1000 ? (Math.round(v / 100) / 10) + 'k' : String(v); }
  function textOf(v) {
    if (typeof v === 'string') return v;
    if (!v || typeof v !== 'object') return String(v || '');
    return v.intent || v.question || v.title || v.name || v.goal || v.summary || JSON.stringify(v);
  }
  function list(items, empty) {
    items = Array.isArray(items) ? items : (items ? [items] : []);
    return items.length ? '<ul>' + items.map(function (x) { return '<li>' + esc(textOf(x)) + '</li>'; }).join('') + '</ul>' : '<div class="planning-empty">' + esc(empty) + '</div>';
  }
  function validCharacterIntents(items) {
    return (Array.isArray(items) ? items : []).filter(function (x) {
      return x && typeof x === 'object' && String(x.intent || '').trim();
    });
  }
  function characterIntents(items) {
    items = validCharacterIntents(items);
    if (!items.length) return '<div class="planning-empty">暂无人物意图</div>';
    return '<div class="planning-character-intents">' + items.map(function (x) {
      return '<article><b>' + esc(x.name || x.character || '人物') + '</b><span>当前目标：' + esc(x.intent) + '</span></article>';
    }).join('') + '</div>';
  }
  var panel = document.getElementById('planning-state-panel');
  if (!panel) return;
  var bookId = panel.getAttribute('data-book-id') || '';
  /* 写作台 = compact（Mini Bar + 详情抽屉，信息减法）；书详情 = 完整 6 卡矩阵 */
  var compact = panel.getAttribute('data-compact') === '1';
  var detailOpen = false;
  var current = null;
  var drawer = document.getElementById('replan-drawer');
  var backdrop = document.getElementById('replan-backdrop');
  function openDrawer() { if (drawer) { drawer.classList.add('open'); drawer.setAttribute('aria-hidden', 'false'); } if (backdrop) backdrop.hidden = false; }
  function closeDrawer() { if (drawer) { drawer.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); } if (backdrop) backdrop.hidden = true; }
  function reasonLabel(codes) {
    var labels = {PLOTS_LOW:'剩余可写情节段不足', WORDS_LOW:'承诺字数即将耗尽', PLAN_INVALIDATED:'新事实推翻了原规划', MAJOR_CHARACTER_CHANGE:'人物状态发生重大变化', NEW_HIGH_PRIORITY_QUESTION:'出现高优先级故事问题'};
    return (codes || []).map(function (x) { return labels[x] || x; }).join('、');
  }
  function render(data) {
    current = data;
    var ps = data.planning_state || {}, hz = ps.display_horizon || {}, snap = data.storyline_snapshot || {};
    document.getElementById('planning-revision').textContent = '故事线版本 ' + (snap.revision || 0);
    if (compact) {
      renderCompact(data, ps, hz, snap);
    } else {
      document.getElementById('planning-panel-body').innerHTML =
        '<div class="planning-metrics"><div><b>' + fmt(ps.written_until_word) + '</b><span>已写</span></div><div><b>' + fmt(ps.committed_until_word) + '</b><span>已承诺</span></div><div><b>' + (snap.remaining_plot_count || 0) + '</b><span>可执行情节段</span></div></div>' +
        '<div class="planning-grid"><section><h4>现在执行</h4>' + list(hz.h0, '暂无可执行情节段') + '</section><section><h4>下一段方向</h4>' + list(hz.h1, '尚未形成近期方向') + '</section><section><h4>远期方向</h4>' + list(hz.h2, '远期保持开放') + '</section><section><h4>待解决问题</h4>' + list(ps.story_questions, '暂无待解决问题') + '</section><section><h4>人物当前意图</h4>' + characterIntents(ps.character_intents) + '</section><section><h4>最近续规划</h4>' + list(ps.last_replan && Object.keys(ps.last_replan).length ? [ps.last_replan] : [], '尚未续规划') + '</section></div>';
    }
    renderBoundary(data.boundary || {});
    renderPreview(data.replan_preview);
    window.__PLANNING_STATE__ = ps;
    window.__PLANNING_BOUNDARY__ = data.boundary || {};
    window.dispatchEvent(new CustomEvent('ne:planning-updated', {detail: data}));
  }
  function trunc(v, n) { var s = String(v == null ? '' : v); return s.length > n ? s.slice(0, n) + '…' : s; }
  /* 写作台 compact：默认 Mini Bar 单行（60-90px），点「展开规划」恢复原横向 6 卡矩阵，可折叠 */
  function renderCompact(data, ps, hz, snap) {
    panel.classList.add('compact');
    var body = document.getElementById('planning-panel-body');
    var h0s = hz.h0 || [], h1s = hz.h1 || [], h2s = hz.h2 || [];
    var h0Name = h0s.length ? trunc(textOf(h0s[0]), 14) : '—';
    var h1Txt = h1s.length ? trunc(textOf(h1s[0]), 12) : '未规划';
    var h2Txt = h2s.length ? trunc(textOf(h2s[0]), 12) : '开放';
    var b = data.boundary || {};
    var bCls, bHtml;
    if (b.needs_replan) {
      bCls = (b.remaining_plots || 0) === 0 ? 'crit' : 'warn';
      bHtml = (bCls === 'crit' ? '🔴' : '🟡') + ' ' + trunc(reasonLabel(b.reason_codes), 16);
    } else {
      bCls = 'ok'; bHtml = '🟢 规划充足';
    }
    var alerts = '';
    if ((ps.story_questions || []).length) alerts += '<span class="pm-alert">🟡 ' + (ps.story_questions || []).length + ' 个开放问题</span>';
    var visibleIntents = validCharacterIntents(ps.character_intents);
    if (visibleIntents.length) alerts += '<span class="pm-alert">👥 ' + visibleIntents.length + ' 个人物意图</span>';
    var lr = (ps.last_replan && Object.keys(ps.last_replan).length) ? trunc(textOf(ps.last_replan), 12) : '';
    if (lr) alerts += '<span class="pm-alert">🔄 ' + lr + '</span>';
    body.innerHTML =
      '<div class="planning-minibar">'
      + '<span class="pm-chip">版本 ' + esc(snap.revision || 0) + '</span>'
      + '<span class="pm-sep"></span>'
      + '<span class="pm-words"><b>' + fmt(ps.written_until_word) + '</b> 已写 / <b>' + fmt(ps.committed_until_word) + '</b> 已承诺</span>'
      + '<span class="pm-sep"></span>'
      + '<span class="pm-rem">剩 ' + (snap.remaining_plot_count || 0) + ' 个情节段</span>'
      + '<span class="pm-sep"></span>'
      + '<span class="pm-h0">当前执行：' + esc(h0Name) + '</span>'
      + '<span class="pm-sep"></span>'
      + '<span class="pm-h1">下一段：' + esc(h1Txt) + '</span>'
      + '<span class="pm-sep"></span>'
      + '<span class="pm-h2">远期：' + esc(h2Txt) + '</span>'
      + '<span class="pm-boundary ' + bCls + '">' + bHtml + '</span>'
      + alerts
      + '<button type="button" class="pm-detail-btn" data-planning-toggle>' + (detailOpen ? '收起 ▴' : '展开规划 ▾') + '</button>'
      + '</div>'
      + (detailOpen
        ? '<div class="planning-cards">'
          + '<div class="planning-metrics"><div><b>' + fmt(ps.written_until_word) + '</b><span>已写</span></div><div><b>' + fmt(ps.committed_until_word) + '</b><span>已承诺</span></div><div><b>' + (snap.remaining_plot_count || 0) + '</b><span>可执行情节段</span></div></div>'
          + '<div class="planning-grid"><section><h4>现在执行</h4>' + list(hz.h0, '暂无可执行情节段') + '</section><section><h4>下一段方向</h4>' + list(hz.h1, '尚未形成近期方向') + '</section><section><h4>远期方向</h4>' + list(hz.h2, '远期保持开放') + '</section><section><h4>待解决问题</h4>' + list(ps.story_questions, '暂无待解决问题') + '</section><section><h4>人物当前意图</h4>' + characterIntents(ps.character_intents) + '</section><section><h4>最近续规划</h4>' + list(ps.last_replan && Object.keys(ps.last_replan).length ? [ps.last_replan] : [], '尚未续规划') + '</section></div>'
          + '</div>'
        : '');
    var tgl = body.querySelector('[data-planning-toggle]');
    if (tgl) tgl.onclick = function () { detailOpen = !detailOpen; renderCompact(data, ps, hz, snap); };
  }
  function renderBoundary(b) {
    var el = document.getElementById('boundary-banner'); if (!el) return;
    if (compact) { el.hidden = true; return; }   // 写作台：边界状态已并入 Mini Bar，横幅让位
    if (!b.needs_replan) { el.hidden = true; return; }
    el.hidden = false;
    el.className = 'boundary-banner ' + ((b.remaining_plots || 0) === 0 ? 'critical' : 'warning');
    el.innerHTML = '<div><strong>⚠️ 临近规划边界</strong><span>' + esc(reasonLabel(b.reason_codes)) + ' · 剩余 ' + (b.remaining_plots || 0) + ' 个情节段 / ' + fmt(b.remaining_words) + ' 字</span></div><button type="button" class="small" id="boundary-replan-btn">规划下一段</button>';
    document.getElementById('boundary-replan-btn').onclick = requestReplan;
  }
  function renderPreview(p) {
    var body = document.getElementById('replan-body'); if (!body) return;
    if (!p) { body.innerHTML = '<div class="empty">尚无规划预览</div>'; return; }
    var diag = p.diagnosis || {}, dirs = p.directions || [], validation = p.validation || {};
    body.innerHTML = '<section class="replan-section"><h4>当前诊断</h4>' + list([diag.current_pressure || diag.main_tension, diag.reader_question, diag.urgent_problem].filter(Boolean), '尚未提供诊断') + '</section>' +
      '<section class="replan-section"><h4>候选方向</h4><div class="replan-directions">' + dirs.map(function (d) { var active = String(d.id) === String(p.selected_direction_id); return '<button type="button" class="replan-direction ' + (active ? 'active' : '') + '" data-direction="' + esc(d.id) + '"><b>' + esc(d.title) + '</b><span>' + esc(d.reason || d.summary || '') + '</span></button>'; }).join('') + '</div></section>' +
      '<section class="replan-section"><h4>待提交情节段</h4>' + list(p.plots || [], '没有可执行情节段') + '</section>' +
      (!validation.passed ? '<div class="replan-errors">' + list(validation.problems, '预览校验未通过') + '</div>' : '') +
      '<div class="replan-actions"><button type="button" class="small" id="replan-redraft">重拟</button><button type="button" class="small danger" id="replan-discard">丢弃</button><button type="button" class="btn" id="replan-commit" ' + (validation.passed ? '' : 'disabled') + '>确认并提交</button></div>';
    body.querySelectorAll('[data-direction]').forEach(function (el) { el.onclick = function () { if (String(el.dataset.direction) !== String(p.selected_direction_id)) requestReplan(el.dataset.direction); }; });
    document.getElementById('replan-redraft').onclick = function () { requestReplan(p.selected_direction_id, true); };
    document.getElementById('replan-discard').onclick = function () { fetch('/api/storyline/' + encodeURIComponent(bookId) + '/replan-preview/' + encodeURIComponent(p.preview_id), {method:'DELETE'}).then(refresh); closeDrawer(); };
    document.getElementById('replan-commit').onclick = function () { commitPreview(p); };
  }
  function requestReplan(direction, redraw) {
    openDrawer();
    var rev = current && current.storyline_snapshot ? current.storyline_snapshot.revision : 0;
    var suffix = direction ? '用户选择方向 id=' + direction + '，请只为这个方向生成完整可执行预览。' : '请比较 2-3 个方向并选择推荐方向。';
    if (redraw) suffix += '这是重拟请求，请换一种具体方案。';
    var task = '请为 book ' + bookId + '生成增量续规划预览，不要提交正式故事线。先 get_story_state，再生成下一批 3-8 个可写情节段；最后必须调用 drive_ui(set_replan_preview)，expected_revision=' + rev + '。' + suffix;
    if (window.switchAgentTab) window.switchAgentTab('chat');
    if (window.agentSendTask) window.agentSendTask(task, {card:true, cardLabel:'🔭 续规划预览'});
  }
  function showConflict(p) {
    var modal = document.getElementById('revision-modal'); if (!modal) return;
    document.getElementById('revision-message').textContent = '预览基于版本 ' + p.expected + '，当前故事线已经是版本 ' + p.actual + '，系统没有覆盖新内容。';
    modal.hidden = false;
  }
  function commitPreview(p) {
    var btn = document.getElementById('replan-commit'); if (btn) btn.disabled = true;
    fetch('/api/storyline/' + encodeURIComponent(bookId) + '/commit-plan', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({preview_id:p.preview_id, expected_revision:p.expected_revision})})
      .then(function (r) { return r.json(); }).then(function (d) { if (d.error === 'stale_storyline') { showConflict(d); return; } if (!d.ok) throw new Error(d.message || d.error || '提交失败'); closeDrawer(); render(d); if (window.showToast) showToast('规划已提交', 'success'); })
      .catch(function (e) { if (window.showToast) showToast(e.message, 'error'); }).finally(function () { if (btn) btn.disabled = false; });
  }
  function refresh() { return fetch('/api/storyline/' + encodeURIComponent(bookId) + '/planning-state').then(function (r) { return r.json(); }).then(function (d) { if (d.ok) render(d); }); }
  panel.addEventListener('click', function () { if (current && current.replan_preview) openDrawer(); });
  if (backdrop) backdrop.onclick = closeDrawer;
  document.querySelectorAll('[data-replan-close]').forEach(function (x) { x.onclick = closeDrawer; });
  var cancel = document.querySelector('[data-revision-cancel]'); if (cancel) cancel.onclick = function () { document.getElementById('revision-modal').hidden = true; };
  var retry = document.querySelector('[data-revision-refresh]'); if (retry) retry.onclick = function () { document.getElementById('revision-modal').hidden = true; refresh().then(function () { requestReplan('', true); }); };
  window.__nePageReceiver = true;
  var previousReceiver = window.onnecommand;
  window.onnecommand = function (e) { var d = e.detail || {}; if (d.cmd === 'set_replan_preview' && (!d.args.book_id || d.args.book_id === bookId)) { refresh().then(openDrawer); return; } if (typeof previousReceiver === 'function') previousReceiver(e); };
  window.NEPlanning = {refresh:refresh, open:openDrawer, requestReplan:requestReplan};
  refresh();
})();
