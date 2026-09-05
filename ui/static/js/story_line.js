/*
 * 故事线（Story Line）组件 — 垂直 Gantt
 * 从 BookStoryline dict 渲染：字数轴 + 弧/桥段/线程通道。
 * 支持叙事手法视觉区分：顺叙(chronological)/倒叙(flashback)/插叙(interleaved)。
 *
 * 用法：StoryLine.init('mount-id', bookStorylineDict, {currentChapter: N})
 */
(function () {
  'use strict';

  var TOTAL_WORDS = 0;
  var WPC = 3000;
  var CHARS_PER_BEAT = 200, MAX_BRIDGE_WORDS = 1200, MAX_PLAN_WORDS = 3000;   // 与后端 storyline_writer.py 同一公式/口径
  var outlines = [], plots = [], threads = [];
  var promises = [], promiseByPlot = {};   // 读者承诺台账：桥段id → [{kind:setup/payoff, pr}]
  var setupIds = {};                       // 设局桥段 id 集合（被 resolves_plot_id 引用的桥段）
  var PALETTE = ['#f97583', '#79c0ff', '#56d364', '#e3b341', '#d2a8ff', '#ffa657', '#c084fc', '#7ee787'];
  var THREAD_PALETTE = ['#ffa657', '#79c0ff', '#d2a8ff', '#56d364', '#e3b341', '#ff7b72', '#7ee787'];

  var _lastRender = null;
  var _lastMountId = null;
  if (!window._sl_resize_bound) {
    window.addEventListener('resize', function () { if (_lastRender) _lastRender(); });
    window._sl_resize_bound = true;
  }

  /* ─── 纵向缩放：调整内容高度 → 百分比映射更多像素 → 条间距更大/更紧凑 ─── */
  var _DEFAULT_ZOOM = 4;    // 默认缩放倍率 400%（重置按钮目标）
  var _zoom = _DEFAULT_ZOOM; // 当前缩放倍率（0.4x ~ 4x，步进 0.25）
  var _baseScrollH = 720;   // scrollable 模式下未缩放的基准内容高度
  var _panels = [];         // 需随缩放改高度的面板（轴/内容区）
  var _scrollableMode = false;

  function zoomHeight() {
    return Math.max(300, Math.min(8000, Math.round((_baseScrollH || 720) * _zoom)));
  }

  /* 字数坐标 → 百分比：0 基字数，wordToPercent 过原点线性，
     故 height=wordToPercent(end-start)、mid=(start+end)/2 公式直接成立 */
  function wordToPercent(w) {
    return (TOTAL_WORDS > 0) ? (w / TOTAL_WORDS) * 100 : 0;
  }
  function fmtW(w) {
    return (w >= 1000) ? (Math.round(w / 1000 * 10) / 10) + 'k' : String(Math.round(w));
  }
  /* 桥段预计字数（规划/预估）：plot.words 优先（agent 目标字数，clamp 3000），否则节拍制 cover_beats×200 封顶 1200
     ——与后端 storyline_writer.planned_words 同一口径（words 覆盖 + beat 兜底） */
  function plannedWords(p) {
    if (p) {
      var w = parseInt((p && p.words) || 0, 10) || 0;
      if (w > 0) return Math.max(200, Math.min(w, MAX_PLAN_WORDS));
    }
    var beats = Math.max(parseInt((p && p.cover_beats) || 0, 10) || 0, 2);
    return Math.min(beats * CHARS_PER_BEAT, MAX_BRIDGE_WORDS);
  }

  /* 线程 id → 颜色 */
  function threadColor(tid) {
    for (var i = 0; i < threads.length; i++) { if (threads[i].id === tid) return threads[i].color; }
    return '#ffa657';
  }

  /* 线程稳定色：按 id 字符 hash 取色（跨编辑/派生线程顺序变化时同一线程恒色，不再按序循环） */
  function threadPalette(tid) {
    var s = String(tid || '主线');
    var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return THREAD_PALETTE[h % THREAD_PALETTE.length];
  }

  /* ─── 数据适配：BookStoryline → 平铺数组（桥段按真实规划字数定位，预计=实际） ───
     legacy 书 outline/plot/thread 缺 id：在此合成稳定 id + 建 raw→syn 映射，
     保证泳道/连线/徽标/高亮全自洽，前端不崩。 */
  function adapt(bt) {
    bt = bt || {};
    WPC = bt.words_per_chapter || 3000;
    setupIds = {};

    // Pass 1：outline 合成 id + raw→syn 映射（先于 plots 分组，供 outline_id 解析）
    var rawOutlineToSyn = {}, rawPlotToSyn = {};
    var rawOutlines = (bt.outlines || []);
    var rawPlots = (bt.plots || []);
    var outlineSynAt = [];
    rawOutlines.forEach(function (o, i) {
      var syn = o.id ? String(o.id) : '__outline_' + (i + 1);
      outlineSynAt[i] = syn;
      if (o.id) rawOutlineToSyn[String(o.id)] = syn;
    });

    // Pass 2：桥段按所属弧分组（syn outline id 作键；空/dangling outline_id 按位置均分保序）
    var byOutline = {};
    rawPlots.forEach(function (p, i) {
      var synPid = p.id ? String(p.id) : '__plot_' + (i + 1);
      if (p.id) rawPlotToSyn[String(p.id)] = synPid;
      var oid;
      if (p.outline_id && rawOutlineToSyn[String(p.outline_id)]) {
        oid = rawOutlineToSyn[String(p.outline_id)];
      } else {
        var n = Math.max(rawOutlines.length, 1);
        var bucket = rawOutlines.length ? Math.min(Math.floor(i * n / Math.max(rawPlots.length, 1)), n - 1) : 0;
        oid = outlineSynAt[bucket] || '__outline_1';
      }
      (byOutline[oid] = byOutline[oid] || []).push({ p: p, synPid: synPid });
    });
    function sortPlots(a, b) {
      return ((a.p.stage_index || 0) - (b.p.stage_index || 0)) || ((a.p.order || 0) - (b.p.order || 0));
    }
    // raw plot id → syn id（未知引用保留原文；空 → null）
    function resolvePlotId(rawId) {
      if (!rawId) return null;
      var s = String(rawId);
      return rawPlotToSyn[s] || s;
    }
    // 弧字数域归一：优先 start_word/end_word（0 基、start 含/end 不含，权威）；无则 start_chapter/end_chapter × WPC 推导
    function arcWords(o) {
      var sw = parseInt(o && o.start_word, 10);
      var ew = parseInt(o && o.end_word, 10);
      if (isFinite(sw) && sw >= 0 && isFinite(ew) && ew > sw) return { start_w: sw, end_w: ew };
      var sc = parseInt(o && o.start_chapter, 10);
      var ec = parseInt(o && o.end_chapter, 10);
      if (isFinite(sc) && sc >= 1 && isFinite(ec) && ec >= sc) return { start_w: (sc - 1) * WPC, end_w: ec * WPC };
      return { start_w: 0, end_w: 30 * WPC };   // 全缺 → 兜底 30 章 × 每章字数
    }

    // 弧 → 按字数跨度落位（字数轴），弧树嵌套靠 parent；start_ch/end_ch 为兼容视图（tooltip 展示）
    outlines = rawOutlines.map(function (o, i) {
      var syn = outlineSynAt[i];
      var w = arcWords(o);
      var st_ch = Math.floor(w.start_w / WPC) + 1;
      var en_ch = Math.max(st_ch, Math.ceil(w.end_w / WPC));
      return {
        id: syn, name: o.name,
        start_w: w.start_w, end_w: w.end_w,
        start_ch: st_ch, end_ch: en_ch,
        color: PALETTE[i % PALETTE.length],
        narrative: o.narrative || 'chronological',
        narrative_target: o.narrative_target || '',
        parent: (o.parent_arc_id && rawOutlineToSyn[String(o.parent_arc_id)])
          ? rawOutlineToSyn[String(o.parent_arc_id)] : null,
      };
    });
    // 字数坐标：0 基；总字数 = max(end_w)，至少 1
    TOTAL_WORDS = 1;
    outlines.forEach(function (o) {
      o.start = o.start_w;
      o.end = o.end_w;
      if (o.end > TOTAL_WORDS) TOTAL_WORDS = o.end;
    });

    // 桥段 → 在弧内按序比例均分章节段（弧 [s,e] 内 n 个桥段均分）
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
      // 桥段按 planned_words 在弧内累计定位（字数轴，与后端 storyline_writer.planned_words 同公式）
      var cursor = o.start;
      list.forEach(function (x) {
        var p = x.p;
        var s = cursor;
        var pw = plannedWords(p);
        var e = Math.min(cursor + pw, o.end);
        if (e <= s) e = Math.min(s + MAX_BRIDGE_WORDS, o.end);   // cover_beats 缺失兜底
        cursor = e;
        var parentSyn = resolvePlotId(p.parent_plot_id);
        var resolvesSyn = resolvePlotId(p.resolves_plot_id);
        if (resolvesSyn) setupIds[resolvesSyn] = true;   // 设局桥段登记（供设局徽标/设局→收局线）
        plots.push({
          id: x.synPid, name: p.name,
          oid: key,
          start: s,
          end: e,
          parent: parentSyn,
          color: parentSyn ? '#a5d6ff' : rootColor,
          category: p.category || '',
          thread: p.thread_id || '主线',
          resolves: resolvesSyn || '',
          resolves_name: p.resolves_name || '',
          roles: p.roles || [],
        });
      });
    });

    // 叙事线程 → 横带区间（id/name 双表匹配，解决存量「thread_id 与 threads 列表不闭合」；
    // 每线程收集 members 供泳道 tooltip 与设局/收局点）
    threads = [];
    var threadById = {}, threadByName = {};
    (bt.threads || []).forEach(function (t, i) {
      var id = t.id ? String(t.id) : (t.name || '__thread_' + (i + 1));
      if (!(id in threadById)) {
        threadById[id] = threads.length;
        if (t.name) threadByName[t.name] = threads.length;
        threads.push({ id: id, name: t.name || id, desc: t.desc || '', start: Infinity, end: -Infinity, color: threadPalette(id), members: [] });
      }
    });
    plots.forEach(function (fp) {
      var tid = fp.thread || '主线';
      var idx = (tid in threadById) ? threadById[tid] : ((tid in threadByName) ? threadByName[tid] : -1);
      if (idx < 0) {
        idx = threads.length;
        threadById[tid] = idx;
        threads.push({ id: tid, name: tid, desc: '', start: Infinity, end: -Infinity, color: threadPalette(tid), members: [] });
      }
      var t = threads[idx];
      if (fp.start < t.start) t.start = fp.start;
      if (fp.end > t.end) t.end = fp.end;
      t.members.push({ id: fp.id, name: fp.name, start: fp.start, end: fp.end, resolves: !!fp.resolves, setup: !!setupIds[fp.id] });
    });
    threads.forEach(function (t) {
      if (t.start === Infinity) { t.start = 0; t.end = Math.max(t.end, 1); }
    });

    // 读者承诺台账：设局桥段→⏳待兑现，收局桥段→✅已兑现；按 syn id 映射（legacy 书不错位）
    promises = (bt.promises || []);
    promiseByPlot = {};
    promises.forEach(function (pr) {
      var su = resolvePlotId(pr.setup_plot_id);
      var po = resolvePlotId(pr.payoff_plot_id);
      if (su) (promiseByPlot[su] = promiseByPlot[su] || []).push({ kind: 'setup', pr: pr });
      if (po) (promiseByPlot[po] = promiseByPlot[po] || []).push({ kind: 'payoff', pr: pr });
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

  /* ─── 渲染：字数轴（刻度线 = 该字数处，与弧 bar 顶边精确对齐） ─── */
  function renderAxis(axisPanel) {
    axisPanel.innerHTML = '<div class="sl-axis-line"></div>';
    var step = Math.max(WPC, Math.round(TOTAL_WORDS / 18 / 1000) * 1000);   // ~18 刻度，每段 ≥1 章字数
    function addTick(w) {
      var yPct = wordToPercent(w);
      var tick = document.createElement('div');
      tick.className = 'sl-tick'; tick.style.top = yPct + '%';
      axisPanel.appendChild(tick);
      var label = document.createElement('div');
      label.className = 'sl-tick-label'; label.style.top = yPct + '%';
      label.textContent = fmtW(w);
      axisPanel.appendChild(label);
    }
    for (var w = 0; w < TOTAL_WORDS; w += step) addTick(w);
    if (TOTAL_WORDS % step !== 0) addTick(TOTAL_WORDS);   // 兜底末刻度
  }

  /* ─── 渲染：弧（弧树嵌套：parent_arc_id 层级缩进 + 父子弧连线 + narrative_target 目标） ─── */
  function renderOutlines(outlineBody, tooltip, showTooltip, moveTooltip, hideTooltip) {
    outlineBody.innerHTML = '';
    var bodyW = outlineBody.clientWidth, bodyH = outlineBody.clientHeight;
    if (!bodyH || bodyH < 40) bodyH = 400;

    // 弧树层级：沿 parent（parent_arc_id）递归，父缺失→0 防环；
    // stack 记录求深中的祖先 id，parent_arc_id 成环（异常数据）时在此截断为 0，避免无限递归栈溢出
    function getArcLevel(o, cache, stack) {
      if (cache[o.id] !== undefined) return cache[o.id];
      stack = stack || {};
      if (stack[o.id]) return (cache[o.id] = 0);
      if (!o.parent) return (cache[o.id] = 0);
      var parent = null;
      for (var i = 0; i < outlines.length; i++) { if (outlines[i].id === o.parent) { parent = outlines[i]; break; } }
      stack[o.id] = true;
      cache[o.id] = parent ? getArcLevel(parent, cache, stack) + 1 : 0;
      delete stack[o.id];
      return cache[o.id];
    }
    var levels = {};
    outlines.forEach(function (o) { getArcLevel(o, levels); });
    var maxLevel = 0;
    outlines.forEach(function (o) { if (levels[o.id] > maxLevel) maxLevel = levels[o.id]; });

    var byLevel = {};
    outlines.forEach(function (o) {
      var lv = levels[o.id];
      (byLevel[lv] = byLevel[lv] || []).push({ id: o.id, start: o.start, end: o.end });
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

    outlines.forEach(function (o) {
      var level = levels[o.id];
      var li = laneInfo[o.id] || { lane: 0, totalLanes: 1 };
      var top = wordToPercent(o.start);
      var height = wordToPercent(o.end - o.start);
      var blockLeft = level * levelBlockW;
      var laneW = 100 / li.totalLanes;
      var innerLeft = li.lane * laneW;
      var barLeft = blockLeft + innerLeft * (levelBlockW / 100);
      var barW = levelBlockW / li.totalLanes - gap;

      var bar = document.createElement('div');
      bar.className = 'sl-bar sl-bar-outline level-' + level;
      bar.dataset.oid = o.id;
      bar.style.top = top + '%';
      bar.style.height = Math.max(height, 0.5) + '%';
      bar.style.left = barLeft + '%';
      bar.style.width = 'calc(' + barW + '% - ' + (li.totalLanes * gap) + 'px)';
      bar.style.right = 'auto';
      bar.style.zIndex = 10 + level;
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
      // 父弧名（供 tooltip）
      var parentName = '';
      if (o.parent) {
        for (var pi = 0; pi < outlines.length; pi++) { if (outlines[pi].id === o.parent) { parentName = outlines[pi].name; break; } }
      }
      bar.dataset.tooltip = JSON.stringify({
        title: o.name,
        rows: [
          ['字数', fmtW(o.start_w) + '—' + fmtW(o.end_w)],
          ['约第', o.start_ch + '—' + o.end_ch + '章'],
          ['手法', o.narrative === 'chronological' ? '顺叙' : (o.narrative === 'flashback' ? '倒叙' : '插叙')],
          parentName ? ['父弧', parentName] : null,
          o.narrative_target ? ['目标', o.narrative_target] : null,
        ].filter(Boolean),
        tag: '弧',
      });
      if (height > 1.2) {
        var label = document.createElement('span');
        label.className = 'sl-bar-label';
        label.textContent = o.name + narration;
        bar.appendChild(label);
      }
      // 叙事目标标记：倒叙/插叙且有 narrative_target 时在条上标 ◉
      if (o.narrative_target && o.narrative !== 'chronological') {
        var mark = document.createElement('span');
        mark.className = 'sl-target-mark';
        mark.textContent = '◉';
        mark.title = '目标：' + o.narrative_target;
        bar.appendChild(mark);
      }
      bar.addEventListener('mouseenter', showTooltip);
      bar.addEventListener('mousemove', moveTooltip);
      bar.addEventListener('mouseleave', hideTooltip);
      bar.addEventListener('click', function (e) {
        e.stopPropagation();
        bar.dispatchEvent(new CustomEvent('sl:outline-click',
          {detail: {outline_id: o.id}, bubbles: true}));
      });
      outlineBody.appendChild(bar);
    });

    // 父子弧连线（SVG 贝塞尔，复用桥段父子线的 px/py/barCenterX 机制）
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
    svg.style.position = 'absolute'; svg.style.top = '0'; svg.style.left = '0';
    svg.style.pointerEvents = 'none'; svg.style.zIndex = '0';
    bodyW = bodyW || outlineBody.clientWidth || 1;
    function px(x) { return (x / 100) * bodyW; }
    function py(y) { return (y / 100) * bodyH; }
    function arcCenterX(level, lane, totalLanes) {
      var blockL = (level / (maxLevel + 1)) * 100;
      var laneW = (1 / (maxLevel + 1)) * 100 / totalLanes;
      return blockL + lane * laneW + laneW / 2;
    }
    outlines.forEach(function (o) {
      if (!o.parent) return;
      var parent = null;
      for (var i = 0; i < outlines.length; i++) { if (outlines[i].id === o.parent) { parent = outlines[i]; break; } }
      if (!parent) return;
      var parentLI = laneInfo[parent.id], childLI = laneInfo[o.id];
      if (!parentLI || !childLI) return;
      var parentMid = py(wordToPercent((parent.start + parent.end) / 2));
      var childMid = py(wordToPercent((o.start + o.end) / 2));
      var pcx = px(arcCenterX(levels[parent.id], parentLI.lane, parentLI.totalLanes));
      var ccx = px(arcCenterX(levels[o.id], childLI.lane, childLI.totalLanes));
      var midY = (parentMid + childMid) / 2;
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M ' + pcx + ' ' + parentMid + ' C ' + pcx + ' ' + midY + ' ' + ccx + ' ' + midY + ' ' + ccx + ' ' + childMid);
      path.setAttribute('stroke', 'rgba(255,255,255,0.15)');
      path.setAttribute('stroke-width', '1'); path.setAttribute('fill', 'none');
      svg.appendChild(path);
    });
    outlineBody.appendChild(svg);
  }

  /* ─── 渲染：桥段（嵌套 + 通道 + SVG 连线） ─── */
  function renderPlots(plotBody, tooltip, showTooltip, moveTooltip, hideTooltip) {
    plotBody.innerHTML = '';
    var bodyW = plotBody.clientWidth, bodyH = plotBody.clientHeight;
    if (!bodyH || bodyH < 40) bodyH = 400;
    var contentH = zoomHeight();   // 可滚动内容区高度：桥段条实际像素高 = height% × contentH / 100（标签阈值按像素判断）
    var plotById = {};
    plots.forEach(function (p) { plotById[p.id] = p; });

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
      var barColor = threadColor(p.thread);   // 桥段条颜色 = 所属线程色（去右上角色点后，条本身颜色即线程标识）
      if (level === 0) {
        bar.style.background = 'linear-gradient(135deg,' + barColor + ',' + barColor + 'cc)';
        bar.style.border = '1px solid rgba(255,255,255,.2)';
      } else if (level === 1) {
        bar.style.background = 'linear-gradient(135deg,' + barColor + '99,' + barColor + '88)';
        bar.style.borderLeft = '2px solid rgba(255,255,255,.3)';
      } else {
        bar.style.background = 'linear-gradient(135deg,' + barColor + '77,' + barColor + '55)';
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
          ['字数', fmtW(p.start) + '—' + fmtW(p.end)],
          ['线程', p.thread || '主线'],
          p.resolves ? ['收局', '解决「' + p.resolves_name + '」'] : null,
          (p.roles && p.roles.length) ? ['出场', p.roles.join('、')] : null,
        ].filter(Boolean).concat(promiseRows),
        tag: '桥段',
      });
      if ((height / 100) * contentH >= 12) {   // 实际像素高 ≥12px 才显示名称：默认4x缩放下 1%≈28px 可显示；缩小到薄条时自动隐藏避免重叠
        var label = document.createElement('span');
        label.className = 'sl-bar-label';
        label.textContent = p.name;
        label.style.fontSize = Math.min(13, Math.max(11, height * 0.4)) + 'px';
        bar.appendChild(label);
      }
      if (p.resolves) {
        var pbadge = document.createElement('span');
        pbadge.className = 'sl-payoff-badge';
        pbadge.textContent = '↪ 收局';
        bar.appendChild(pbadge);
      }
      // 设局徽标：被其他桥段 resolves_plot_id 引用的桥段（top-right，与收局徽标并存）
      if (setupIds[p.id]) {
        var sbadge = document.createElement('span');
        sbadge.className = 'sl-setup-badge';
        sbadge.textContent = '◉ 设局';
        sbadge.title = '设局桥段：被后续桥段收束';
        bar.appendChild(sbadge);
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
      bar.addEventListener('click', function (e) {
        e.stopPropagation();
        bar.dispatchEvent(new CustomEvent('sl:plot-click',
          {detail: {plot_id: p.id, outline_id: p.oid}, bubbles: true}));
      });
      plotBody.appendChild(bar);
    });

    // 父子连线（SVG path 的 d 不支持 % 坐标 → 按 bodyW/bodyH 换算成像素）
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '100%'); svg.setAttribute('height', '100%');
    svg.style.position = 'absolute'; svg.style.top = '0'; svg.style.left = '0';
    svg.style.pointerEvents = 'none'; svg.style.zIndex = '0';
    bodyW = bodyW || plotBody.clientWidth || 1;
    function px(x) { return (x / 100) * bodyW; }
    function py(y) { return (y / 100) * bodyH; }
    plots.forEach(function (p) {
      if (!p.parent) return;
      var parent = null;
      for (var i = 0; i < plots.length; i++) { if (plots[i].id === p.parent) { parent = plots[i]; break; } }
      if (!parent) return;
      var parentLI = laneInfo[parent.id], childLI = laneInfo[p.id];
      if (!parentLI || !childLI) return;
      var parentMid = py(wordToPercent((parent.start + parent.end) / 2));
      var childMid = py(wordToPercent((p.start + p.end) / 2));
      function barCenterX(level, lane, totalLanes) {
        var blockL = (level / (maxLevel + 1)) * 100;
        var laneW = (1 / (maxLevel + 1)) * 100 / totalLanes;
        return blockL + lane * laneW + laneW / 2;
      }
      var pcx = px(barCenterX(levels[parent.id], parentLI.lane, parentLI.totalLanes));
      var ccx = px(barCenterX(levels[p.id], childLI.lane, childLI.totalLanes));
      var midY = (parentMid + childMid) / 2;
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M ' + pcx + ' ' + parentMid + ' C ' + pcx + ' ' + midY + ' ' + ccx + ' ' + midY + ' ' + ccx + ' ' + childMid);
      path.setAttribute('stroke', 'rgba(255,255,255,0.1)');
      path.setAttribute('stroke-width', '1'); path.setAttribute('fill', 'none');
      svg.appendChild(path);
    });

    // 设局→收局连线：resolves_plot_id 指向的设局桥段 → 本收局桥段的语义色虚线（与父子实线区分）
    plots.forEach(function (p) {
      if (!p.resolves) return;
      var target = plotById[p.resolves];
      if (!target) return;
      var srcLI = laneInfo[p.id], tgtLI = laneInfo[target.id];
      if (!srcLI || !tgtLI) return;
      var srcMid = py(wordToPercent((p.start + p.end) / 2));
      var tgtMid = py(wordToPercent((target.start + target.end) / 2));
      function barCenterX(level, lane, totalLanes) {
        var blockL = (level / (maxLevel + 1)) * 100;
        var laneW = (1 / (maxLevel + 1)) * 100 / totalLanes;
        return blockL + lane * laneW + laneW / 2;
      }
      var pcx = px(barCenterX(levels[p.id], srcLI.lane, srcLI.totalLanes));
      var ccx = px(barCenterX(levels[target.id], tgtLI.lane, tgtLI.totalLanes));
      var midY = (srcMid + tgtMid) / 2;
      // 跨长距离（连线高度 >60% 面板）衰减透明度，避免长线喧宾夺主
      var distRatio = Math.abs(srcMid - tgtMid) / Math.max(bodyH, 1);
      var opacity = distRatio > 0.6 ? 0.35 : 0.7;
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M ' + pcx + ' ' + srcMid + ' C ' + pcx + ' ' + midY + ' ' + ccx + ' ' + midY + ' ' + ccx + ' ' + tgtMid);
      path.setAttribute('stroke', '#e3b341');
      path.setAttribute('stroke-width', '1.5');
      path.setAttribute('stroke-dasharray', '4 3');
      path.setAttribute('opacity', String(opacity));
      path.setAttribute('fill', 'none');
      svg.appendChild(path);
    });
    plotBody.appendChild(svg);
  }

  /* ─── 渲染：叙事线程横带（成员列表 tooltip + 设局/收局点，多线重叠=穿插可视） ─── */
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
      // 成员列表：哪几个桥段构成这条线、哪处设局哪处收局
      var memberRows = (t.members || []).map(function (m) {
        var tag = m.setup ? '设局' : (m.resolves ? '收局' : '');
        return tag ? [m.name, tag] : [m.name, ''];
      });
      band.dataset.tooltip = JSON.stringify({
        title: '🧵 ' + t.name,
        rows: [['字数', fmtW(t.start) + '—' + fmtW(t.end)]].concat(memberRows),
        desc: t.desc || '',
        tag: '线程',
      });
      if (height > 1.0) {
        var label = document.createElement('span');
        label.className = 'sl-bar-label';
        label.textContent = t.name;
        label.style.fontSize = '10px';
        label.style.color = t.color;
        // 长线程名称跟随滚动显示：align-self 顶置 + sticky 钉在通道表头下方(24px)，
        // 滚动经过整条线程区间时名称始终可见；短线程无滚动时静置线程顶部。
        label.style.alignSelf = 'flex-start';
        label.style.position = 'sticky';
        label.style.top = '24px';
        label.style.zIndex = '30';
        label.style.background = 'rgba(13, 17, 23, 0.82)';
        label.style.padding = '1px 4px';
        label.style.borderRadius = '3px';
        band.appendChild(label);
      }
      // 设局/收局点：在横带上标出成员桥段位置（设局琥珀 / 收局绿）
      var bandH = wordToPercent(t.end - t.start);
      (t.members || []).forEach(function (m) {
        var point = document.createElement('span');
        point.className = 'sl-thread-point' + (m.setup ? ' setup' : (m.resolves ? ' payoff' : ''));
        if (bandH > 0) {
          var relTop = (wordToPercent(m.start) - wordToPercent(t.start)) / bandH * 100;
          point.style.top = Math.max(0, Math.min(100, relTop)) + '%';
        }
        point.title = m.name + (m.setup ? '（设局）' : (m.resolves ? '（收局）' : ''));
        band.appendChild(point);
      });
      band.addEventListener('mouseenter', showTooltip);
      band.addEventListener('mousemove', moveTooltip);
      band.addEventListener('mouseleave', hideTooltip);
      threadBody.appendChild(band);
    });
  }

  /* ─── 渲染：进度光标（字数轴：currentWord=累计已写字数；兼容 currentChapter×WPC） ─── */
  function renderCursor(contentArea, currentWord, currentChapter) {
    if (currentWord === undefined || currentWord === null || currentWord === 0) {
      if (!currentChapter || currentChapter <= 0) return;
      currentWord = currentChapter * WPC;
    }
    var w = Math.min(Math.max(0, currentWord), TOTAL_WORDS);   // 钳制越界
    var y = wordToPercent(w);
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
      if (data.desc) html += '<div class="sl-tt-desc">' + data.desc + '</div>';
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
      // 内容条用 min-height（非 height）：内容短于容器 → flex stretch 填满容器、无滚动条（空故事线=干净固定容器）；
      // 内容长于容器 → 内部 .sl-main 滚动，画布随内容增长（无限长）。仅 scrollable 调用方生效。
      var scrollH = 0;
      if (opts.scrollable) {
        var totalCh = Math.max(1, Math.round(TOTAL_WORDS / Math.max(WPC, 1)));   // 预计章数（字数轴）
        _baseScrollH = Math.min(2400, totalCh * 18);
        scrollH = zoomHeight();
        mount.style.height = '100%';
        mount.style.minHeight = '0px';
      }
      var narrCount = { flashback: 0, interleaved: 0 };
      outlines.forEach(function (o) { if (o.narrative !== 'chronological') narrCount[o.narrative] = (narrCount[o.narrative] || 0) + 1; });

      var hstyle = scrollH ? (' style="min-height:' + scrollH + 'px"') : '';
      var threadLegendHtml = threads.map(function (t) {
        return '<div class="sl-legend-item"><span class="sl-legend-swatch" style="background:' + t.color + '"></span> ' + t.name + '</div>';
      }).join('');
      var zoomHtml = opts.scrollable
        ? '<div class="sl-zoom">' +
          '<button type="button" class="sl-zoom-btn" data-zoom="-1" title="缩小">−</button>' +
          '<span class="sl-zoom-label">' + Math.round(_zoom * 100) + '%</span>' +
          '<button type="button" class="sl-zoom-btn" data-zoom="1" title="放大">+</button>' +
          '<button type="button" class="sl-zoom-btn" data-zoom="0" title="重置默认">默认</button>' +
          '</div>'
        : '';
      var html =
        '<div class="sl-root">' +
        '<div class="sl-header"><h1><span class="dot"></span>故事线</h1>' +
        '<div class="sl-header-right">' + zoomHtml +
        '<div class="sl-meta">总字数 <span>' + fmtW(TOTAL_WORDS) + '</span> · 预计 <span>' + Math.max(1, Math.ceil(TOTAL_WORDS / Math.max(WPC, 1))) + '</span> 章 · 每章约 <span>' + WPC + '</span> 字 · 弧 <span>' + outlines.length + '</span> · 桥段 <span>' + plots.length + '</span> · 线程 <span>' + threads.length + '</span></div></div></div>' +
        '<div class="sl-main">' +
        '<div class="sl-axis-panel"' + hstyle + ' id="' + mountId + '-ax"></div>' +
        '<div class="sl-content-area"' + hstyle + ' id="' + mountId + '-ct">' +
        '<div class="sl-lane" style="flex:4"><div class="sl-lane-header">📋 弧</div><div class="sl-lane-body" id="' + mountId + '-ob"></div></div>' +
        '<div class="sl-lane" style="flex:4"><div class="sl-lane-header">🔗 桥段</div><div class="sl-lane-body" id="' + mountId + '-pb"></div></div>' +
        '<div class="sl-lane" style="flex:2"><div class="sl-lane-header">🧵 线程</div><div class="sl-lane-body" id="' + mountId + '-tb"></div></div>' +
        '</div></div>' +
        '<div class="sl-legend">' +
        '<div class="sl-legend-item"><span class="sl-legend-swatch" style="background:#f97583"></span> 弧</div>' +
        '<div class="sl-legend-item"><span class="sl-legend-swatch" style="background:#79c0ff"></span> 主桥段</div>' +
        '<div class="sl-legend-item"><span class="sl-legend-swatch" style="background:#a5d6ff"></span> 子桥段</div>' +
        threadLegendHtml +
        '<div class="sl-legend-item"><span class="sl-legend-swatch payoff-line"></span> ◉设局 → ↪收局</div>' +
        (narrCount.flashback ? '<div class="sl-legend-item"><span class="sl-legend-swatch flashback"></span> 倒叙</div>' : '') +
        (narrCount.interleaved ? '<div class="sl-legend-item"><span class="sl-legend-swatch interleaved"></span> 插叙</div>' : '') +
        '</div></div>';

      mount.innerHTML = html;
      var tooltip = document.createElement('div');
      tooltip.className = 'sl-tooltip';
      tooltip.id = mountId + '-tt';
      mount.appendChild(tooltip);
      var tt = makeTooltip(tooltip);

      var axisPanel = document.getElementById(mountId + '-ax');
      var outlineBody = document.getElementById(mountId + '-ob');
      var plotBody = document.getElementById(mountId + '-pb');
      var threadBody = document.getElementById(mountId + '-tb');
      var contentArea = document.getElementById(mountId + '-ct');

      // 纵向缩放控件：记录需改高度的面板 + 绑定 + / − / 1x 按钮
      _scrollableMode = !!opts.scrollable;
      _panels = [axisPanel, contentArea];
      var zoomBtns = mount.querySelectorAll('.sl-zoom-btn');
      for (var zb = 0; zb < zoomBtns.length; zb++) {
        (function (btn) {
          btn.addEventListener('click', function () {
            var d = parseFloat(btn.getAttribute('data-zoom') || '0');
            window.StoryLine.setZoom(d === 0 ? _DEFAULT_ZOOM : (_zoom + d * 0.25));
          });
        })(zoomBtns[zb]);
      }

      function renderAll() {
        renderAxis(axisPanel);
        renderOutlines(outlineBody, tooltip, tt.show, tt.move, tt.hide);
        renderPlots(plotBody, tooltip, tt.show, tt.move, tt.hide);
        renderThreads(threadBody, tooltip, tt.show, tt.move, tt.hide);
        var existing = contentArea.querySelector('.sl-cursor');
        if (existing) existing.remove();
        renderCursor(contentArea, opts.currentWord, opts.currentChapter);
      }
      _lastRender = renderAll;
      _lastMountId = mountId;
      renderAll();
    },

    /* 高亮：按 outline_id / plot_id 给故事线里对应的弧/桥段条加高亮并滚动到可见位置。
       写作流页面在 plot_start / plot_done 时调用。 */
    highlight: function (target) {
      var mount = _lastMountId ? document.getElementById(_lastMountId) : null;
      if (!mount) return;
      var main = mount.querySelector('.sl-main');
      var prev = mount.querySelectorAll('.sl-bar.sl-highlight');
      for (var i = 0; i < prev.length; i++) prev[i].classList.remove('sl-highlight');
      var sel = [];
      // 用类限定：弧条只匹配 sl-bar-outline；桥段条只匹配 sl-bar-plot（避免 data-oid 把整个弧的桥段全点亮）
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

    /* 纯滚动：把故事线滚动到目标弧/桥段条可见（不改变高亮状态）。
       Agent 画布控制 scroll_to_plot / scroll_to_outline 使用。 */
    scrollTo: function (target) {
      var mount = _lastMountId ? document.getElementById(_lastMountId) : null;
      if (!mount) return;
      var main = mount.querySelector('.sl-main');
      var anchor = null;
      if (target && target.plot_id) {
        anchor = mount.querySelector('.sl-bar-plot[data-pid="' + target.plot_id + '"]');
      }
      if (!anchor && target && target.outline_id) {
        anchor = mount.querySelector('.sl-bar-outline[data-oid="' + target.outline_id + '"]');
      }
      if (anchor && main) main.scrollTop = Math.max(0, anchor.offsetTop - main.clientHeight * 0.3);
    },

    /* 纵向缩放：调整内容高度（放大=条间距更大可细看，缩小=更紧凑看全貌）。
       仅 scrollable 模式生效；改动面板高度后整卷重渲染。 */
    setZoom: function (factor) {
      if (!_scrollableMode) return;
      _zoom = Math.max(0.4, Math.min(4, factor));
      var h = zoomHeight();
      for (var i = 0; i < _panels.length; i++) {
        if (_panels[i]) _panels[i].style.minHeight = h + 'px';
      }
      var mount = _lastMountId ? document.getElementById(_lastMountId) : null;
      var label = mount ? mount.querySelector('.sl-zoom-label') : null;
      if (label) label.textContent = Math.round(_zoom * 100) + '%';
      if (_lastRender) _lastRender();
    },
  };
})();
