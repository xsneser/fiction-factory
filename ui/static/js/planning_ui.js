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
  var panel = document.getElementById('planning-state-panel');
  if (!panel) return;
  var bookId = panel.getAttribute('data-book-id') || '';
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
    document.getElementById('planning-revision').textContent = 'Revision ' + (snap.revision || 0);
    document.getElementById('planning-panel-body').innerHTML =
      '<div class="planning-metrics"><div><b>' + fmt(ps.written_until_word) + '</b><span>已写</span></div><div><b>' + fmt(ps.committed_until_word) + '</b><span>已承诺</span></div><div><b>' + (snap.remaining_plot_count || 0) + '</b><span>可执行 plots</span></div></div>' +
      '<div class="planning-grid"><section><h4>H0 · 现在执行</h4>' + list(hz.h0, '暂无可执行情节段') + '</section><section><h4>H1 · 下一段方向</h4>' + list(hz.h1, '尚未形成近期方向') + '</section><section><h4>H2 · 远期意图</h4>' + list(hz.h2, '远期保持开放') + '</section><section><h4>开放问题</h4>' + list(ps.story_questions, '暂无开放问题') + '</section><section><h4>人物意图</h4>' + list(ps.character_intents, '暂无人物意图') + '</section><section><h4>最近续规划</h4>' + list(ps.last_replan && Object.keys(ps.last_replan).length ? [ps.last_replan] : [], '尚未续规划') + '</section></div>';
    renderBoundary(data.boundary || {});
    renderPreview(data.replan_preview);
    window.__PLANNING_STATE__ = ps;
    window.__PLANNING_BOUNDARY__ = data.boundary || {};
    window.dispatchEvent(new CustomEvent('ne:planning-updated', {detail: data}));
  }
  function renderBoundary(b) {
    var el = document.getElementById('boundary-banner'); if (!el) return;
    if (!b.needs_replan) { el.hidden = true; return; }
    el.hidden = false;
    el.className = 'boundary-banner ' + ((b.remaining_plots || 0) === 0 ? 'critical' : 'warning');
    el.innerHTML = '<div><strong>⚠️ 临近规划边界</strong><span>' + esc(reasonLabel(b.reason_codes)) + ' · 剩余 ' + (b.remaining_plots || 0) + ' plots / ' + fmt(b.remaining_words) + ' 字</span></div><button type="button" class="small" id="boundary-replan-btn">让 Agent 规划下一段</button>';
    document.getElementById('boundary-replan-btn').onclick = requestReplan;
  }
  function renderPreview(p) {
    var body = document.getElementById('replan-body'); if (!body) return;
    if (!p) { body.innerHTML = '<div class="empty">尚无规划预览</div>'; return; }
    var diag = p.diagnosis || {}, dirs = p.directions || [], validation = p.validation || {};
    body.innerHTML = '<section class="replan-section"><h4>当前诊断</h4>' + list([diag.current_pressure || diag.main_tension, diag.reader_question, diag.urgent_problem].filter(Boolean), 'Agent 未提供诊断') + '</section>' +
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
