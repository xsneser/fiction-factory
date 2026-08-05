/*
 * 故事线（Story Line）组件 — 垂直 Gantt
 * 从 BookStoryline dict 渲染：章节轴 + 大纲/桥段/线程通道。
 * 支持叙事手法视觉区分：顺叙(chronological)/倒叙(flashback)/插叙(interleaved)。
 *
 * 用法：StoryLine.init('mount-id', bookStorylineDict, {currentChapter: N})
 */
(function () {
  'use strict';

  var TOTAL_WORDS = 0;
  var WPC = 3000;
  var chapters = [], outlines = [], plots = [], threads = [];
  var promises = [], promiseByPlot = {};   // 读者承诺台账：桥段id → [{kind:setup/payoff, pr}]
  var PALETTE = ['#f97583', '#79c0ff', '#56d364', '#e3b341', '#d2a8ff', '#ffa657', '#c084fc', '#7ee787'];
  var THREAD_PALETTE = ['#ffa657', '#79c0ff', '#d2a8ff', '#56d364', '#e3b341', '#ff7b72', '#7ee787'];

  var _lastRender = null;
  var _lastMountId = null;
  if (!window._sl_resize_bound) {
    window.addEventListener('resize', function () { if (_lastRender) _lastRender(); });
    window._sl_resize_bound = true;
  }

  /* ─── 纵向缩放：调整内容高度 → 百分比映射更多像素 → 条间距更大/更紧凑 ─── */
  var _zoom = 1;            // 缩放倍率（0.4x ~ 4x，步进 0.25）
  var _baseScrollH = 720;   // scrollable 模式下未缩放的基准内容高度
  var _panels = [];         // 需随缩放改高度的三个面板（章节/轴/内容区）
  var _scrollableMode = false;

  function zoomHeight() {
    return Math.max(300, Math.min(8000, Math.round((_baseScrollH || 720) * _zoom)));
  }

  function wordToPercent(w) {
    return (TOTAL_WORDS > 0) ? (w / TOTAL_WORDS) * 100 : 0;
  }

  /* 桥段预计字数 = cover_beats × 200，封顶 1200（与后端 storyline_writer.planned_words 同一公式） */
  function plannedWords(p) {
    var beats = Math.max(parseInt((p && p.cover_beats) || 0, 10) || 0, 2);
    return Math.min(beats * 200, 1200);
  }

  /* 线程 id → 颜色 */
  function threadColor(tid) {
    for (var i = 0; i < threads.length; i++) { if (threads[i].id === tid) return threads[i].color; }
    return '#ffa657';
  }

  /* ─── 数据适配：BookStoryline → 平铺数组（桥段按真实规划字数定位，预计=实际） ─── */
  function adapt(bt) {
    bt = bt || {};
    WPC = bt.words_per_chapter || 3000;

    // 桥段按所属大纲分组
    var byOutline = {};
    (bt.plots || []).forEach(function (p) {
      var key = p.outline_id || '';
      (byOutline[key] = byOutline[key] || []).push(p);
    });
    function sortPlots(a, b) {
      return ((a.stage_index || 0) - (b.stage_index || 0)) || ((a.order || 0) - (b.order || 0));
    }

    // 大纲 → 按顺序纵向排列，宽度=其桥段规划字数总和（不再按章节范围均分/假大空）
    outlines = (bt.outlines || []).map(function (o, i) {
      var ow = 0;
      (byOutline[o.id] || []).slice().sort(sortPlots).forEach(function (p) { ow += plannedWords(p); });
      return {
        id: o.id, name: o.name, ow: ow,
        color: PALETTE[i % PALETTE.length],
        narrative: o.narrative || 'chronological',
        narrative_target: o.narrative_target || '',
      };
    });
    var cursor = 0;
    outlines.forEach(function (o) {
      o.start = cursor;
      o.end = cursor + Math.max(o.ow, WPC);   // 无桥段的大纲至少占一章宽度
      cursor = o.end + WPC * 0.5;             // 弧间留半章空隙
    });
    TOTAL_WORDS = Math.max(cursor - WPC * 0.5, WPC);
    var totalCh = Math.max(1, Math.round(TOTAL_WORDS / WPC));

    // 章节轴：每章一条虚线+圆点（不再抽稀跳号），标签按空间自动省略
    chapters = [];
    for (var n = 1; n <= totalCh; n++) {
      chapters.push({ num: n, words: n * WPC });
    }
    if (!chapters.length) chapters.push({ num: 1, words: WPC });

    // 桥段 → 在大纲内按规划字数累计定位（首桥段 0—~1200字，而非 0—13517）
    plots = [];
    var outlineById = {};
    outlines.forEach(function (o) { outlineById[o.id] = o; });
    var outlineColorById = {};
    outlines.forEach(function (o) { outlineColorById[o.id] = o.color; });
    Object.keys(byOutline).forEach(function (key) {
      var list = byOutline[key].slice().sort(sortPlots);
      var o = outlineById[key];
      if (!o) return;
      var rootColor = outlineColorById[key] || '#79c0ff';
      var cum = 0;
      list.forEach(function (p) {
        var pw = plannedWords(p);
        plots.push({
          id: p.id, name: p.name,
          oid: p.outline_id || '',
          start: o.start + cum,
          end: o.start + cum + pw,
          parent: p.parent_plot_id || null,
          color: p.parent_plot_id ? '#a5d6ff' : rootColor,
          category: p.category || '',
          thread: p.thread_id || '主线',
          resolves: p.resolves_plot_id || '',
          resolves_name: p.resolves_name || '',
          roles: p.roles || [],
        });
        cum += pw;
      });
    });

    // 叙事线程 → 横带区间（书级 threads 定义 + 桥段 thread_id 推导，多线重叠=穿插可视）
    threads = [];
    var tIdx = {};
    (bt.threads || []).forEach(function (t) {
      tIdx[t.id] = threads.length;
      threads.push({ id: t.id, name: t.name || t.id, desc: t.desc || '', start: Infinity, end: -Infinity, color: THREAD_PALETTE[threads.length % THREAD_PALETTE.length] });
    });
    plots.forEach(function (fp) {
      var tid = fp.thread || '主线';
      if (!(tid in tIdx)) {
        tIdx[tid] = threads.length;
        threads.push({ id: tid, name: tid, desc: '', start: Infinity, end: -Infinity, color: THREAD_PALETTE[threads.length % THREAD_PALETTE.length] });
      }
      var idx = tIdx[tid];
      if (fp.start < threads[idx].start) threads[idx].start = fp.start;
      if (fp.end > threads[idx].end) threads[idx].end = fp.end;
    });
    threads.forEach(function (t) {
      if (t.start === Infinity) { t.start = 0; t.end = Math.max(t.end, WPC); }
    });

    // 读者承诺台账：设局桥段→⏳待兑现，收局桥段→✅已兑现；映射到对应桥段条
    promises = (bt.promises || []);
    promiseByPlot = {};
    promises.forEach(function (pr) {
      if (pr.setup_plot_id) {
        (promiseByPlot[pr.setup_plot_id] = promiseByPlot[pr.setup_plot_id] || []).push({ kind: 'setup', pr: pr });
      }
      if (pr.payoff_plot_id) {
        (promiseByPlot[pr.payoff_plot_id] = promiseByPlot[pr.payoff_plot_id] || []).push({ kind: 'payoff', pr: pr });
      }
    });
  }

  /* ─── 通道打包 ─── */
  function assignLanes(items) {
    var sorted = items.map(function (it, i) { return { it: it, i: i }; })
      .sort(function (a, b) { return a.it.start - b.it.start || b.it.end - a.it.end; });
    var lanes = [], assignments = new Array(items.length);
    sorted.forEach(function (item) {
      var lane = 0;
      while (lane < lanes.length && lanes[lane] > item.it.start) lane++;
      lanes[lane] = item.it.end;
      assignments[item.i] = lane;
    });
    return { assignments: assignments, totalLanes: lanes.length };
  }

  /* ─── 渲染：章节 + 轴 ─── */
  function renderChapters(chapterPanel, axisPanel) {
    chapterPanel.innerHTML = '';
    axisPanel.innerHTML = '<div class="sl-axis-line"></div>';
    for (var w = 0; w <= TOTAL_WORDS; w += 1000) {
      var yPct = wordToPercent(w);
      var tick = document.createElement('div');
      tick.className = 'sl-tick'; tick.style.top = yPct + '%';
      axisPanel.appendChild(tick);
      var label = document.createElement('div');
      label.className = 'sl-tick-label'; label.style.top = yPct + '%';
      label.textContent = (w / 1000) + 'k';
      axisPanel.appendChild(label);
    }
    // 每章一条虚线（连续，不再 1→9→17 跳号）；"第N章"标签按间距自动省略以免重叠
    var h = chapterPanel.clientHeight || 400;
    var minLabelGap = Math.max(1.5, (18 / Math.max(h, 120)) * 100);   // 标签间隔约 18px
    var lastLabelY = -Infinity;
    chapters.forEach(function (ch, idx) {
      var y = wordToPercent(ch.words);
      var line = document.createElement('div');
      line.className = 'sl-chapter-line'; line.style.top = y + '%';
      line.style.borderTop = '1px dashed rgba(88,166,255,.25)';
      chapterPanel.appendChild(line);
      var isFirst = idx === 0;
      var isLast = idx === chapters.length - 1;
      if (isFirst || isLast || (y - lastLabelY) >= minLabelGap) {
        var mark = document.createElement('div');
        mark.className = 'sl-chapter-mark'; mark.style.top = y + '%';
        mark.innerHTML = '<div class="sl-chapter-dot"></div><div><div class="sl-chapter-num">第' + ch.num + '章</div></div>';
        chapterPanel.appendChild(mark);
        lastLabelY = y;
      }
    });
  }

  /* ─── 渲染：大纲 ─── */
  function renderOutlines(outlineBody, tooltip, showTooltip, moveTooltip, hideTooltip) {
    outlineBody.innerHTML = '';
    var h = outlineBody.clientHeight;
    if (!h || h < 40) h = 400;
    var res = assignLanes(outlines);
    outlines.forEach(function (o, i) {
      var lane = res.assignments[i];
      var top = wordToPercent(o.start);
      var height = wordToPercent(o.end - o.start);
      var laneW = 100 / res.totalLanes;
      var gap = 3;
      var bar = document.createElement('div');
      bar.className = 'sl-bar sl-bar-outline';
      bar.dataset.oid = o.id;
      bar.style.top = top + '%';
      bar.style.height = Math.max(height, 0.5) + '%';
      bar.style.left = 'calc(' + (lane * laneW) + '% + ' + (lane * gap) + 'px)';
      bar.style.width = 'calc(' + laneW + '% - ' + (res.totalLanes * gap) + 'px)';
      bar.style.right = 'auto';
      bar.style.zIndex = 10;
      if (o.narrative === 'flashback') {
        bar.style.background = 'linear-gradient(135deg,#d29922,#d29922cc)';
        bar.classList.add('sl-flashback');
      } else if (o.narrative === 'interleaved') {
        bar.style.background = 'linear-gradient(135deg,#3fb950,#3fb950aa)';
        bar.classList.add('sl-interleaved');
      } else {
        bar.style.background = 'linear-gradient(135deg,' + o.color + ',' + o.color + 'cc)';
      }
      var narration = o.narrative === 'flashback' ? '（倒叙）' : (o.narrative === 'interleaved' ? '（插叙）' : '');
      bar.dataset.tooltip = JSON.stringify({
        title: o.name,
        rows: [
          ['范围', '第' + (o.start / WPC + 1 | 0) + '—' + (o.end / WPC | 0) + '章'],
          ['字数', (o.start).toLocaleString() + ' — ' + o.end.toLocaleString()],
          ['手法', o.narrative === 'chronological' ? '顺叙' : (o.narrative === 'flashback' ? '倒叙' : '插叙')],
        ],
        tag: '大纲',
      });
      if (height > 1.2) {
        var label = document.createElement('span');
        label.className = 'sl-bar-label';
        label.textContent = o.name + narration;
        bar.appendChild(label);
      }
      bar.addEventListener('mouseenter', showTooltip);
      bar.addEventListener('mousemove', moveTooltip);
      bar.addEventListener('mouseleave', hideTooltip);
      outlineBody.appendChild(bar);
    });
  }

  /* ─── 渲染：桥段（嵌套 + 通道 + SVG 连线） ─── */
  function renderPlots(plotBody, tooltip, showTooltip, moveTooltip, hideTooltip) {
    plotBody.innerHTML = '';
    var bodyW = plotBody.clientWidth, bodyH = plotBody.clientHeight;
    if (!bodyH || bodyH < 40) bodyH = 400;

    function getLevel(plot, cache) {
      if (cache[plot.id] !== undefined) return cache[plot.id];
      if (!plot.parent) return (cache[plot.id] = 0);
      var parent = null;
      for (var i = 0; i < plots.length; i++) { if (plots[i].id === plot.parent) { parent = plots[i]; break; } }
      cache[plot.id] = parent ? getLevel(parent, cache) + 1 : 0;
      return cache[plot.id];
    }
    var levels = {};
    plots.forEach(function (p) { getLevel(p, levels); });
    var maxLevel = 0;
    plots.forEach(function (p) { if (levels[p.id] > maxLevel) maxLevel = levels[p.id]; });

    var byLevel = {};
    plots.forEach(function (p) {
      var lv = levels[p.id];
      (byLevel[lv] = byLevel[lv] || []).push({ id: p.id, start: p.start, end: p.end });
    });
    var laneInfo = {};
    Object.keys(byLevel).forEach(function (lv) {
      var res = assignLanes(byLevel[lv]);
      byLevel[lv].forEach(function (it, idx) {
        laneInfo[it.id] = { lane: res.assignments[idx], totalLanes: res.totalLanes };
      });
    });

    var levelBlockW = 100 / (maxLevel + 1);
    var gap = 3;

    plots.forEach(function (p) {
      var level = levels[p.id];
      var li = laneInfo[p.id] || { lane: 0, totalLanes: 1 };
      var top = wordToPercent(p.start);
      var height = wordToPercent(p.end - p.start);
      var blockLeft = level * levelBlockW;
      var laneW = 100 / li.totalLanes;
      var innerLeft = li.lane * laneW;
      var barLeft = blockLeft + innerLeft * (levelBlockW / 100);
      var barW = levelBlockW / li.totalLanes - gap;

      var bar = document.createElement('div');
      bar.className = 'sl-bar sl-bar-plot level-' + level;
      bar.dataset.pid = p.id;
      if (p.oid) bar.dataset.oid = p.oid;
      bar.style.top = top + '%';
      bar.style.height = Math.max(height, 0.4) + '%';
      bar.style.left = barLeft + '%';
      bar.style.width = 'calc(' + barW + '% - ' + (li.totalLanes * gap) + 'px)';
      bar.style.right = 'auto';
      bar.style.zIndex = 5 + level;
      if (level === 0) {
        bar.style.background = 'linear-gradient(135deg,' + p.color + ',' + p.color + 'cc)';
        bar.style.border = '1px solid rgba(255,255,255,.2)';
      } else if (level === 1) {
        bar.style.background = 'linear-gradient(135deg,' + p.color + '99,' + p.color + '88)';
        bar.style.borderLeft = '2px solid rgba(255,255,255,.3)';
      } else {
        bar.style.background = 'linear-gradient(135deg,' + p.color + '77,' + p.color + '55)';
        bar.style.borderLeft = '2px solid rgba(255,255,255,.2)';
      }
      var pms = promiseByPlot[p.id] || [];
      var promiseRows = pms.map(function (pm) {
        var tag = pm.kind === 'payoff' ? '✅ 已兑现' : '⏳ 待兑现';
        var txt = pm.pr.desc || '钩子';
        if (pm.kind === 'setup' && pm.pr.deadline_chapter) txt += '（约第' + pm.pr.deadline_chapter + '章）';
        if (pm.kind === 'payoff' && pm.pr.payoff_chapter) txt += '（第' + pm.pr.payoff_chapter + '章）';
        return [tag, txt];
      });
      bar.dataset.tooltip = JSON.stringify({
        title: p.name,
        rows: [
          ['层级', level === 0 ? '主桥段' : '子桥段 L' + level],
          ['范围', (p.start).toLocaleString() + ' — ' + p.end.toLocaleString() + ' 字'],
          ['线程', p.thread || '主线'],
          p.resolves ? ['收局', '解决「' + p.resolves_name + '」'] : null,
          (p.roles && p.roles.length) ? ['出场', p.roles.join('、')] : null,
        ].filter(Boolean).concat(promiseRows),
        tag: '桥段',
      });
      if (height > 1.0) {
        var label = document.createElement('span');
        label.className = 'sl-bar-label';
        label.textContent = p.name;
        label.style.fontSize = Math.min(9, Math.max(7, height * 0.3)) + 'px';
        bar.appendChild(label);
      }
      var tdot = document.createElement('span');
      tdot.className = 'sl-thread-dot';
      tdot.style.background = threadColor(p.thread);
      tdot.title = '线程：' + (p.thread || '主线');
      bar.appendChild(tdot);
      if (p.resolves) {
        var pbadge = document.createElement('span');
        pbadge.className = 'sl-payoff-badge';
        pbadge.textContent = '↪ 收局';
        bar.appendChild(pbadge);
      }
      // 读者承诺标记：设局⏳(待兑现) / 收局✅(已兑现)，直接画在桥段条上
      pms.forEach(function (pm) {
        var badge = document.createElement('span');
        badge.className = 'sl-promise ' + (pm.kind === 'payoff' ? 'ok' : 'pending');
        badge.textContent = pm.kind === 'payoff' ? '✅' : '⏳';
        badge.title = (pm.kind === 'payoff' ? '已兑现' : '待兑现') + '：' + (pm.pr.desc || '钩子');
        bar.appendChild(badge);
      });
      bar.addEventListener('mouseenter', showTooltip);
      bar.addEventListener('mousemove', moveTooltip);
      bar.addEventListener('mouseleave', hideTooltip);
      plotBody.appendChild(bar);
    });

    // 父子连线
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
    svg.style.position = 'absolute'; svg.style.top = '0'; svg.style.left = '0';
    svg.style.pointerEvents = 'none'; svg.style.zIndex = '0';
    plots.forEach(function (p) {
      if (!p.parent) return;
      var parent = null;
      for (var i = 0; i < plots.length; i++) { if (plots[i].id === p.parent) { parent = plots[i]; break; } }
      if (!parent) return;
      var parentLI = laneInfo[parent.id], childLI = laneInfo[p.id];
      if (!parentLI || !childLI) return;
      var parentMid = wordToPercent((parent.start + parent.end) / 2);
      var childMid = wordToPercent((p.start + p.end) / 2);
      function barCenterX(level, lane, totalLanes) {
        var blockL = (level / (maxLevel + 1)) * 100;
        var laneW = (1 / (maxLevel + 1)) * 100 / totalLanes;
        return blockL + lane * laneW + laneW / 2;
      }
      var pcx = barCenterX(levels[parent.id], parentLI.lane, parentLI.totalLanes);
      var ccx = barCenterX(levels[p.id], childLI.lane, childLI.totalLanes);
      var midY = (parentMid + childMid) / 2;
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M ' + pcx + '% ' + parentMid + '% C ' + pcx + '% ' + midY + '%, ' + ccx + '% ' + midY + '%, ' + ccx + '% ' + childMid + '%');
      path.setAttribute('stroke', 'rgba(255,255,255,0.1)');
      path.setAttribute('stroke-width', '1'); path.setAttribute('fill', 'none');
      svg.appendChild(path);
    });
    plotBody.appendChild(svg);
  }

  /* ─── 渲染：叙事线程横带（多线重叠=穿插可视） ─── */
  function renderThreads(threadBody, tooltip, showTooltip, moveTooltip, hideTooltip) {
    threadBody.innerHTML = '';
    var h = threadBody.clientHeight;
    if (!h || h < 40) h = 400;
    var res = assignLanes(threads);
    threads.forEach(function (t, i) {
      var lane = res.assignments[i];
      var top = wordToPercent(t.start);
      var height = wordToPercent(t.end - t.start);
      var laneW = 100 / res.totalLanes;
      var gap = 3;
      var band = document.createElement('div');
      band.className = 'sl-bar sl-bar-thread';
      band.dataset.tid = t.id;
      band.style.top = top + '%';
      band.style.height = Math.max(height, 0.8) + '%';
      band.style.left = 'calc(' + (lane * laneW) + '% + ' + (lane * gap) + 'px)';
      band.style.width = 'calc(' + laneW + '% - ' + (res.totalLanes * gap) + 'px)';
      band.style.right = 'auto';
      band.style.background = 'linear-gradient(135deg,' + t.color + '44,' + t.color + '22)';
      band.style.borderLeft = '2px solid ' + t.color;
      band.dataset.tooltip = JSON.stringify({
        title: '🧵 ' + t.name,
        rows: [
          ['范围', (t.start).toLocaleString() + ' — ' + (t.end).toLocaleString() + ' 字'],
        ],
        desc: t.desc || '',
        tag: '线程',
      });
      if (height > 1.0) {
        var label = document.createElement('span');
        label.className = 'sl-bar-label';
        label.textContent = t.name;
        label.style.fontSize = '9px';
        label.style.color = t.color;
        band.appendChild(label);
      }
      band.addEventListener('mouseenter', showTooltip);
      band.addEventListener('mousemove', moveTooltip);
      band.addEventListener('mouseleave', hideTooltip);
      threadBody.appendChild(band);
    });
  }

  /* ─── 渲染：进度光标 ─── */
  function renderCursor(contentArea, currentChapter) {
    if (!currentChapter || currentChapter <= 0) return;
    var y = wordToPercent(Math.min(currentChapter, TOTAL_WORDS / WPC) * WPC);
    var cursor = document.createElement('div');
    cursor.className = 'sl-cursor';
    cursor.style.top = y + '%';
    contentArea.appendChild(cursor);
  }

  /* ─── 工具提示 ─── */
  function makeTooltip(el) {
    function show(e) {
      var data = {};
      try { data = JSON.parse(e.currentTarget.dataset.tooltip); } catch (err) {}
      var html = '<div class="sl-tt-title">' + (data.title || '') + '</div>';
      (data.rows || []).forEach(function (r) { html += '<div class="sl-tt-row">' + r[0] + ': <span>' + r[1] + '</span></div>'; });
      if (data.desc) html += '<div class="sl-tt-row" style="margin-top:4px;color:#8b949e;">' + data.desc + '</div>';
      html += '<div class="sl-tt-tag">' + (data.tag || '') + '</div>';
      el.innerHTML = html;
      el.classList.add('show');
    }
    function move(e) { el.style.left = (e.clientX + 16) + 'px'; el.style.top = (e.clientY - 10) + 'px'; }
    function hide() { el.classList.remove('show'); }
    return { show: show, move: move, hide: hide };
  }

  /* ─── 对外入口 ─── */
  window.StoryLine = {
    init: function (mountId, bt, opts) {
      var mount = document.getElementById(mountId);
      if (!mount) return;
      opts = opts || {};
      adapt(bt);
      // 可选：按章节数拉长内容（仍是百分比渲染 → 每个百分比映射更多像素 → 条间距更大、可上下滚动）。
      // mount 填满外层容器，长卷内容由内部 .sl-main 滚动——整块只出现这一条滚动条。
      // 仅在调用方显式传 scrollable 时生效，避免影响 continue_flow 等「填满容器高度」的用法。
      var scrollH = 0;
      if (opts.scrollable) {
        var totalCh = Math.max(1, Math.round(TOTAL_WORDS / WPC));
        _baseScrollH = Math.min(2400, Math.max(720, totalCh * 18));
        scrollH = zoomHeight();
        mount.style.height = '100%';
        mount.style.minHeight = '0px';
      }
      var narrCount = { flashback: 0, interleaved: 0 };
      outlines.forEach(function (o) { if (o.narrative !== 'chronological') narrCount[o.narrative] = (narrCount[o.narrative] || 0) + 1; });

      var hstyle = scrollH ? (' style="height:' + scrollH + 'px"') : '';
      var threadLegendHtml = threads.map(function (t) {
        return '<div class="sl-legend-item"><span class="sl-legend-swatch" style="background:' + t.color + '"></span> ' + t.name + '</div>';
      }).join('');
      var zoomHtml = opts.scrollable
        ? '<div class="sl-zoom">' +
          '<button type="button" class="sl-zoom-btn" data-zoom="-1" title="缩小">−</button>' +
          '<span class="sl-zoom-label">' + Math.round(_zoom * 100) + '%</span>' +
          '<button type="button" class="sl-zoom-btn" data-zoom="1" title="放大">+</button>' +
          '<button type="button" class="sl-zoom-btn" data-zoom="0" title="重置 100%">1x</button>' +
          '</div>'
        : '';
      var html =
        '<div class="sl-root">' +
        '<div class="sl-header"><h1><span class="dot"></span>故事线</h1>' +
        '<div class="sl-header-right">' + zoomHtml +
        '<div class="sl-meta">总字数 <span>' + (TOTAL_WORDS).toLocaleString() + '</span> · 章节 <span>' + (TOTAL_WORDS / WPC | 0) + '</span> · 大纲 <span>' + outlines.length + '</span> · 桥段 <span>' + plots.length + '</span> · 线程 <span>' + threads.length + '</span></div></div></div>' +
        '<div class="sl-main">' +
        '<div class="sl-chapter-panel"' + hstyle + ' id="' + mountId + '-ch"></div>' +
        '<div class="sl-axis-panel"' + hstyle + ' id="' + mountId + '-ax"></div>' +
        '<div class="sl-content-area"' + hstyle + ' id="' + mountId + '-ct">' +
        '<div class="sl-lane" style="flex:3"><div class="sl-lane-header">📋 大纲</div><div class="sl-lane-body" id="' + mountId + '-ob"></div></div>' +
        '<div class="sl-lane" style="flex:7"><div class="sl-lane-header">🔗 桥段</div><div class="sl-lane-body" id="' + mountId + '-pb"></div></div>' +
        '<div class="sl-lane" style="flex:2"><div class="sl-lane-header">🧵 线程</div><div class="sl-lane-body" id="' + mountId + '-tb"></div></div>' +
        '</div></div>' +
        '<div class="sl-legend">' +
        '<div class="sl-legend-item"><span class="sl-legend-swatch" style="background:#f97583"></span> 大纲</div>' +
        '<div class="sl-legend-item"><span class="sl-legend-swatch" style="background:#79c0ff"></span> 主桥段</div>' +
        '<div class="sl-legend-item"><span class="sl-legend-swatch" style="background:#a5d6ff"></span> 子桥段</div>' +
        threadLegendHtml +
        (narrCount.flashback ? '<div class="sl-legend-item"><span class="sl-legend-swatch flashback"></span> 倒叙</div>' : '') +
        (narrCount.interleaved ? '<div class="sl-legend-item"><span class="sl-legend-swatch interleaved"></span> 插叙</div>' : '') +
        '<div class="sl-legend-item"><span class="sl-legend-swatch circle" style="background:#58a6ff"></span> 章节</div>' +
        '</div></div>';

      mount.innerHTML = html;
      var tooltip = document.createElement('div');
      tooltip.className = 'sl-tooltip';
      tooltip.id = mountId + '-tt';
      mount.appendChild(tooltip);
      var tt = makeTooltip(tooltip);

      var chapterPanel = document.getElementById(mountId + '-ch');
      var axisPanel = document.getElementById(mountId + '-ax');
      var outlineBody = document.getElementById(mountId + '-ob');
      var plotBody = document.getElementById(mountId + '-pb');
      var threadBody = document.getElementById(mountId + '-tb');
      var contentArea = document.getElementById(mountId + '-ct');

      // 纵向缩放控件：记录需改高度的面板 + 绑定 + / − / 1x 按钮
      _scrollableMode = !!opts.scrollable;
      _panels = [chapterPanel, axisPanel, contentArea];
      var zoomBtns = mount.querySelectorAll('.sl-zoom-btn');
      for (var zb = 0; zb < zoomBtns.length; zb++) {
        (function (btn) {
          btn.addEventListener('click', function () {
            var d = parseFloat(btn.getAttribute('data-zoom') || '0');
            window.StoryLine.setZoom(d === 0 ? 1 : (_zoom + d * 0.25));
          });
        })(zoomBtns[zb]);
      }

      function renderAll() {
        renderChapters(chapterPanel, axisPanel);
        renderOutlines(outlineBody, tooltip, tt.show, tt.move, tt.hide);
        renderPlots(plotBody, tooltip, tt.show, tt.move, tt.hide);
        renderThreads(threadBody, tooltip, tt.show, tt.move, tt.hide);
        var existing = contentArea.querySelector('.sl-cursor');
        if (existing) existing.remove();
        renderCursor(contentArea, opts.currentChapter);
      }
      _lastRender = renderAll;
      _lastMountId = mountId;
      renderAll();
    },

    /* 高亮：按 outline_id / plot_id 给故事线里对应的大纲/桥段条加高亮并滚动到可见位置。
       写作流页面在 plot_start / plot_done 时调用。 */
    highlight: function (target) {
      var mount = _lastMountId ? document.getElementById(_lastMountId) : null;
      if (!mount) return;
      var main = mount.querySelector('.sl-main');
      var prev = mount.querySelectorAll('.sl-bar.sl-highlight');
      for (var i = 0; i < prev.length; i++) prev[i].classList.remove('sl-highlight');
      var sel = [];
      // 用类限定：大纲条只匹配 sl-bar-outline；桥段条只匹配 sl-bar-plot（避免 data-oid 把整个大纲的桥段全点亮）
      if (target && target.outline_id) sel.push('.sl-bar-outline[data-oid="' + target.outline_id + '"]');
      if (target && target.plot_id) sel.push('.sl-bar-plot[data-pid="' + target.plot_id + '"]');
      if (!sel.length) return;
      var els = mount.querySelectorAll(sel.join(','));
      var anchor = null;
      for (var j = 0; j < els.length; j++) {
        els[j].classList.add('sl-highlight');
        if (!anchor) anchor = els[j];
      }
      if (anchor && main) {
        main.scrollTop = Math.max(0, anchor.offsetTop - main.clientHeight * 0.3);
      }
    },

    /* 纵向缩放：调整内容高度（放大=条间距更大可细看，缩小=更紧凑看全貌）。
       仅 scrollable 模式生效；改动面板高度后整卷重渲染。 */
    setZoom: function (factor) {
      if (!_scrollableMode) return;
      _zoom = Math.max(0.4, Math.min(4, factor));
      var h = zoomHeight();
      for (var i = 0; i < _panels.length; i++) {
        if (_panels[i]) _panels[i].style.height = h + 'px';
      }
      var mount = _lastMountId ? document.getElementById(_lastMountId) : null;
      var label = mount ? mount.querySelector('.sl-zoom-label') : null;
      if (label) label.textContent = Math.round(_zoom * 100) + '%';
      if (_lastRender) _lastRender();
    },
  };
})();
