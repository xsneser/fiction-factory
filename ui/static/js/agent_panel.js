// Agent 聊天助手面板（OpenClaw 式）：侧栏对话，Agent 通过 function calling 操作引擎并导航页面。
// 后端 /api/agent/chat（SSE）。对话历史仅存 user/assistant 文本，工具步骤卡临时展示不入历史。
// 版本标记：新 JS（事件流实时工具卡）会在控制台打印 v3；旧 JS 无此输出——用于排查浏览器缓存。
console.log('[agent-panel] v10 events-stream');
(function() {
    var chat = document.getElementById('agent-chat');
    var input = document.getElementById('agent-input');
    var sendBtn = document.getElementById('agent-send');
    var clearBtn = document.getElementById('agent-clear');
    var stopBtn = document.getElementById('agent-stop');
    var toolsLog = document.getElementById('agent-tools-log');
    if (!chat || !input || !sendBtn) return;   // 布局缺失则静默跳过

    var HISTORY_KEY = 'ne_agent_history';
    var HISTORY_LIMIT = 40;
    var busy = false;
    var pendingTask = null;                     // busy 时排队待发任务（done 后接力）
    var currentToolRun = null;                  // 当前工具卡引用
    var toolPollTimer = null;                   // 工具日志轮询定时器
    var toolCards = {};                         // callId → 工具卡（事件流配对）
    var toolCardOrder = [];                     // 工具卡创建顺序（上限裁剪用）
    var TOOL_CARD_LIMIT = 20;                   // 对话页签工具卡上限（防 DOM 膨胀）

    function loadHistory() {
        try { return JSON.parse(sessionStorage.getItem(HISTORY_KEY) || '[]'); }
        catch (e) { return []; }
    }
    function saveHistory(h) {
        try { sessionStorage.setItem(HISTORY_KEY, JSON.stringify(h.slice(-HISTORY_LIMIT))); } catch (e) {}
    }
    var history = loadHistory();

    // ─── 工具卡中文化：工具名 → 中文动作；drive_ui cmd → 中文效果；JSON 键 → 中文 ───
    var TOOL_ZH = {
        borrow_preview: '预览借鉴设定', confirm_outlines: '确认大纲', confirm_world: '确认世界观',
        deai_text: '去 AI 味', diagnose_retention: '追读诊断', drive_ui: '驱动建书向导',
        export_book: '导出投稿包', extend_outline: '续写故事线', fill_gags: '挂载笑点',
        fill_plots: '填充桥段', generate_book_meta: '生成书名+简介', generate_characters: '生成角色',
        generate_core_conflict: '生成核心矛盾', generate_factions: '生成势力', generate_full_outline: '生成完整大纲',
        generate_outlines: '生成大纲序列', generate_rest_world: '补全其余世界观', generate_title: '生成书名',
        generate_world: '生成世界观', get_book_detail: '读取书详情', get_book_state: '读取书状态',
        get_build_status: '读取建书状态', get_storyline: '读取故事线', list_books: '列出书库',
        mark_finished: '标记完本', navigate: '页面跳转', outline_agent: '大纲助手',
        outline_material_candidates: '取选材候选', publish_book: '上架', publish_check: '上架检查',
        query_characters: '查角色原型', query_gags: '查笑点库', query_plots: '查桥段库',
        query_profiles: '查笔名档案', query_structures: '查大纲库', review_text: '审查文本',
        save_basic_info: '保存基础设定', tag_punch_points: '标注爽点', world_candidates: '生成世界观候选',
        write_chapter: '写章节', write_next_bridge: '写下一桥段'
    };
    var CMD_ZH = {
        set_world: '写入世界观', set_characters: '写入角色', set_candidates: '填入候选',
        pick_candidate: '选中候选', set_field: '填写字段', set_tags: '设置标签',
        next: '下一步', prev: '上一步', reset: '重置向导', submit: '提交建书',
        skip_candidates: '跳过候选', load_candidates: '加载候选', fill_world: '重新补全',
        set_picks: '记录选材'
    };
    var KEY_ZH = {
        core_conflict: '核心矛盾', genre: '题材', sub_genre: '题材细分', factions: '势力', faction: '势力',
        name: '名称', stance: '立场', desc: '描述', characters: '人物', protagonist: '主角',
        supporting_cast: '配角', identity: '身份', personality: '性格', golden_finger: '金手指',
        catchphrase: '口癖', role: '角色', importance: '重要度', relation: '关系', brief: '简介',
        title: '标题', idea: '一句话设定', tags: '题材标签', pen_name: '笔名', candidates: '候选',
        one_liner: '一句话梗概', world_brief: '世界观简述', outline: '大纲', templates: '模板',
        era: '时代', power_system: '力量体系', geography: '地理', culture: '文化', history: '历史',
        social_structure: '社会结构', rules: '规则', world_summary: '设定概述', tone: '基调',
        target_audience: '目标读者', pov: '视角', era_language: '时代语言', description: '描述',
        status: '状态', ok: '成功', error: '错误', book_id: '书 ID', phase: '阶段',
        world_building: '世界观', cmd: '命令', __ui_command__: '命令', book: '书', chapter: '章节',
        words_per_chapter: '每章字数', archetype_id: '原型', age: '年龄', death_year: '去世年份',
        gender: '性别', borrow: '借鉴', tweak: '微调', category: '分类', keyword: '关键词',
        mode: '模式', plot: '桥段', plots: '桥段', structure: '结构', structures: '模板',
        gag: '梗', gags: '梗', count: '数量', total: '总计', storyline: '时间线',
        outlines: '大纲', timeline: '时间线', source: '来源', id: 'ID', pen: '笔名',
        url: '地址', words: '字数', word_count: '字数', target_words: '目标字数',
        passed: '通过', score: '评分', message: '消息', recent_n: '最近章数',
        chapter_num: '章节号', max_outlines: '大纲数', struct: '结构'
    };
    function toolLabel(tool, args) {
        if (tool === 'drive_ui') {
            var cmd = (args && args.cmd) || '';
            var c = CMD_ZH[cmd] || cmd || '';
            return '驱动向导' + (c ? ' · ' + c : '');
        }
        return TOOL_ZH[tool] || tool;
    }
    function zhKeys(v) {
        if (Array.isArray(v)) return v.map(zhKeys);
        if (v && typeof v === 'object') {
            var out = {};
            for (var k in v) if (Object.prototype.hasOwnProperty.call(v, k)) out[KEY_ZH[k] || k] = zhKeys(v[k]);
            return out;
        }
        return v;
    }
    function zhSummary(tool, args, summary) {
        if (tool === 'drive_ui') {
            var cmd = (args && args.cmd) || '';
            return '已' + (CMD_ZH[cmd] || cmd || '执行向导命令');
        }
        if (!summary) return '';
        try {
            var obj = JSON.parse(summary);
            if (obj && typeof obj === 'object') return JSON.stringify(zhKeys(obj), null, 1).slice(0, 600);
        } catch (e) {}
        return summary;
    }

    // ─── 渲染 ───
    function el(tag, cls, text) {
        var d = document.createElement(tag);
        if (cls) d.className = cls;
        if (text !== undefined) d.textContent = text;
        return d;
    }
    function scrollBottom() { chat.scrollTop = chat.scrollHeight; }

    function addMsg(role, text) {
        var wrap = el('div', 'agent-msg ' + (role === 'user' ? 'user' : 'assistant'));
        wrap.appendChild(el('div', 'agent-msg-label', role === 'user' ? '你' : '🤖 Agent'));
        var body = el('div', 'agent-msg-body', text || '');
        wrap.appendChild(body);
        chat.appendChild(wrap);
        scrollBottom();
        return wrap;
    }

    function addToolCard(tool, args) {
        var card = el('div', 'agent-tool-card');
        var head = el('div', 'agent-tool-head', '🔧 ' + escapeHtml(toolLabel(tool, args)));
        head.title = '点击展开/收起参数';
        var detail = el('div', 'agent-tool-detail', '');
        detail.style.display = 'none';
        if (args && typeof args === 'object' && Object.keys(args).length) {
            head.onclick = function() {
                var show = detail.style.display === 'none';
                detail.style.display = show ? 'block' : 'none';
                if (show) detail.textContent = JSON.stringify(zhKeys(args), null, 2);
            };
        }
        var status = el('div', 'agent-tool-status', '运行中…');
        card.appendChild(head);
        card.appendChild(detail);
        card.appendChild(status);
        chat.appendChild(card);
        scrollBottom();
        return { card: card, status: status, t0: performance.now(), tool: tool, args: args };
    }

    // 事件流工具卡：按 callId 建档，超上限裁剪最旧（防 DOM 无限膨胀）
    function addToolCardFor(name, args, callId) {
        var run = addToolCard(name, args);
        if (callId) toolCards[callId] = run;
        toolCardOrder.push(callId || ('#' + toolCardOrder.length));
        if (toolCardOrder.length > TOOL_CARD_LIMIT) {
            var old = toolCardOrder.shift();
            var oldRun = toolCards[old];
            if (oldRun && oldRun.card && oldRun.card.parentNode) oldRun.card.parentNode.removeChild(oldRun.card);
            delete toolCards[old];
        }
        return run;
    }

    // 工具卡收尾：状态文本 + 执行耗时（⏱）+ ok/err 类名
    function finishToolCard(run, text) {
        if (!run || !run.status) return;
        var dur = '';
        if (run.t0) {
            var ms = performance.now() - run.t0;
            dur = ' ⏱ ' + (ms < 1000 ? Math.round(ms) + 'ms' : (ms / 1000).toFixed(1) + 's');
        }
        run.status.textContent = (text || '') + dur;
        run.status.className = (text && text.indexOf('✅') === 0)
            ? 'agent-tool-status ok' : 'agent-tool-status err';
    }
    // ─── 工具日志页签（右侧面板「💬 对话 / 🔧 工具日志」切换）───
    function switchAgentTab(key) {
        var chatPane = document.getElementById('agent-chat');
        document.querySelectorAll('.agent-tabs a').forEach(function(a) {
            a.classList.toggle('active', a.getAttribute('data-tab') === key);
        });
        if (chatPane) chatPane.style.display = (key === 'chat') ? '' : 'none';
        if (toolsLog) toolsLog.style.display = (key === 'chat') ? 'none' : '';
        // 输入框/发送只属于「💬 对话」页签；工具日志页签下不出现对话框
        var inputWrap = document.getElementById('agent-input-wrap');
        if (inputWrap) inputWrap.style.display = (key === 'chat') ? '' : 'none';
        if (key === 'tools') {
            loadToolLog();
            if (!toolPollTimer) toolPollTimer = setInterval(loadToolLog, 3000);
        } else {
            if (toolPollTimer) { clearInterval(toolPollTimer); toolPollTimer = null; }
        }
    }

    function escArg(args) {
        try { return JSON.stringify(args, null, 1).slice(0, 500); } catch (e) { return String(args); }
    }

    function loadToolLog() {
        if (!toolsLog) return;
        fetch('/api/agent/tool-log')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.ok) return;
                // 只展示外部 MCP 调用（source=mcp）；内嵌 agent 的 web 调用留在对话页签的工具卡片里
                var log = (d.log || []).filter(function(x) { return x.source === 'mcp'; });
                var success = log.filter(function(x) { return x.ok; }).length;
                // 超长自动清理：只渲染最近 DISPLAY_LIMIT 条，避免 DOM 无限膨胀拖垮侧栏
                var DISPLAY_LIMIT = 80;
                var show = log.slice(-DISPLAY_LIMIT);
                // 自动滚到底：渲染前记录是否在底部附近（跟随中）；新工具到达时跟随到底，
                // 不在底部（在翻旧记录）则按比例还原位置，不被 3s 轮询顶回顶部
                var h0 = toolsLog.scrollHeight;
                var ratio = h0 ? toolsLog.scrollTop / h0 : 0;
                var nearBottom = h0 - toolsLog.scrollTop - toolsLog.clientHeight < 80;
                var html = '<div style="position:sticky;top:0;z-index:1;background:#161b22;font-size:12px;color:#8b949e;padding:4px 0 8px;margin-bottom:4px">'
                    + '已暴露 <strong>' + d.tools_exposed + '</strong> 工具 · MCP 调用 <strong>' + log.length
                    + '</strong> 次 · 成功 <span style="color:#3fb950">' + success
                    + '</span> 失败 <span style="color:#f85149">' + (log.length - success) + '</span>'
                    + ' <button class="small" onclick="window.loadToolLog()">🔄 刷新</button>'
                    + ' <button class="small" onclick="window.clearToolLog()">🗑 清空</button>'
                    + (log.length > DISPLAY_LIMIT ? ' <span style="color:#8b949e">仅显示最近 ' + DISPLAY_LIMIT + ' 条（共 ' + log.length + '）</span>' : '')
                    + '</div>';
                if (!log.length) {
                    html += '<div style="font-size:12px;color:#8b949e;padding:8px 4px">暂无外部 MCP 调用记录（由 Claude Code 经 MCP 驱动时产生）。</div>';
                }
                show.forEach(function(x) {
                    html += '<div class="agent-tool-card ' + (x.ok ? 'ok' : 'err') + '">'
                        + '<div class="agent-tool-head">' + escapeHtml((x.time || '') + ' ' + (x.ok ? '✅' : '❌') + ' ' + toolLabel(x.tool, x.args))
                        + ' <span style="color:#8b949e;font-weight:normal">' + (x.duration_ms || 0) + 'ms</span></div>';
                    if (x.args && typeof x.args === 'object' && Object.keys(x.args).length) {
                        html += '<div class="agent-tool-detail" style="display:none">' + escArg(zhKeys(x.args)) + '</div>';
                    }
                    html += '<div class="agent-tool-status">' + (x.ok ? '' : '❌ ') + escapeHtml(zhSummary(x.tool, x.args, x.summary) || '') + '</div></div>';
                });
                toolsLog.innerHTML = html;
                // 渲染后滚动：跟随中→贴底（新工具自动可见）；翻旧记录→按比例还原位置
                if (nearBottom) toolsLog.scrollTop = toolsLog.scrollHeight;
                else toolsLog.scrollTop = Math.round(ratio * toolsLog.scrollHeight);
                toolsLog.querySelectorAll('.agent-tool-card').forEach(function(card) {
                    var head = card.querySelector('.agent-tool-head');
                    var detail = card.querySelector('.agent-tool-detail');
                    if (head && detail) head.addEventListener('click', function() {
                        detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
                    });
                });
            })
            .catch(function() {});
    }

    function clearToolLog() {
        fetch('/api/agent/tool-log/clear', { method: 'POST' })
            .then(function() { loadToolLog(); })
            .catch(function() {});
    }

    // 供 base.html inline onclick 调用
    window.switchAgentTab = switchAgentTab;
    window.loadToolLog = loadToolLog;
    window.clearToolLog = clearToolLog;

    // ─── SSE 消费（fetch + getReader 手写解析，项目现有模式）───
    function consumeSSE(body) {
        return fetch('/api/agent/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        }).then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.body.getReader();
        }).then(function(reader) {
            var decoder = new TextDecoder();
            var buf = '';
            function pump() {
                return reader.read().then(function(res) {
                    if (res.done) return;
                    buf += decoder.decode(res.value, { stream: true });
                    var lines = buf.split('\n');
                    buf = lines.pop();
                    for (var i = 0; i < lines.length; i++) {
                        var line = lines[i].trim();
                        if (line.indexOf('data:') !== 0) continue;
                        var data = line.slice(5).trim();
                        if (!data) continue;
                        try { handleEvent(JSON.parse(data)); } catch (e) {}
                    }
                    return pump();
                });
            }
            return pump();
        });
    }

    function handleEvent(evt) {
        var t = evt.type;
        if (t === 'tool_call') {
            // dsh 核心实时推送：工具开始 → 建卡
            currentToolRun = addToolCardFor(evt.name, evt.args, evt.callId);
        } else if (t === 'tool_result') {
            // 按 callId 配对卡；配不到就忽略（绝不 fallback 到别的卡，避免污染）。
            // navigate/drive_ui 的 tool/call 也会建卡，故正常情况都配得到。
            var run = (evt.callId && toolCards[evt.callId]) ? toolCards[evt.callId] : null;
            if (evt.callId) delete toolCards[evt.callId];
            if (run) finishToolCard(run, (evt.ok ? '✅ ' : '❌ ') + zhSummary(run.tool, run.args, evt.summary));
        } else if (t === 'navigate') {
            handleNavigate(evt.url);            // dsh 调用 navigate → 实时切页
        } else if (t === 'ui_command') {
            dispatchCommand({ cmd: evt.cmd, args: evt.args || {} });  // drive_ui → 驱动建书向导
        } else if (t === 'reply') {
            addMsg('assistant', evt.content);
            history.push({ role: 'assistant', content: evt.content });
            saveHistory(history);
        } else if (t === 'error') {
            addMsg('assistant', '⚠️ ' + (evt.message || '发生错误'));
            if (currentToolRun) finishToolCard(currentToolRun, '❌ 失败');
        } else if (t === 'done') {
            // C3：收尾所有未 resolve 的工具卡（result 缺失/滞后时兜底），清空映射
            Object.keys(toolCards).forEach(function(id) {
                finishToolCard(toolCards[id], '⚠️ 会话结束未收尾');
            });
            toolCards = {};
            toolCardOrder = [];
            if (currentToolRun) { finishToolCard(currentToolRun, '✅ 完成'); currentToolRun = null; }
            // C1：drain nav-intent 残留——会话期间 busy 守卫跳过轮询消费、而 navigate/drive_ui
            // 每次都无条件写队列，这些意图已由 SSE 事件流实时执行过；done 后轮询恢复会把
            // 残留重放（drive_ui(next) 走两步 / submit 二次建书）。取走清空，丢弃安全。
            fetch('/api/agent/nav-intents').catch(function(){});
            if (pendingTask) {
                // 接力排队任务（busy 保持 true）：先打断已 done，此刻才渲染新任务卡并启动 —— 严格先打断后开始
                var pt = pendingTask; pendingTask = null;
                renderTaskStart(pt.text, pt.opts || {});
                startTask(pt.text);
            } else {
                busy = false;
                setSendEnabled(true);
            }
        }
    }

    // ─── navigate 外部驱动桥（P1b）：轮询 MCP 写入的导航意图，取到即翻页/切页签 ───
    var navTimer = null;
    // 消费意图队列。onlyCmds 非空时只处理指定的 ui_command（busy 中用）：dsh 会话中模型驱动的
    // 命令（set_field/set_world/set_characters/next/submit…）与 add_candidate 已由 SSE 实时推送，
    // 轮询消费会双触发；但 set_outline 由 generate_outline_preview 后端直推（SSE 无此事件），
    // 必须 busy 中也消费，否则大纲数据落不进向导、步3 故事线不显示。
    function consumeNavIntents(onlyCmds) {
        return fetch('/api/agent/nav-intents')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.ok) return;
                (d.intents || []).forEach(function(it) {
                    if (it.kind === 'ui_command') {
                        if (onlyCmds && onlyCmds.indexOf(it.cmd) < 0) return;
                        dispatchCommand(it); return;
                    }
                    if (it.url) handleNavigate(it.url);
                    if (it.tab && (it.tab === 'chat' || it.tab === 'tools')) switchAgentTab(it.tab);
                });
            })
            .catch(function() {});
    }
    function pollNavIntents() {
        if (busy) { consumeNavIntents(['set_outline']); return; }   // dsh 会话中：仅消费工具直推的 set_outline
        consumeNavIntents(null);
    }
    navTimer = setInterval(pollNavIntents, 2500);

    // ─── 导航（内置 agent 已删；navigate 走下方 ne:command / pollNavIntents 意图桥）───
    function handleNavigate(url) {
        if (typeof navigateTo === 'function') navigateTo(url);
        else window.location.href = url;
    }
    // 全局唯一 ne:command 监听，委托给各页面注册的单槽位 window.onnecommand（建书向导等）
    window.addEventListener('ne:command', function(e) {
        if (typeof window.onnecommand === 'function') window.onnecommand(e);
    });
    // 建书向导命令桥：等「向导 DOM 就绪」后 dispatch（≤10s，给 SPA navigate 加载留余量）。
    // 就绪信号用 #wz-idea 存在而非 __neWizardReady__ 标志——SPA navigate 不触发 unload，
    // 标志会在离开向导页后残留，导致在别的页误派发。
    function dispatchCommand(it) {
        var tries = 0;
        (function poll() {
            if (window.WZ && document.getElementById('wz-idea')) {
                window.dispatchEvent(new CustomEvent('ne:command', {
                    detail: { cmd: it.cmd, args: it.args || {} } }));
            } else if (++tries <= 100) {
                setTimeout(poll, 100);
            } else {
                if (typeof showToast === 'function')
                    showToast('建书向导未就绪（请确认已打开 /books/start 后重试）', 'error');
            }
        })();
    }

    // ─── 发送（全服务单任务：busy 时先打断旧的再排队）───
    function setSendEnabled(on) {
        sendBtn.disabled = !on;
        sendBtn.textContent = on ? '发送' : '…';
        // 停止按钮与发送态联动：busy（send 禁用）时显示，idle 时隐藏复位
        if (stopBtn) {
            stopBtn.style.display = on ? 'none' : '';
            stopBtn.disabled = false;
            stopBtn.textContent = '⏹ 停止';
        }
    }
    function startTask(text) {
        busy = true;
        setSendEnabled(false);
        removeRunningBanner();   // 新任务接管：清掉恢复期的「后台运行中」卡（SSE 实时流展示）
        consumeSSE({ messages: history }).catch(function(err) {
            addMsg('assistant', '⚠️ 请求失败：' + err.message);
            if (pendingTask) {
                var pt = pendingTask; pendingTask = null;
                renderTaskStart(pt.text, pt.opts || {});
                startTask(pt.text);
            } else {
                busy = false;
                setSendEnabled(true);
            }
        });
    }
    // 任务卡（「让 Agent 构建」/ 写作台技能卡触发时替代用户气泡展示，任务文本仍进 history 供 SSE 取）
    function addBuildCard(text, label) {
        var card = el('div', 'agent-tool-card');
        card.appendChild(el('div', 'agent-tool-head', label || '🚀 建书任务'));
        var body = el('div', 'agent-tool-detail', text || '');
        body.style.display = 'block';
        card.appendChild(body);
        chat.appendChild(card);
        scrollBottom();
        return card;
    }
    // 渲染任务卡/气泡（由 agentSendTask 或 done 接力调用；busy 排队时等上一个任务被打断才渲染）
    function renderTaskStart(text, opts) {
        if (opts && opts.card) addBuildCard(text, opts.cardLabel);
        else addMsg('user', text);
    }
    function agentSendTask(text, opts) {
        var taskText = String(text || '').trim();
        if (!taskText) return;
        // card 标记：刷新后 restore 时渲染为卡片而非「你」气泡（SSE 后端只看 role/content，card 无副作用）
        history.push({ role: 'user', content: taskText, card: !!(opts && opts.card),
                       label: (opts && opts.cardLabel) || undefined });
        saveHistory(history);
        input.value = '';
        if (busy) {
            // 先打断当前任务，新任务卡等当前任务 done（被打断）后再渲染并启动 —— 严格先打断后开始
            pendingTask = { text: taskText, opts: opts || {} };
            fetch('/api/agent/chat/cancel', { method: 'POST' }).catch(function() {});
        } else {
            renderTaskStart(taskText, opts || {});
            startTask(taskText);
        }
    }
    // 供向导「让 Agent 构建」按钮 / 侧栏统一调用
    window.agentSendTask = agentSendTask;

    function send() {
        var text = (input.value || '').trim();
        if (!text) return;
        agentSendTask(text);
    }

    sendBtn.addEventListener('click', send);
    // 停止按钮（类似 Ctrl+C）：打断当前 dsh 任务，UI 复位由 SSE 的 done 事件接管
    if (stopBtn) stopBtn.addEventListener('click', function() {
        if (!busy) return;
        stopBtn.disabled = true;              // 防连点
        stopBtn.textContent = '停止中…';
        fetch('/api/agent/chat/cancel', { method: 'POST' }).catch(function() {});
        // 兜底：正常由 done 事件复位 busy；若 SSE 流异常卡死，超时强制复位防发送永久禁用
        setTimeout(function() {
            if (busy) { busy = false; setSendEnabled(true); }
        }, 8000);
    });
    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    clearBtn.addEventListener('click', function() {
        history = [];
        saveHistory(history);
        chat.innerHTML = '';
        fetch('/api/agent/task-events/clear', { method: 'POST' }).catch(function() {});   // 清服务器事件存储，防清空后旧工具卡回显
        addMsg('assistant', '对话已清空。有什么可以帮你？');
    });

    // 初始欢迎语（开篇语常驻：无论是否有历史都置顶） + 恢复会话历史（card 标记 → 渲染卡片）
    function renderHistory() {
        chat.innerHTML = '';
        addMsg('assistant', '👋 我是 NovelEngine 的 Agent，可以帮你完成从建书到上架的全部创作流程。\n试试：\n· 「创建一本都市爽文 by 枫落」\n· 「给 book_001 生成完整大纲」\n· 「续写 book_001，写下一个桥段」\n· 「打开书库看看」');
        for (var i = 0; i < history.length; i++) {
            if (history[i].card) addBuildCard(history[i].content, history[i].label);
            else addMsg(history[i].role, history[i].content);
        }
    }
    renderHistory();
    loadRestoredTaskView();   // 刷新/切页后一次性拉取服务器临时存储瞬时还原工具卡流
    refreshRunningBar();      // 刷新后还原「任务运行中」非阻塞指示

    // ─── 刷新/切页瞬时还原：一次性加载服务器临时存储（task-events）渲染全部工具卡 ───
    // 无轮询、无 busy 重建态、无「检查后台任务」提示。后台任务是否在跑不再感知——
    // 用户下次发消息时 run_dsh_task 内部自动打断，无死锁。活跃 SSE 会话的实时渲染不受影响。
    function removeRunningBanner() {
        // 历史已无横幅卡；保留函数名供 startTask/pageshow 调用（无副作用）
    }

    function loadRestoredTaskView() {
        if (!history.length) return;   // 无对话则不拉（新 tab / 已清空）
        var seen = {};
        fetch('/api/agent/task-events')
            .then(function(r) { return r.json(); })
            .then(function(ld) {
                if (!ld || !ld.ok) return;
                (ld.events || []).forEach(function(evt) {
                    if (evt.type === 'tool_call') {
                        if (seen[evt.callId]) return;
                        seen[evt.callId] = true;
                        addToolCardFor(evt.name, evt.args, evt.callId);
                    } else if (evt.type === 'tool_result') {
                        var run = (evt.callId && toolCards[evt.callId]) ? toolCards[evt.callId] : null;
                        if (evt.callId) delete toolCards[evt.callId];
                        if (run) finishToolCard(run, (evt.ok ? '✅ ' : '❌ ') + zhSummary(run.tool, run.args, evt.summary));
                    }
                });
                scrollBottom();
            })
            .catch(function() {});
    }

    // ─── 运行状态非阻塞指示：刷新/切页后若后台任务仍在跑，对话最上方插一条「⏳ 运行中 🛑 停止」 ───
    // 不置 busy（不锁发送框）——用户随时可发新消息，run_dsh_task 内部自动打断后台任务，无死锁。
    // 无定时轮询：靠启动时一次 + 页面重新获得焦点时重查（后台任务结束、切回页面即自动移除）。
    var runningBarEl = null;

    function refreshRunningBar() {
        fetch('/api/agent/chat/status')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.ok) return;
                if (d.running && !runningBarEl) {
                    var bar = el('div', 'agent-tool-card running');
                    var head = el('div', 'agent-tool-head', '⏳ 任务运行中');
                    var body = el('div', 'agent-tool-detail', '');
                    body.style.display = 'block';
                    var stopBtn = el('button', 'small', '🛑 停止');
                    stopBtn.onclick = function() {
                        fetch('/api/agent/chat/cancel', { method: 'POST' }).catch(function() {});
                    };
                    body.appendChild(stopBtn);
                    bar.appendChild(head);
                    bar.appendChild(body);
                    chat.insertBefore(bar, chat.firstChild);   // 置于对话最上方
                    runningBarEl = bar;
                } else if (!d.running && runningBarEl) {
                    if (runningBarEl.parentNode) runningBarEl.parentNode.removeChild(runningBarEl);
                    runningBarEl = null;
                }
            })
            .catch(function() {});
    }
    window.addEventListener('focus', refreshRunningBar);
    document.addEventListener('visibilitychange', function() {
        if (!document.hidden) refreshRunningBar();
    });

    // bfcache（前进/后退）恢复：清切页前的陈旧状态（死 SSE、卡住的 busy/工具卡），
    // 重渲染历史并感知后台任务。整页重载不触发本监听，靠启动时兜底。
    window.addEventListener('pageshow', function(e) {
        if (!e.persisted) return;
        busy = false;
        pendingTask = null;
        currentToolRun = null;
        toolCards = {};
        toolCardOrder = [];
        setSendEnabled(true);   // 复位发送/停止按钮（bfcache 恢复，防陈旧 busy 卡输入）
        removeRunningBanner();
        renderHistory();
        loadRestoredTaskView();
        refreshRunningBar();
    });
})();
