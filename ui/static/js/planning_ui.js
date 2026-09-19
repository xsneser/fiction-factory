(function () {
  'use strict';
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function fmt(v) { v = Number(v || 0); return v >= 1000 ? (Math.round(v / 100) / 10) + 'k' : String(v); }
  function textOf(v) {
    if (typeof v === 'string') return v;
    if (!v || typeof v !== 'object') return String(v || '');
    return v.intent || v.question || v.title || v.name || v.goal || v.summary || JSON.stringify(v);
  }
  function validCharacterIntents(items) {
    return (Array.isArray(items) ? items : []).filter(function (x) {
      return x && typeof x === 'object' && String(x.intent || '').trim();
    });
  }
  function openStoryQuestions(items) {
    var terminal = {answered: true, superseded: true, resolved: true, closed: true};
    return (Array.isArray(items) ? items : []).filter(function (x) {
      if (!x || (typeof x !== 'string' && typeof x !== 'object')) return false;
      var status = typeof x === 'object' ? String(x.status || '').toLowerCase() : '';
      var text = typeof x === 'string' ? x : (x.question || x.title || x.text || x.summary || '');
      return !terminal[status] && String(text).trim();
    });
  }
  /* 顶部两栏里有没有「本段待写」——决定规划横幅怎么说话（H0 与本段标题不同时显示）。
     信号由写作台模板的 renderLatestContext() 维护（旧的 __NE_COMPARE_HAS_CURRENT__ 已随三列
     对照区一起删除，别再用那个名字）。 */
  function hasCurrentPlotContext() {
    var box = document.getElementById('wf-latest-context');
    return !!(box && !box.hidden && window.__NE_LATEST_HAS_CURRENT__);
  }
  var panel = document.getElementById('planning-state-panel');
  if (!panel) return;
  var bookId = panel.getAttribute('data-book-id') || '';
  /* 规划 UI 只剩写作台的提示条这一种形态：书详情那份完整四卡矩阵已删（agent 决策视图，
     人不看）。这里只观察规划，不发起规划任务。 */
  var latestData = null;
  function reasonLabel(codes) {
    var labels = {
      PLOTS_LOW: '剩余可写情节段不足',
      WORDS_LOW: '承诺字数即将耗尽',
      PLAN_INVALIDATED: '新事实推翻了原规划',
      MAJOR_CHARACTER_CHANGE: '人物状态发生重大变化',
      NEW_HIGH_PRIORITY_QUESTION: '出现高优先级故事问题'
    };
    codes = Array.isArray(codes) ? codes : [];
    var text = codes.map(function (x) { return labels[x] || x; }).filter(Boolean).join('、');
    return text || '规划余量不足';
  }
  function storylineRevision(sl) {
    return Number(sl && (sl.storyline_revision != null ? sl.storyline_revision : sl.revision) || 0);
  }
  var storylineSyncInFlight = null;
  var storylineSyncTarget = 0;
  function mergeStorylineRuntimeFields(next) {
    var old = window.__BOOK_STORYLINE__ || {};
    var oldPlots = {};
    (old.plots || []).forEach(function (p) { if (p && p.id) oldPlots[String(p.id)] = p; });
    (next.plots || []).forEach(function (p) {
      var oldPlot = p && p.id ? oldPlots[String(p.id)] : null;
      if (oldPlot && oldPlot.actual_words != null && p.actual_words == null) p.actual_words = oldPlot.actual_words;
    });
    return next;
  }
  function syncStoryline(targetRevision) {
    storylineSyncTarget = Math.max(storylineSyncTarget, Number(targetRevision || 0));
    var currentRevision = storylineRevision(window.__BOOK_STORYLINE__);
    if (!bookId || (window.__BOOK_STORYLINE__ && currentRevision >= storylineSyncTarget)) return Promise.resolve(false);
    if (storylineSyncInFlight) return storylineSyncInFlight;
    var requestedRevision = storylineSyncTarget;
    storylineSyncInFlight = fetch('/api/storyline/' + encodeURIComponent(bookId))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || !d.storyline) return false;
        var incoming = d.storyline;
        var incomingRevision = storylineRevision(incoming);
        var latestRevision = storylineRevision(window.__BOOK_STORYLINE__);
        if (incomingRevision < latestRevision || incomingRevision < requestedRevision) return false;
        incoming = mergeStorylineRuntimeFields(incoming);
        window.__BOOK_STORYLINE__ = incoming;
        window.dispatchEvent(new CustomEvent('ne:storyline-updated', {detail: {
          storyline: incoming, revision: incomingRevision
        }}));
        return true;
      })
      .catch(function () { return false; })
      .finally(function () {
        storylineSyncInFlight = null;
      });
    return storylineSyncInFlight;
  }
  function render(data) {
    data = data || {};
    latestData = data;
    var ps = data.planning_state || {}, hz = ps.display_horizon || {}, snap = data.storyline_snapshot || {};
    /* 先发布权威状态，再渲染 DOM；面板小故障不能阻断故事线/写作台刷新。 */
    window.__PLANNING_STATE__ = ps;
    window.__PLANNING_BOUNDARY__ = data.boundary || {};
    if (snap.revision != null) syncStoryline(snap.revision);
    try {
      document.getElementById('planning-revision').textContent = '故事线版本 ' + (snap.revision || 0);
      renderChecklist(ps.checklist);
      /* 只有写作台的紧凑提示条这一种形态：完整四卡规划矩阵已从书详情移除
         （agent 决策视图，人不看）。 */
      renderCompact(data, ps, hz, snap);
    } catch (err) {
      var body = document.getElementById('planning-panel-body');
      if (body) body.innerHTML = '<div class="planning-error">规划面板渲染失败，状态仍已同步；请刷新页面重试。</div>';
      if (window.console && console.error) console.error('规划面板渲染失败', err);
    } finally {
      window.dispatchEvent(new CustomEvent('ne:planning-updated', {detail: data}));
    }
  }
  /* 待填清单徽标：与"故事线版本"并排，一眼看到"还差什么没填"。
     阶段（流程走到哪）与清单（东西填得怎么样）刻意分开表述。 */
  function renderChecklist(cl) {
    var head = document.querySelector('.planning-panel-head');
    if (!head) return;
    var el = document.getElementById('planning-checklist');
    if (!el) {
      el = document.createElement('span');
      el.id = 'planning-checklist';
      el.className = 'hint';
      el.style.marginLeft = '8px';
      head.appendChild(el);
    }
    if (!cl || !cl.items) { el.textContent = ''; return; }
    var pending = (cl.pending || []).length;
    var blocking = (cl.gates && cl.gates.blocking) || [];
    el.textContent = cl.summary || ('待填 ' + pending + ' 项');
    el.style.color = blocking.length ? 'var(--danger, #f85149)'
      : (pending ? 'var(--warn, #d29922)' : 'var(--fg-dim)');
    if ((cl.summary || '').indexOf('硬门禁未过') >= 0) {
      el.textContent += '（' + blocking.join('、') + '）';
    }
  }

  function trunc(v, n) { var s = String(v == null ? '' : v); return s.length > n ? s.slice(0, n) + '…' : s; }
  /* 只保留会影响当前写作决策的提示（边界告警 + 规划门禁 + 少量方向/问题/人物意图）。 */
  function renderCompact(data, ps, hz, snap) {
    panel.classList.add('compact');
    panel.hidden = false;
    var body = document.getElementById('planning-panel-body');
    if (!body) return;
    var h0s = hz.h0 || [];
    var h0Name = h0s.length ? trunc(textOf(h0s[0]), 14) : '';
    var b = data.boundary || {};
    var alerts = '';
    var questions = openStoryQuestions(ps.story_questions);
    var visibleIntents = validCharacterIntents(ps.character_intents);
    var checklist = ps.checklist || {};
    var blocking = Array.isArray(checklist.gates && checklist.gates.blocking)
      ? checklist.gates.blocking : [];

    /* 无边界时不占一块醒目的常驻状态；只有真的需要续规划时才提示原因和余量。 */
    if (b.needs_replan) {
      var bCls = (b.remaining_plots || 0) === 0 ? 'crit' : 'warn';
      var remaining = [];
      if (b.remaining_plots != null) remaining.push('剩 ' + b.remaining_plots + ' 段');
      if (b.remaining_words != null) remaining.push(fmt(b.remaining_words) + ' 字余量');
      alerts += '<span class="pm-boundary ' + bCls + '">' +
        (bCls === 'crit' ? '🔴' : '🟡') + ' ' + esc(reasonLabel(b.reason_codes)) +
        (remaining.length ? ' · ' + esc(remaining.join(' · ')) : '') + '</span>';
    }
    if (questions.length) alerts += '<span class="pm-alert">🟡 ' + questions.length + ' 个待解问题</span>';
    if (visibleIntents.length) alerts += '<span class="pm-alert">👥 ' + visibleIntents.length + ' 个人物意图</span>';
    if (blocking.length) alerts += '<span class="pm-alert pm-alert-danger">⛔ ' + blocking.length + ' 项规划门禁未过</span>';
    else if (Array.isArray(checklist.pending) && checklist.pending.length) {
      alerts += '<span class="pm-alert">📝 待填 ' + checklist.pending.length + ' 项</span>';
    }

    /* 对照区已有本段时会直接显示当前 Plot；只在它没有可用当前段时保留 H0 兜底。 */
    var currentHint = !hasCurrentPlotContext() && h0Name
      ? '<span class="pm-h0">下一段：' + esc(h0Name) + '</span>' : '';
    if (!currentHint && !alerts) {
      panel.hidden = true;
      body.innerHTML = '';
      return;
    }
    body.innerHTML =
      '<div class="planning-minibar" role="status" aria-label="写作提示">'
      + currentHint
      + alerts
      + '</div>';
  }
  function refresh() { return fetch('/api/storyline/' + encodeURIComponent(bookId) + '/planning-state').then(function (r) { return r.json(); }).then(function (d) { if (d.ok) render(d); }); }
  /* Planner 仍会通过 drive_ui 暂存预览；自动写作路径只刷新状态，不再打开人工抽屉。 */
  window.__nePageReceiver = true;
  var previousReceiver = window.onnecommand;
  window.onnecommand = function (e) {
    var d = e.detail || {};
    if (d.cmd === 'set_replan_preview' && (!d.args || !d.args.book_id || d.args.book_id === bookId)) {
      refresh();
      return;
    }
    if (typeof previousReceiver === 'function') previousReceiver(e);
  };
  /* 顶部两栏异步就绪时重绘提示条，避免首屏竞态让 H0 与本段标题同时显示。 */
  window.addEventListener('ne:current-context-updated', function () {
    if (!latestData) return;
    var ps = latestData.planning_state || {};
    renderCompact(latestData, ps, ps.display_horizon || {}, latestData.storyline_snapshot || {});
  });
  window.NEPlanning = {refresh:refresh, syncStoryline:syncStoryline};
  refresh();
})();
