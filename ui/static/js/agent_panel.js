// Agent 聊天助手面板（OpenClaw 式）：侧栏对话，Agent 通过 function calling 操作引擎并导航页面。
// 后端 /api/agent/chat（SSE）。对话历史仅存 user/assistant 文本，工具步骤卡临时展示不入历史。
// 版本标记：新 JS（事件流实时工具卡）会在控制台打印 v3；旧 JS 无此输出——用于排查浏览器缓存。
console.log('[agent-panel] v29 delegation-tree');
(function() {
    var chat = document.getElementById('agent-chat');
    var input = document.getElementById('agent-input');
    var sendBtn = document.getElementById('agent-send');
    var clearBtn = document.getElementById('agent-clear');
    var stopBtn = document.getElementById('agent-stop');
    var debugBtn = document.getElementById('agent-debug');
    var tokenFlowEl = document.getElementById('agent-token-flow');
    var toolsLog = document.getElementById('agent-tools-log');
    if (!chat || !input || !sendBtn) return;   // 布局缺失则静默跳过

    var HISTORY_KEY = 'ne_agent_history';
    var DEBUG_KEY = 'ne_agent_debug';           // 调试模式开关（localStorage 持久化）
    var HISTORY_LIMIT = 40;
    var busy = false;
    var sessionTokens = 0;                      // 当前任务累计 token 流量（每个 tool_call 的 usage 相加；新任务/清空重置）
    var activeSse = false;                      // 是否有活跃 SSE 会话（活跃时 busy 由事件流管理；刷新后无 SSE 则区分后台任务）
    var pendingTask = null;                     // busy 时排队待发任务（done 后接力）
    var activeTaskOptions = {};                 // 当前任务的流程模式/忙碌策略（随 SSE 请求传递）
    var currentToolRun = null;                  // 当前工具卡引用
    var toolPollTimer = null;                   // 工具日志轮询定时器
    var toolCards = {};                         // sessionId:callId → 工具卡（父子 Agent 事件流配对）
    var delegationCards = {};                   // delegation_id → 委派父卡对象（树状折叠组）
    var _buildCards = [];                       // 任务卡（建书/写作）列表：done 时移除其停止按钮
    var toolCardOrder = [];                     // 工具卡创建顺序（上限裁剪用）
    var taskStartedAt = 0;                      // 当前任务起始时间（unix 秒）：SSE 断线后补渲染 task_events 的 since
    var renderedCallIds = {};                   // 已渲染过的工具卡 callId：断线补渲染防重
    var recovering = false;                     // SSE 断线自愈进行中（防并发触发）
    var TOOL_CARD_LIMIT = 20;                   // 对话页签工具卡上限（防 DOM 膨胀）
    var llmCards = [];                          // LLM 调用调试卡 DOM 顺序（上限裁剪用）
    var LLM_CARD_LIMIT = 10;                    // 调试卡上限（体积大，比工具卡更保守）
    var expandedLog = {};                       // 工具日志页签展开态：按记录 time 为 key（3s 轮询重渲染后保留）

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
        drive_ui: '驱动建书向导',
        export_book: '导出投稿包', extend_outline: '续写故事线',
        fill_plots: '填充情节段', generate_book_meta: '生成书名+简介', generate_characters: '生成角色',
        generate_core_conflict: '生成核心矛盾', generate_factions: '生成势力', generate_full_outline: '生成完整大纲',
        generate_outlines: '生成大纲序列', generate_rest_world: '补全其余世界观', generate_title: '生成书名',
        generate_world: '生成世界观', get_book_detail: '读取书详情', get_book_state: '读取书状态',
        get_build_status: '读取建书状态', get_storyline: '读取故事线', list_books: '列出书库',
        mark_finished: '标记完本', navigate: '页面跳转', outline_agent: '大纲助手',
        arc_material_candidates: '取选材候选', publish_book: '上架', publish_check: '上架检查',
        query_characters: '查角色原型', query_gags: '查笑点库', query_plots: '查情节段库',
        query_profiles: '查笔名档案', query_arc_library: '查情节弧库',
        save_basic_info: '保存基础设定', world_candidates: '生成世界观候选',
        write_chapter: '写章节', write_next_bridge: '写下一情节段',
        save_plot_draft: '保存情节段', save_chapter_text: '保存整章',
        save_outlines: '保存大纲', save_book_meta: '保存书名简介',
        skill: '技能', chapter_quality_gate: '章节质量门禁',
        discover_hot: '侦察热榜', list_rankings: '榜单分类', fetch_novel: '抓取小说',
        list_crawled_novels: '已抓取书库', read_crawled_novel: '读抓取书', ingest_library_assets: '提取入库',
        prepare_plot_run: '准备情节段运行上下文'
    };
    var CMD_ZH = {
        set_world: '写入世界观', set_characters: '写入角色', set_candidates: '填入候选',
        pick_candidate: '选中候选', set_field: '填写字段', set_tags: '设置标签',
        next: '下一步', prev: '上一步', reset: '重置向导', submit: '提交建书',
        skip_candidates: '跳过候选', load_candidates: '加载候选', fill_world: '重新补全',
        set_picks: '记录选材', set_replan_preview: '暂存续规划预览'
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
        mode: '模式', plot: '情节段', plots: '情节段', structure: '结构', structures: '模板',
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
            // 服务端已按 ok 生成摘要（失败时是「失败：…」）；直接复用，别在这里无条件
            // 改写成成功文案，否则失败会被显示成「已暂存续规划预览」。
            if (summary) return summary;
            var cmd = (args && args.cmd) || '';
            return '已' + (CMD_ZH[cmd] || cmd || '执行向导命令');
        }
        if (!summary) return '';
        try {
            var obj = JSON.parse(summary);
            if (obj && typeof obj === 'object') return JSON.stringify(zhKeys(obj), null, 1);
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

    // Agent 回复按 markdown 渲染（markdown.js 提供 window.renderMarkdown，先转义再解析，XSS 安全）；
    // 库缺失/异常时兜底为转义纯文本（保留换行），与旧行为一致。
    function md(text) {
        if (typeof window.renderMarkdown === 'function') {
            try { return window.renderMarkdown(text); } catch (e) {}
        }
        return escapeHtml(text == null ? '' : String(text)).replace(/\n/g, '<br>');
    }

    function addMsg(role, text) {
        var wrap = el('div', 'agent-msg ' + (role === 'user' ? 'user' : 'assistant'));
        wrap.appendChild(el('div', 'agent-msg-label', role === 'user' ? '你' : '🤖 Agent'));
        var body = el('div', 'agent-msg-body');
        if (role === 'assistant') body.innerHTML = md(text);   // agent 回复渲染 markdown
        else body.textContent = text || '';                    // 用户消息保持纯文本
        wrap.appendChild(body);
        chat.appendChild(wrap);
        scrollBottom();
        return wrap;
    }

    function formatDur(ms) {
        return ms < 1000 ? Math.round(ms) + 'ms' : (ms / 1000).toFixed(1) + 's';
    }

    function addToolCard(tool, args) {
        var card = el('div', 'agent-tool-card');
        var head = el('div', 'agent-tool-head');
        head.title = '点击展开/收起详情';
        var label = el('span', 'agent-tool-head-label', '🔧 ' + escapeHtml(toolLabel(tool, args)));
        var meta = el('span', 'agent-tool-head-meta', '');   // 第一行右侧：⏱ 运行时长 · token 用量
        // 卡片内停止按钮：打断当前 Agent 任务（复用全局 cancel 通道），工具完成/会话结束自动移除
        var stop = el('button', 'agent-tool-stop', '⏹ 停止');
        stop.type = 'button';
        stop.title = '打断当前 Agent 任务';
        stop.addEventListener('click', function (e) {
            e && e.stopPropagation();   // 不触发头部展开/折叠
            stop.disabled = true; stop.textContent = '停止中…';
            fetch('/api/agent/chat/cancel', { method: 'POST' }).catch(function () {});
        });
        var detail = el('div', 'agent-tool-detail', '');
        // 默认折叠成两行（头部 + 单行结果摘要，参数默认隐藏）；点击头部展开/收起全部，参数首次展开时懒加载
        head.onclick = function () {
            var open = card.classList.toggle('open');
            if (open && args && typeof args === 'object' && Object.keys(args).length && !detail.dataset.filled) {
                detail.dataset.filled = '1';
                // 参数全量展示（.agent-tool-detail 有 max-height + overflow 滚动容器，长内容可滚动查看不截断）
                detail.textContent = JSON.stringify(zhKeys(args), null, 2);
            }
        };
        head.appendChild(label);
        head.appendChild(stop);
        head.appendChild(meta);
        var status = el('div', 'agent-tool-status', '运行中…');
        card.appendChild(head);
        card.appendChild(detail);
        card.appendChild(status);
        chat.appendChild(card);
        scrollBottom();
        var run = { card: card, status: status, meta: meta, stopBtn: stop, t0: performance.now(), ts0: null, tool: tool, args: args, timer: null };
        // 运行中实时计时：活跃卡用 performance 基；刷新重建卡 run.ts0=事件 ts，用 Date.now 基算真实已用时长。
        // 运行中只显示 ⏱（token 是决策那轮已消耗的固定值，工具完成时才与最终时长一起显示，避免「token 已出现却仍运行中」误解）。
        run.timer = setInterval(function() {
            if (!run.meta) return;
            var ms = run.ts0 ? (Date.now() / 1000 - run.ts0) * 1000 : (performance.now() - run.t0);
            run.meta.textContent = '⏱ ' + formatDur(ms);
        }, 1000);
        return run;
    }

    function formatTokens(u) {
        if (!u) return '';
        var n = (u.input || 0) + (u.output || 0) + (u.cache_read || 0) + (u.cache_write || 0);
        return n >= 1000 ? (n / 1000).toFixed(1) + 'k tokens' : n + ' tokens';
    }

    // 实时 token 流量：每个 tool_call 的 usage 累加到当前任务。
    // 显示用平滑 count-up 滚动（每帧朝目标渐进），模拟流量滚动而非每次推送跳变；新增时脉冲高亮。
    var _tokenFlowShown = 0;
    var _tokenFlowRaf = null;
    var _tokenFlowPulseT = null;
    var _proxyActive = false;   // 代理有值（total>0）→ 事件累计不叠加，避免双计
    var _liveLlml = null;       // 「LLM 生成中」实时行（每步 LLM 流式期间实时滚动的载体）
    function _renderTokenFlow() {
        if (tokenFlowEl) tokenFlowEl.textContent = '⚡ '
            + (_tokenFlowShown >= 1000 ? (_tokenFlowShown / 1000).toFixed(1) + 'k' : _tokenFlowShown) + ' tokens';
    }
    function _animateTokenFlow() {
        if (_tokenFlowShown === sessionTokens) { _tokenFlowRaf = null; return; }
        var d = sessionTokens - _tokenFlowShown;
        _tokenFlowShown += Math.max(1, Math.ceil(Math.abs(d) * 0.2)) * (d < 0 ? -1 : 1);
        if (Math.abs(sessionTokens - _tokenFlowShown) <= 1) _tokenFlowShown = sessionTokens;
        _renderTokenFlow();
        _tokenFlowRaf = requestAnimationFrame(_animateTokenFlow);
    }
    // 实时 token 流量：500ms 轮询本地 API 代理检测器（show-me-the-story tokenPoll 模式）。
    // total 含流式中 pending → LLM 调用期间状态栏实时滚动；pending>0 时在聊天流里
    // 显示「⏳ LLM 生成中」实时行（每步 LLM 的 token 从开始到结束实时增长，随后落工具卡）。
    function pollTokenUsage() {
        fetch('/api/agent/token-usage')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.ok) return;
                var total = d.total || 0;
                _proxyActive = (total > 0);   // 代理有值 → 事件累计不叠加（addSessionTokens 双计防护）
                var pending = (d.pending_prompt || 0) + (d.pending_completion || 0);
                // LLM 流式进行中 → 显示/更新「LLM 生成中」实时行；结束（pending=0）移除
                if (pending > 0) {
                    if (!_liveLlml) {
                        _liveLlml = el('div', 'agent-llm-live', '');
                        chat.appendChild(_liveLlml);
                        scrollBottom();
                    }
                    _liveLlml.textContent = '⏳ LLM 生成中 · '
                        + formatTokens({ output: d.pending_completion || 0 });
                } else if (_liveLlml) {
                    _liveLlml.remove();
                    _liveLlml = null;
                }
                if (total > 0 && total !== sessionTokens) {   // 代理有值才覆盖（dsh 未走代理时保持事件累计）
                    sessionTokens = total;
                    if (tokenFlowEl) {
                        if (!_tokenFlowRaf) _animateTokenFlow();
                        // 脉冲只在每次 LLM 结束提交时（pending=0）闪，流式中只滚动不闪（避免连闪）
                        if (pending === 0) {
                            tokenFlowEl.classList.add('pulse');
                            clearTimeout(_tokenFlowPulseT);
                            _tokenFlowPulseT = setTimeout(function() { tokenFlowEl.classList.remove('pulse'); }, 400);
                        }
                    }
                }
            })
            .catch(function() {});
    }
    function addSessionTokens(usage) {
        if (_proxyActive) return;   // 代理已实时计数（含流式中 pending），事件累计叠加会双计；代理失效时照常兜底
        if (!usage) return;
        var n = (usage.input || 0) + (usage.output || 0) + (usage.cache_read || 0) + (usage.cache_write || 0);
        if (!n) return;
        sessionTokens += n;
        if (tokenFlowEl) {
            if (!_tokenFlowRaf) _animateTokenFlow();
            tokenFlowEl.classList.add('pulse');
            clearTimeout(_tokenFlowPulseT);
            _tokenFlowPulseT = setTimeout(function() { tokenFlowEl.classList.remove('pulse'); }, 400);
        }
    }
    var tokenPollTimer = null;
    function startTokenPoll() {
        if (!tokenPollTimer) tokenPollTimer = setInterval(pollTokenUsage, 500);   // 500ms：流式中 token 实时滚动
        pollTokenUsage();
    }
    function resetTokenFlow() {
        sessionTokens = 0;
        _tokenFlowShown = 0;
        if (_tokenFlowRaf) { cancelAnimationFrame(_tokenFlowRaf); _tokenFlowRaf = null; }
        if (_liveLlml) { _liveLlml.remove(); _liveLlml = null; }   // 清掉残留的「LLM 生成中」行
        _renderTokenFlow();
        fetch('/api/agent/token-usage/clear', { method: 'POST' }).catch(function() {});   // 清零代理累计
    }

    function eventCallKey(evt) {
        if (!evt || !evt.callId) return '';
        return String(evt.sessionId || evt.session_id || 'root') + ':' + String(evt.callId);
    }

    // 委派主卡（Writer / Critic / Planner 父卡容器）
    function addDelegationCard(tool, args, delegationId, role) {
        var roleZh = {
            writer: '🤖 Writer · 创作子代理',
            critic: '🔎 Critic · 规则评审子代理',
            planner: '🧭 Planner · 故事线规划子代理'
        }[role] || ('🤖 ' + (role || '子代理') + ' · 委派运行');

        var card = el('div', 'agent-delegation-card');
        var head = el('div', 'agent-delegation-head');
        head.title = '点击展开/折叠内部步骤';
        var label = el('span', 'agent-tool-head-label', escapeHtml(roleZh));
        var meta = el('span', 'agent-tool-head-meta', '⏱ 0s');
        head.appendChild(label);
        head.appendChild(meta);

        var status = el('div', 'agent-tool-status', '正在执行子任务…');
        var childrenContainer = el('div', 'agent-delegation-children');

        head.onclick = function() {
            card.classList.toggle('open');
        };

        card.appendChild(head);
        card.appendChild(status);
        card.appendChild(childrenContainer);
        chat.appendChild(card);
        scrollBottom();

        var run = {
            card: card,
            status: status,
            meta: meta,
            children: childrenContainer,
            isDelegation: true,
            t0: performance.now(),
            ts0: null,
            subCount: 0,
            tool: tool,
            args: args,
            timer: null
        };
        run.timer = setInterval(function() {
            if (!run.meta) return;
            var ms = run.ts0 ? (Date.now() / 1000 - run.ts0) * 1000 : (performance.now() - run.t0);
            run.meta.textContent = '⏱ ' + formatDur(ms);
        }, 1000);

        if (delegationId) delegationCards[delegationId] = run;
        return run;
    }

    // 子代理内嵌卡片（挂在委派父卡内部）
    function addSubToolCard(tool, args, parentContainer) {
        var card = el('div', 'agent-subtool-card');
        var head = el('div', 'agent-tool-head');
        head.title = '点击展开/收起详情';
        var label = el('span', 'agent-tool-head-label', '├─ ⚙️ ' + escapeHtml(toolLabel(tool, args)));
        var meta = el('span', 'agent-tool-head-meta', '');
        var detail = el('div', 'agent-tool-detail', '');
        head.onclick = function(e) {
            e && e.stopPropagation();
            var open = card.classList.toggle('open');
            if (open && args && typeof args === 'object' && Object.keys(args).length && !detail.dataset.filled) {
                detail.dataset.filled = '1';
                detail.textContent = JSON.stringify(zhKeys(args), null, 2);
            }
        };
        head.appendChild(label);
        head.appendChild(meta);
        var status = el('div', 'agent-tool-status', '运行中…');
        card.appendChild(head);
        card.appendChild(detail);
        card.appendChild(status);
        parentContainer.appendChild(card);
        scrollBottom();

        var run = { card: card, status: status, meta: meta, t0: performance.now(), ts0: null, tool: tool, args: args, timer: null };
        run.timer = setInterval(function() {
            if (!run.meta) return;
            var ms = run.ts0 ? (Date.now() / 1000 - run.ts0) * 1000 : (performance.now() - run.t0);
            run.meta.textContent = '⏱ ' + formatDur(ms);
        }, 1000);
        return run;
    }

    // 事件流工具卡：按 sessionId:callId 建档，支持委派树挂载与超上限裁剪
    function addToolCardFor(name, args, callId, usage, sessionId, delegationId, role) {
        // 若为主 Agent 的委派调用：创建委派父卡
        if (name && name.indexOf('delegate_') === 0) {
            var delId = delegationId || ('dg_' + callId);
            var targetRole = name.replace('delegate_', '');
            var run = addDelegationCard(name, args, delId, targetRole);
            run.usage = usage || null;
            var key = callId ? String(sessionId || 'root') + ':' + String(callId) : '';
            if (key) { toolCards[key] = run; renderedCallIds[key] = true; }
            return run;
        }

        // 若为已知委派的子工具，挂在父卡折叠容器内
        var parentDel = (delegationId && delegationCards[delegationId]) ? delegationCards[delegationId] : null;
        var run = null;
        if (parentDel) {
            run = addSubToolCard(name, args, parentDel.children);
            parentDel.subCount = (parentDel.subCount || 0) + 1;
            parentDel.status.textContent = '执行中 · 已包含 ' + parentDel.subCount + ' 个内部动作';
        } else {
            run = addToolCard(name, args);
        }
        run.usage = usage || null;
        run.delegationId = delegationId || '';
        var key = callId ? String(sessionId || 'root') + ':' + String(callId) : '';
        if (key) { toolCards[key] = run; renderedCallIds[key] = true; }
        toolCardOrder.push(key || ('#' + toolCardOrder.length));
        if (toolCardOrder.length > TOOL_CARD_LIMIT) {
            var old = toolCardOrder.shift();
            var oldRun = toolCards[old];
            if (oldRun && oldRun.timer) { clearInterval(oldRun.timer); oldRun.timer = null; }
            if (oldRun && oldRun.card && oldRun.card.parentNode) oldRun.card.parentNode.removeChild(oldRun.card);
            delete toolCards[old];
        }
        return run;
    }

    // 工具卡收尾：清计时器；第一行右侧 meta 显示 ⏱ 时长 + token 用量；状态行只留摘要
    function finishToolCard(run, text, durMs) {
        if (!run || !run.status) return;
        if (run.timer) { clearInterval(run.timer); run.timer = null; }
        if (run.stopBtn) { try { if (run.stopBtn.parentNode) run.stopBtn.parentNode.removeChild(run.stopBtn); } catch (e) {} run.stopBtn = null; }
        var durStr = '';
        if (durMs !== undefined && durMs !== null) {
            durStr = '⏱ ' + formatDur(durMs);
        } else if (run.t0) {
            durStr = '⏱ ' + formatDur(performance.now() - run.t0);
        }
        var tok = run.usage ? ' · ' + formatTokens(run.usage) : '';
        if (run.meta) run.meta.textContent = durStr + tok;

        if (run.isDelegation) {
            var isOk = (text && text.indexOf('✅') === 0);
            run.status.textContent = isOk
                ? '✅ 委派完成 (共 ' + (run.subCount || 0) + ' 个步骤)'
                : '❌ 委派未通过或异常';
            run.status.className = isOk ? 'agent-tool-status ok' : 'agent-tool-status err';
            if (run.card) {
                run.card.classList.remove('ok', 'err');
                run.card.classList.add(isOk ? 'ok' : 'err');
            }
            return;
        }

        run.status.textContent = (text || '');
        var isSubOk = (text && text.indexOf('✅') === 0);
        run.status.className = isSubOk ? 'agent-tool-status ok' : 'agent-tool-status err';
        if (run.card) {
            run.card.classList.remove('ok', 'err');
            run.card.classList.add(isSubOk ? 'ok' : 'err');
        }
        // 若子工具失败，自动展开其所属父卡，让失败处直观可见
        if (!isSubOk && run.delegationId && delegationCards[run.delegationId]) {
            try { delegationCards[run.delegationId].card.classList.add('open'); } catch (e) {}
        }
    }
    // ─── LLM 调用调试卡（调试模式：每次 LLM 调用的提示词 / MCP 工具 / 返回 JSON 原文）───
    // 对应 events-runner emit 的 llm/call → bridge llm_call → handleEvent。三段独立折叠，
    // 内容一律 textContent 写入（防 HTML 注入）；体积大，上限比工具卡更保守。
    // 载荷在 events-runner 已服务端裁剪，这里再兜底一层（含刷新重建旧持久化事件），
    // 防止大上下文（含巨大工具结果）事件把浏览器 DOM/主线程撑死。
    function capText(s, n) {
        if (typeof s !== 'string') return s;
        return s.length <= n ? s : s.slice(0, n) + '\n…[已截断 +' + (s.length - n) + ' 字符]';
    }
    function capDeep(v, n) {
        if (Array.isArray(v)) return v.map(function(x) { return capDeep(x, n); });
        if (v && typeof v === 'object') {
            var out = {};
            for (var k in v) if (Object.prototype.hasOwnProperty.call(v, k)) out[k] = capDeep(v[k], n);
            return out;
        }
        return capText(v, n);
    }
    function capToolsLite(tools) {
        if (!Array.isArray(tools)) return tools;
        return tools.map(function(t) {
            if (!t || typeof t !== 'object') return t;
            return { name: t.name, description: capText(t.description, 300) };
        });
    }
    function addLlmCallCard(evt) {
        var req = evt.request || {};
        var msgs = req.messages || [];
        var tools = req.tools || [];
        var seq = evt.seq;   // 调试卡完整原文按需拉取用（storage/debug-prompts/<seq>.json）
        // 标题统一显示真实消息总数（events-runner 在 SSE 只发最近 12 条预览，total_messages 带未裁剪全长），
        // 随轮次累加递增；折叠精简只护 SSE/管线，展开提示词/返回JSON 段即 fetch seq 全量原文（全量消息）。
        var total = (typeof req.total_messages === 'number') ? req.total_messages : msgs.length;
        var card = el('div', 'agent-llm-card');
        var head = el('div', 'agent-llm-head');
        head.appendChild(el('span', 'agent-llm-head-label', '🤖 LLM 调用 · ' + (req.model || '?')));
        var budget = evt.input_budget || {};
        var budgetLabel = (budget.total_chars !== undefined)
            ? '📊 ' + budget.total_chars + ' chars ≈ ' + (budget.estimated_tokens || '—') + ' tok' : '';
        var meta = [];
        if (evt.usage) meta.push('⚡ ' + formatTokens(evt.usage));
        if (budgetLabel) meta.push(budgetLabel);
        head.appendChild(el('span', 'agent-llm-head-meta', meta.join(' · ')));
        card.appendChild(head);
        // kind：'prompt'/'response' 段支持展开时按需拉完整原文（初始仍是裁剪预览）；null 段只用预览
        var sections = [
            ['💬 提示词（system + ' + total + ' 条消息）',
             { system: capDeep(req.system, 2000) || '', messages: capDeep(msgs.slice(-12), 600) }, 'prompt'],
            ['🧰 MCP 工具（' + tools.length + ' 个）', capToolsLite(tools), null],
            ['📦 返回 JSON', evt.response !== undefined ? capDeep(evt.response, 1500) : null, 'response']
        ];
        var fullPres = {};   // kind → pre 元素：一次 fetch 后填充所有可加载段
        var loaded = false;  // 本卡是否已拉过完整原文（成功/失败均只拉一次）
        for (var i = 0; i < sections.length; i++) {
            var det = el('details', 'agent-llm-section');
            var title = sections[i][0];
            if (seq && sections[i][2]) title += ' · 展开加载全文';
            det.appendChild(el('summary', '', title));
            var pre = el('pre', '', '');
            try { pre.textContent = JSON.stringify(sections[i][1], null, 2); }
            catch (e) { pre.textContent = String(sections[i][1]); }
            det.appendChild(pre);
            // 首次展开 prompt/response 段 → 按 seq 拉完整原文替换预览（SSE 只发裁剪预览，防撑爆管线）
            if (seq && sections[i][2]) {
                fullPres[sections[i][2]] = pre;
                (function(det) {
                    det.addEventListener('toggle', function() {
                        if (!det.open || loaded) return;
                        loaded = true;
                        fetch('/api/agent/debug-prompt/' + seq)
                            .then(function(r) { return r.json(); })
                            .then(function(d) {
                                if (!d || !d.ok) return;
                                if (fullPres.prompt && d.request) {
                                    fullPres.prompt.textContent = JSON.stringify(
                                        { system: d.request.system, messages: d.request.messages }, null, 2);
                                }
                                if (fullPres.response && d.response !== undefined) {
                                    fullPres.response.textContent = JSON.stringify(d.response, null, 2);
                                }
                            })
                            .catch(function() {});
                    });
                })(det);
            }
            card.appendChild(det);
        }
        chat.appendChild(card);
        scrollBottom();
        llmCards.push(card);
        if (llmCards.length > LLM_CARD_LIMIT) {
            var old = llmCards.shift();
            if (old && old.parentNode) old.parentNode.removeChild(old);
        }
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
                var fmtCount = function(v) { return (v === null || v === undefined) ? '—' : v; };
                var html = '<div style="position:sticky;top:0;z-index:1;background:#161b22;font-size:12px;color:#8b949e;padding:4px 0 8px;margin-bottom:4px">'
                    + '注册 <strong>' + fmtCount(d.registry_tool_count) + '</strong> · 当前 MCP <strong>' + fmtCount(d.profile_mcp_tool_count)
                    + '</strong> · Host <strong>' + fmtCount(d.host_tool_count) + '</strong> · 调用 <strong>' + log.length
                    + '</strong> 次 · 成功 <span style="color:#3fb950">' + success
                    + '</span> 失败 <span style="color:#f85149">' + (log.length - success) + '</span>'
                    + ' <button class="small" onclick="window.loadToolLog()">🔄 刷新</button>'
                    + ' <button class="small" onclick="window.clearToolLog()">🗑 清空</button>'
                    + (log.length > DISPLAY_LIMIT ? ' <span style="color:#8b949e">仅显示最近 ' + DISPLAY_LIMIT + ' 条（共 ' + log.length + '）</span>' : '')
                    + '</div>';
                if (!log.length) {
                    html += '<div style="font-size:12px;color:#8b949e;padding:8px 4px">暂无外部 MCP 调用记录（由 Claude Code 经 MCP 驱动时产生）。</div>';
                }
                // 展开态保留条数上限，防止对象无限膨胀
                var ek = Object.keys(expandedLog);
                if (ek.length > 200) {
                    ek.slice(0, ek.length - 200).forEach(function(k) { delete expandedLog[k]; });
                }
                show.forEach(function(x) {
                    var open = expandedLog[x.time] ? ' open' : '';
                    html += '<div class="agent-tool-card ' + (x.ok ? 'ok' : 'err') + open + '" data-t="' + escapeHtml(x.time || '') + '">'
                        + '<div class="agent-tool-head">' + escapeHtml((x.time || '') + ' ' + (x.ok ? '✅' : '❌') + ' ' + toolLabel(x.tool, x.args))
                        + ' <span style="color:#8b949e;font-weight:normal">' + (x.duration_ms || 0) + 'ms</span></div>';
                    if (x.args && typeof x.args === 'object' && Object.keys(x.args).length) {
                        html += '<div class="agent-tool-detail">' + escArg(zhKeys(x.args)) + '</div>';
                    }
                    html += '<div class="agent-tool-status">' + (x.ok ? '' : '❌ ') + escapeHtml(zhSummary(x.tool, x.args, x.summary) || '') + '</div></div>';
                });
                toolsLog.innerHTML = html;
                // 渲染后滚动：跟随中→贴底（新工具自动可见）；翻旧记录→按比例还原位置
                if (nearBottom) toolsLog.scrollTop = toolsLog.scrollHeight;
                else toolsLog.scrollTop = Math.round(ratio * toolsLog.scrollHeight);
                toolsLog.querySelectorAll('.agent-tool-card').forEach(function(card) {
                    var head = card.querySelector('.agent-tool-head');
                    if (head) head.addEventListener('click', function() {
                        var isOpen = card.classList.toggle('open');
                        var t = card.getAttribute('data-t');
                        if (t) { if (isOpen) expandedLog[t] = true; else delete expandedLog[t]; }
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
    // 断线自愈：reader 结束/读错误但从未收到 done 事件（activeSse 仍 true）时，
    // 服务器侧 dsh 任务大概率还在跑（run_dsh_task 断连不杀子进程），前端从
    // task_events 补渲染已落盘卡片 + 查 status 恢复 busy——避免永久卡在「LLM 生成中」。
    function consumeSSE(body) {
        var readerStarted = false;
        return fetch('/api/agent/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        }).then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.body.getReader();
        }).then(function(reader) {
            readerStarted = true;
            var decoder = new TextDecoder();
            var buf = '';
            function pump() {
                return reader.read().then(function(res) {
                    if (res.done) {
                        // 正常结束时 handleEvent 的 done 已把 activeSse 置 false；仍 true = 提前断开
                        if (activeSse) recoverAfterDrop();
                        return;
                    }
                    buf += decoder.decode(res.value, { stream: true });
                    var lines = buf.split('\n');
                    buf = lines.pop();
                    for (var i = 0; i < lines.length; i++) {
                        var line = lines[i].trim();
                        if (line.indexOf('data:') !== 0) continue;
                        var data = line.slice(5).trim();
                        if (!data) continue;
                        try { handleEvent(JSON.parse(data)); }
                        catch (e) { if (window.console) console.error('SSE 事件处理失败', e, data); }
                    }
                    return pump();
                });
            }
            return pump();
        }).catch(function(err) {
            // 流中途读错误（连接异常重置）：自愈；初始 fetch 失败：抛给外层显示「请求失败」
            if (readerStarted && activeSse) { recoverAfterDrop(); return; }
            throw err;
        });
    }

    // ─── SSE 断线自愈：补渲染已落盘工具卡 + 按运行态恢复 busy ───
    // 1) 从 /api/agent/task-events 拉当前任务落盘事件，补齐断线后丢失的卡片（防重）；
    // 2) 有排队任务则接力（与 done 分支一致）；否则查 status：在跑保留停止按钮，否则复位。
    function recoverAfterDrop() {
        if (recovering) return;
        recovering = true;
        var p = fetch('/api/agent/task-events?since=' + (taskStartedAt || 0))
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var cards = (d && d.ok && d.events) ? d.events : [];
                cards.forEach(function(e) {
                    // Planner 内部事件只作后台诊断，断线补渲染时也不能复活成用户可见卡片
                    if (e.internal) return;
                    if (e.type === 'tool_call') {
                        if (e.callId && renderedCallIds[e.callId]) return;
                        var run = addToolCardFor(e.name, e.args, e.callId, e.usage,
                            e.sessionId || e.session_id, e.delegation_id, e.agent_role);
                        if (run) run.ts0 = e.ts;
                    } else if (e.type === 'tool_result') {
                        var run = toolCards[eventCallKey(e)] ? toolCards[eventCallKey(e)] : null;
                        if (e.callId) delete toolCards[eventCallKey(e)];
                        if (run) {
                            var durMs = (run.ts0 != null) ? (e.ts - run.ts0) * 1000 : undefined;
                            finishToolCard(run, (e.ok ? '✅ ' : '❌ ') + zhSummary(run.tool, run.args, e.summary), durMs);
                        }
                    } else if (e.type === 'llm_call') {
                        addLlmCallCard(e);
                    }
                });
                scrollBottom();
            })
            .catch(function() {});
        p.then(function() {
            if (pendingTask) {
                var pt = pendingTask; pendingTask = null;
                renderTaskStart(pt.text, pt.opts || {});
                startTask(pt.text, pt.opts || {});
                return undefined;
            }
            return fetch('/api/agent/chat/status').then(function(r) { return r.json(); }).catch(function() { return {}; });
        }).then(function(d) {
            if (d === undefined) return;   // 已接力排队任务
            if (d && d.ok && d.running) {
                busy = true;
                setSendEnabled(false);
                emitAgentState();
                addMsg('assistant', '⚠️ SSE 连接已断开，任务仍在后台运行。可点「⏹ 停止」中断，或刷新页面同步状态。');
            } else {
                if (activeTaskOptions.taskKind) {
                    window.dispatchEvent(new CustomEvent('ne:agent-task-event', { detail: {
                        taskKind: activeTaskOptions.taskKind,
                        event: {type: 'done', disconnected: true}
                    }}));
                }
                activeSse = false;
                busy = false;
                activeTaskOptions = {};
                setSendEnabled(true);
                settleInFlightCards();
                emitAgentState();
                addMsg('assistant', '⚠️ SSE 连接已断开，任务已结束。以上为服务器最近记录。');
            }
        }).catch(function() {
            // 兜底：绝不把 UI 永久卡在 busy
            activeSse = false;
            busy = false;
            setSendEnabled(true);
            emitAgentState();
        }).then(function() { recovering = false; });
    }

    function handleEvent(evt) {
        var t = evt.type;
        /* Planner 子 run 的内部事件（校验失败等）：错误已由 MCP isError 回给 Agent 自行修正，
           用户侧既不该建工具卡，也不该把它当成章级任务的失败——FSM 会重试。
           domain / ui_command / navigate / done 仍需照常处理。 */
        if (evt.internal && t !== 'ui_command' && t !== 'navigate') {
            if (t === 'domain') {
                window.dispatchEvent(new CustomEvent('ne:desk-refresh', { detail: evt }));
            } else if (t === 'build_draft_status') {
                window.dispatchEvent(new CustomEvent('ne:build-draft-status', { detail: evt }));
            }
            return;
        }
        if (activeTaskOptions.taskKind && (t === 'domain' || t === 'error' || t === 'done')) {
            window.dispatchEvent(new CustomEvent('ne:agent-task-event', { detail: {
                taskKind: activeTaskOptions.taskKind, event: evt
            }}));
        }
        if (t === 'subagent_start' || t === 'subagent_end') {
            window.dispatchEvent(new CustomEvent('ne:subagent-event', { detail: evt }));
            return;
        }
        if (t === 'progress') {
            var pLine = el('div', 'agent-progress-line');
            pLine.textContent = '⏳ ' + (evt.message || '');
            chat.appendChild(pLine);
            scrollBottom();
            setTimeout(function() {
                try { if (pLine.parentNode) pLine.parentNode.removeChild(pLine); } catch (e) {}
            }, 8000);
            return;
        }
        if (t === 'domain') {
            // WS6：写作相关工具成功 → 领域事件（chapter_changed/plot_run_changed/plan_committed）。
            // 只作为 UI 刷新信号转发（写作台/规划面板监听 ne:desk-refresh），非持久业务状态。
            window.dispatchEvent(new CustomEvent('ne:desk-refresh', { detail: evt }));
            return;
        }
        if (t === 'tool_call') {
            // dsh 核心实时推送：工具开始 → 建卡（usage = 该调用的真实 token 用量）
            if (_liveLlml) { _liveLlml.remove(); _liveLlml = null; }   // LLM 已结束，实时行让位给工具卡
            currentToolRun = addToolCardFor(evt.name, evt.args, evt.callId, evt.usage,
                evt.sessionId || evt.session_id, evt.delegation_id, evt.agent_role);
            addSessionTokens(evt.usage);   // 事件驱动累计（dsh 真实 usage）
        } else if (t === 'tool_result') {
            // 按 callId 配对卡；配不到就忽略（绝不 fallback 到别的卡，避免污染）。
            // navigate/drive_ui 的 tool/call 也会建卡，故正常情况都配得到。
            var resultKey = eventCallKey(evt);
            var run = (resultKey && toolCards[resultKey]) ? toolCards[resultKey] : null;
            if (resultKey) delete toolCards[resultKey];
            if (run) finishToolCard(run, (evt.ok ? '✅ ' : '❌ ') + zhSummary(run.tool, run.args, evt.summary));
        } else if (t === 'navigate') {
            handleNavigate(evt.url);            // dsh 调用 navigate → 实时切页
        } else if (t === 'ui_command') {
            dispatchCommand({ cmd: evt.cmd, args: evt.args || {} });  // drive_ui → 驱动建书向导
        } else if (t === 'llm_call') {
            // 调试模式：一次 LLM 调用 → 一张「LLM 调用」调试卡（提示词/工具/返回JSON），
            // 在它产生的工具卡之前渲染（assistant/message 先于 tool/call 到达）。
            if (_liveLlml) { _liveLlml.remove(); _liveLlml = null; }
            addLlmCallCard(evt);
        } else if (t === 'reply') {
            addMsg('assistant', evt.content);
            history.push({ role: 'assistant', content: evt.content, ts: Date.now() / 1000 });   // ts 供刷新后与卡片按时间交错
            saveHistory(history);
        } else if (t === 'notice') {
            // 非阻塞提示（如「主 Agent 编排未启用 → 本次走 legacy FSM」）：不是失败，
            // 但必须让用户看见——否则他会以为自己在跑新编排。同样写 history。
            addMsg('assistant', 'ℹ️ ' + (evt.message || ''));
            history.push({ role: 'assistant', content: 'ℹ️ ' + (evt.message || ''), ts: Date.now() / 1000 });
            saveHistory(history);
        } else if (t === 'error') {
            // 写进 history：否则刷新/切页后这条错误气泡消失，用户只看到一个"正常结束"的任务
            addMsg('assistant', '⚠️ ' + (evt.message || '发生错误'));
            history.push({ role: 'assistant', content: '⚠️ ' + (evt.message || '发生错误'), ts: Date.now() / 1000 });
            saveHistory(history);
            if (currentToolRun) finishToolCard(currentToolRun, '❌ 失败');
        } else if (t === 'build_draft_status') {
            // 建书薄工具的压缩结果：转发给向导页（进步 3 / 刷新 / 校验失败都要能看到）。
            // 注意**不能**放进 busy 守卫里——它本来就是在 agent 忙的时候产生的。
            window.dispatchEvent(new CustomEvent('ne:build-draft-status', { detail: evt }));
        } else if (t === 'done') {
            if (_liveLlml) { _liveLlml.remove(); _liveLlml = null; }   // 会话结束清实时行
            // 任务结束：移除任务卡（建书/写作）上的停止按钮
            for (var bi = 0; bi < _buildCards.length; bi++) {
                try { if (_buildCards[bi].stop && _buildCards[bi].stop.parentNode) _buildCards[bi].stop.parentNode.removeChild(_buildCards[bi].stop); } catch (e) {}
            }
            _buildCards = [];
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
            activeSse = false;   // 当前 SSE 会话结束（done）
            if (pendingTask) {
                // 接力排队任务（busy 保持 true）：先打断已 done，此刻才渲染新任务卡并启动 —— 严格先打断后开始
                var pt = pendingTask; pendingTask = null;
                renderTaskStart(pt.text, pt.opts || {});
                startTask(pt.text, pt.opts || {});
            } else {
                busy = false;
                activeTaskOptions = {};
                setSendEnabled(true);
                emitAgentState();   // 页面（写作台）感知空闲
            }
        }
    }

    // ─── navigate 外部驱动桥（P1b）：轮询 MCP 写入的导航意图，取到即翻页/切页签 ───
    var navTimer = null;
    // 消费意图队列。onlyCmds 非空时只处理指定的 ui_command（busy 中用）。
    // dsh 会话中（busy）所有向导命令（set_field/set_world/set_characters/set_outline/next/submit…）
    // 均由 drive_ui → SSE ui_command 实时推送，轮询消费会双触发——busy 时只取走清空队列、
    // 不派发，残留由 done 后 C1 drain 丢弃。
    // ⚠️ 因此这条队列**不能**用来做服务端内部的数据投影（没有 SSE 伴随事件、busy 时必被丢）。
    // 步 3 草稿改为页面从 canonical 拉取（见 start_book.html 的 syncCanonicalDraft）。
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
        if (busy) { consumeNavIntents([]); return; }   // dsh 会话中：命令全走 SSE，只清队列不派发
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
    // 页面命令桥：优先分发给当前页的 window.onnecommand（侦察/提取页等非向导页），
    // 否则退回建书向导路径（等 #wz-idea 就绪，≤10s）。
    // 守卫：__nePageReceiver 表示「当前页声明接收页面命令」；#wz-idea 存在表示「当前是建书向导」。
    // 两者都判——SPA navigate 不触发 unload，离开向导后 window.onnecommand 仍可能是向导处理器，
    // 用 #wz-idea 存在与否区分目标页，避免 set_review 等命令被向导守卫静默吞掉。
    function dispatchCommand(it) {
        var cmd = (it && it.cmd) || '';
        var args = (it && it.args) || {};
        var onWizard = !!document.getElementById('wz-idea');
        if (window.__nePageReceiver && !onWizard
                && typeof window.onnecommand === 'function') {
            window.dispatchEvent(new CustomEvent('ne:command', {detail: {cmd: cmd, args: args}}));
            return;
        }
        var tries = 0;
        (function poll() {
            if (window.WZ && document.getElementById('wz-idea')) {
                window.dispatchEvent(new CustomEvent('ne:command', {
                    detail: { cmd: cmd, args: args } }));
            } else if (window.__nePageReceiver && !document.getElementById('wz-idea')
                    && typeof window.onnecommand === 'function') {
                window.dispatchEvent(new CustomEvent('ne:command', {detail: {cmd: cmd, args: args}}));
            } else if (++tries <= 100) {
                setTimeout(poll, 100);
            } else {
                if (typeof showToast === 'function')
                    showToast('页面命令接收器未就绪（请确认已打开对应页面后重试）', 'error');
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
    function startTask(text, opts) {
        opts = opts || {};
        activeTaskOptions = opts;
        busy = true;
        resetTokenFlow();                    // 新任务：token 流量归零
        activeSse = true;                    // 活跃 SSE 会话开始
        taskStartedAt = Date.now() / 1000 - 3;   // 略提前：task_events 的 ts 是服务端 time.time()，本地同机对齐
        setSendEnabled(false);
        emitAgentState();                    // 页面（写作台）感知运行态
        removeRunningBanner();   // 新任务接管：清掉恢复期的「后台运行中」卡（SSE 实时流展示）
        var body = { messages: history, debug: isDebugOn() };
        if (opts.flowMode) body.flow_mode = opts.flowMode;
        if (opts.bookId) body.book_id = opts.bookId;
        if (opts.busyPolicy) body.busy_policy = opts.busyPolicy;
        if (opts.taskKind) body.task_kind = opts.taskKind;
        consumeSSE(body).catch(function(err) {
            addMsg('assistant', '⚠️ 请求失败：' + err.message);
            if (activeTaskOptions.taskKind) {
                var failedTask = activeTaskOptions.taskKind;
                window.dispatchEvent(new CustomEvent('ne:agent-task-event', { detail: {
                    taskKind: failedTask, event: {type: 'error', message: err.message || '请求失败'}
                }}));
                window.dispatchEvent(new CustomEvent('ne:agent-task-event', { detail: {
                    taskKind: failedTask, event: {type: 'done'}
                }}));
            }
            if (pendingTask) {
                var pt = pendingTask; pendingTask = null;
                renderTaskStart(pt.text, pt.opts || {});
                startTask(pt.text, pt.opts || {});
            } else {
                activeSse = false;
                busy = false;
                activeTaskOptions = {};
                setSendEnabled(true);
                emitAgentState();
            }
        });
    }
    // 任务卡（「让 Agent 构建」/ 写作台技能卡触发时替代用户气泡展示，任务文本仍进 history 供 SSE 取）
    function addBuildCard(text, label) {
        var card = el('div', 'agent-tool-card agent-task-card');   // 任务卡：body 常显，无折叠（头部箭头需 CSS 隐藏）
        var head = el('div', 'agent-tool-head');
        head.appendChild(el('span', 'agent-tool-head-label', label || '🚀 建书任务'));
        var stop = el('button', 'agent-tool-stop', '⏹ 停止');
        stop.type = 'button';
        stop.title = '打断当前 Agent 任务';
        stop.addEventListener('click', function () {
            stop.disabled = true; stop.textContent = '停止中…';
            fetch('/api/agent/chat/cancel', { method: 'POST' }).catch(function () {});
        });
        head.appendChild(stop);
        card.appendChild(head);
        var body = el('div', 'agent-tool-detail', text || '');
        body.style.display = 'block';
        card.appendChild(body);
        chat.appendChild(card);
        scrollBottom();
        _buildCards.push({ card: card, stop: stop });
        return card;
    }
    // 渲染任务卡/气泡（由 agentSendTask 或 done 接力调用；busy 排队时等上一个任务被打断才渲染）
    function renderTaskStart(text, opts) {
        if (opts && opts.card) addBuildCard(text, opts.cardLabel);
        else addMsg('user', text);
    }
    function agentSendTask(text, opts) {
        opts = opts || {};
        var taskText = String(text || '').trim();
        if (!taskText) return false;
        // 写作台章级入口不允许取消/排队别的 Agent 任务；普通侧栏仍保留原接力语义。
        if (opts.busyPolicy === 'reject' && (busy || activeSse)) return false;
        // card 标记：刷新后 restore 时渲染为卡片而非「你」气泡（SSE 后端只看 role/content，card 无副作用）
        history.push({ role: 'user', content: taskText, card: !!opts.card,
                       label: opts.cardLabel || undefined,
                       ts: Date.now() / 1000 });   // ts 供刷新后与卡片按时间交错
        saveHistory(history);
        input.value = '';
        if (busy && activeSse) {
            // 活跃 SSE 会话中：先打断当前任务，新任务卡等当前任务 done（被打断）后再渲染并启动 —— 严格先打断后开始
            pendingTask = { text: taskText, opts: opts };
            fetch('/api/agent/chat/cancel', { method: 'POST' }).catch(function() {});
        } else {
            // 非活跃 SSE（刷新后的后台任务 / 空闲）：直接启动，run_dsh_task 内部自动打断后台任务，无死锁
            renderTaskStart(taskText, opts);
            startTask(taskText, opts);
        }
        return true;
    }
    // 供向导「让 Agent 构建」按钮 / 侧栏统一调用
    window.agentSendTask = agentSendTask;
    // 供页面（写作台等）感知/打断全局 agent 任务：busy 变化派发 ne:agent-state 事件
    window.agentIsBusy = function() { return busy; };
    window.agentStopTask = function() {
        fetch('/api/agent/chat/cancel', { method: 'POST' }).catch(function() {});
    };
    function emitAgentState() {
        window.dispatchEvent(new CustomEvent('ne:agent-state', { detail: { busy: !!busy } }));
    }

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
        // 刷新后（无活跃 SSE）：点停止后延时重查，后台任务取消则「⏹ 停止」自动消失
        setTimeout(refreshRunningState, 1500);
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
        renderedCallIds = {};
        resetTokenFlow();
        fetch('/api/agent/task-events/clear', { method: 'POST' }).catch(function() {});   // 清服务器事件存储，防清空后旧工具卡回显
        addMsg('assistant', '对话已清空。有什么可以帮你？');
    });
    // ─── 调试模式开关：🔍 按钮（localStorage 持久化；开启后每次 LLM 调用出「LLM 调用」调试卡）───
    function isDebugOn() { return localStorage.getItem(DEBUG_KEY) === '1'; }
    function renderDebugBtn() {
        if (!debugBtn) return;
        debugBtn.classList.toggle('active', isDebugOn());
        debugBtn.title = isDebugOn()
            ? '调试模式已开：每次 LLM 调用显示「提示词 / MCP 工具 / 返回JSON」'
            : '调试模式已关：LLM 调用不显示调试详情';
    }
    if (debugBtn) {
        debugBtn.addEventListener('click', function() {
            var on = !isDebugOn();
            localStorage.setItem(DEBUG_KEY, on ? '1' : '0');
            renderDebugBtn();
            addMsg('assistant', on
                ? '🔍 调试模式已开启 —— 后续每次 LLM 调用会显示「提示词 / MCP 工具 / 返回 JSON」。'
                : '🔍 调试模式已关闭。');
        });
        renderDebugBtn();
    }

    // 初始欢迎语（开篇语常驻：无论是否有历史都置顶）+ 按时间戳合并渲染会话（历史气泡 + 服务器存储卡片交错）。
    // 消息 ts 在 agentSendTask / reply 时写入历史；卡片 ts 为服务器 time.time()（localhost 与浏览器同源对齐）。
    function renderConversation() {
        chat.innerHTML = '';
        renderedCallIds = {};   // 全量重建：清掉断线补渲染用的防重记录
        addMsg('assistant', '👋 我是 NovelEngine 的 Agent，可以帮你完成从建书到上架的全部创作流程。\n试试：\n· 「创建一本都市爽文 by 枫落」\n· 「给 book_001 生成完整大纲」\n· 「续写 book_001，写下一个情节段」\n· 「打开书库看看」');
        fetch('/api/agent/task-events').then(function(r) { return r.json(); }).then(function(ld) {
            var cards = (ld && ld.ok && ld.events) ? ld.events : [];
            var items = [];
            history.forEach(function(m) {
                // 旧消息无 ts → 排最前（欢迎语之后、卡片之前），退回旧观感；新消息按 ts 与卡片交错
                items.push({ kind: 'msg', ts: (m.ts != null) ? m.ts : -Infinity, m: m });
            });
            cards.forEach(function(c) { items.push({ kind: 'card', ts: c.ts || 0, c: c }); });
            items.sort(function(a, b) {
                if (a.ts !== b.ts) return a.ts - b.ts;
                if (a.kind !== b.kind) return a.kind === 'card' ? -1 : 1;   // 同秒卡片先于消息
                return 0;
            });
            var seen = {};
            items.forEach(function(it) {
                if (it.kind === 'msg') {
                    if (it.m.card) addBuildCard(it.m.content, it.m.label);
                    else addMsg(it.m.role, it.m.content);
                } else {
                    var e = it.c;
                    // 与实时流同一规则：Planner 内部事件不重建为用户可见卡片
                    if (e.internal && e.type !== 'build_draft_status') return;
                    if (e.type === 'tool_call') {
                        if (seen[e.callId]) return;
                        seen[e.callId] = true;
                        var run = addToolCardFor(e.name, e.args, e.callId, e.usage);   // 重建卡：计时器用事件 ts 基
                        if (run) run.ts0 = e.ts;
                    } else if (e.type === 'tool_result') {
                        var run = toolCards[eventCallKey(e)] ? toolCards[eventCallKey(e)] : null;
                        if (e.callId) delete toolCards[eventCallKey(e)];
                        if (run) {
                            var durMs = (run.ts0 != null) ? (e.ts - run.ts0) * 1000 : undefined;
                            finishToolCard(run, (e.ok ? '✅ ' : '❌ ') + zhSummary(run.tool, run.args, e.summary), durMs);
                        }
                    } else if (e.type === 'llm_call') {
                        addLlmCallCard(e);
                    } else if (e.type === 'error') {
                        // 持久化的错误：刷新/切页后仍看得到（此前 error 只即时追加、不落盘，
                        // 用户回来只看得到一个"正常结束"的任务）
                        addMsg('assistant', '⚠️ ' + (e.message || '发生错误'));
                    } else if (e.type === 'build_draft_status') {
                        window.dispatchEvent(new CustomEvent('ne:build-draft-status', { detail: e }));
                    }
                }
            });
            scrollBottom();
            refreshRunningState();   // 重建完再查运行态：任务不在跑则把残留「运行中…」卡标记为结果未保存
        }).catch(function() {
            // 拉取失败兜底：只渲染历史气泡
            history.forEach(function(m) { if (m.card) addBuildCard(m.content, m.label); else addMsg(m.role, m.content); });
            scrollBottom();
        });
    }
    renderConversation();
    startTokenPoll();   // 实时 token 流量：2s 轮询本地 API 代理检测器
    refreshRunningState();    // 刷新后若后台任务在跑 → busy → 右下角「⏹ 停止」显现（运行状态）

    // ─── 刷新/切页瞬时还原：一次性加载服务器临时存储（task-events）渲染全部工具卡 ───
    // 无轮询、无 busy 重建态、无「检查后台任务」提示。后台任务是否在跑不再感知——
    // 用户下次发消息时 run_dsh_task 内部自动打断，无死锁。活跃 SSE 会话的实时渲染不受影响。
    function removeRunningBanner() {
        // 历史已无横幅卡；保留函数名供 startTask/pageshow 调用（无副作用）
    }

    // 任务不在跑时，把仍显示「运行中…」的工具卡（刷新时 in-flight、结果未持久化）标记为已结束未保存
    function settleInFlightCards() {
        Object.keys(toolCards).forEach(function(id) {
            var r = toolCards[id];
            if (r && r.timer) {
                clearInterval(r.timer);
                r.timer = null;
                r.status.textContent = '⚠️ 结果未保存（任务已结束）';
                r.status.className = 'agent-tool-status err';
                if (r.card) { r.card.classList.remove('ok', 'err'); r.card.classList.add('err'); }
            }
        });
    }

    // ─── 运行状态由右下角「⏹ 停止」按钮表示：busy 则它出现（无横幅、无轮询） ───
    // 刷新/切页后从状态端点恢复 busy；focus/visibilitychange/停止点击后重查。
    function refreshRunningState() {
        if (activeSse) return;   // 活跃 SSE 会话由事件流管理 busy，不干扰
        fetch('/api/agent/chat/status')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.ok) return;
                if (d.running) { busy = true; setSendEnabled(false); emitAgentState(); }   // →「⏹ 停止」显现
                else { busy = false; setSendEnabled(true); settleInFlightCards(); emitAgentState(); }
            })
            .catch(function() {});
    }
    window.addEventListener('focus', refreshRunningState);
    document.addEventListener('visibilitychange', function() {
        if (!document.hidden) refreshRunningState();
    });

    // bfcache（前进/后退）恢复：清切页前的陈旧状态（死 SSE、卡住的 busy/工具卡），
    // 重渲染历史并感知后台任务。整页重载不触发本监听，靠启动时兜底。
    window.addEventListener('pageshow', function(e) {
        if (!e.persisted) return;
        busy = false;
        activeSse = false;
        pendingTask = null;
        currentToolRun = null;
        Object.keys(toolCards).forEach(function(id) {
            var r = toolCards[id];
            if (r && r.timer) { clearInterval(r.timer); r.timer = null; }   // 清计时器防泄漏
        });
        toolCards = {};
        toolCardOrder = [];
        setSendEnabled(true);   // 复位发送/停止按钮（bfcache 恢复，防陈旧 busy 卡输入）
        removeRunningBanner();
        renderConversation();   // bfcache 恢复：按 ts 交错渲染（历史气泡 + 服务器存储卡片）
        refreshRunningState();
    });
})();
