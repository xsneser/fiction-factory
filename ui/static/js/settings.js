/**
 * 设置页 · 模型路由工作台
 *
 * 页面职责：管理多个 API 供应商（providers），并为每个子代理角色（subagents）
 * 指定"用哪个供应商的哪个模型、思考强度多少"。
 *
 * 分流机制（本页配置最终怎么生效）：
 *   每个角色的模型别名形如 `novelengine-route:writer`。dsh 侧的子代理用这个别名
 *   发请求，本地 token 代理（libraries/token_proxy.py）拦截后查本页保存的
 *   subagents[role]，替换成真实供应商的 base_url / api_key / model 再转发。
 *   所以**页面上永远只出现别名和模型名，不出现明文 key** —— key 只在服务端文件里。
 *
 * 状态管理：单一 `state` 对象 + 全量重渲染。页面规模小，重渲染比细粒度 diff 更好维护；
 * 只有正在输入的输入框需要避免被打断，所以表单控件都用 change/input 事件写回 state，
 * 不在每次按键时重渲染。
 */
(function () {
  'use strict';

  // ─── 常量 ──────────────────────────────────────────────────

  /** 可选的思考强度档位（default = 不干预，交给上游默认）。 */
  var EFFORTS = [
    ['default', '默认'],
    ['off', '关闭'],
    ['low', '低'],
    ['medium', '中'],
    ['high', '高'],
    ['max', '最大']
  ];

  /** 各角色在"子代理路由"主区展示的顺序。 */
  var MAIN_ROLES = ['orchestrator', 'writer', 'critic', 'planner', 'builder', 'scout', 'publisher', 'style'];

  /** 收在"高级设置"里的角色（用得少，但不该藏起来不可配）。 */
  var ADVANCED_ROLES = ['candidates', 'backend'];

  var ROUTE_PREFIX = 'novelengine-route:';

  /** 新建供应商时的预置模板（Base URL 只填域名，路径部分交给 url_strict）。 */
  var PRESETS = [
    { label: 'DeepSeek 官方', base_url: 'https://api.deepseek.com', model: 'deepseek-chat',
      wire: 'deepseek', models: ['deepseek-chat', 'deepseek-reasoner'] },
    { label: 'OpenAI 兼容中转', base_url: 'https://api.example.com/v1', model: 'gpt-4o',
      wire: 'openai', models: ['gpt-4o'] },
    { label: '本地 Ollama', base_url: 'http://127.0.0.1:11434/v1', model: 'qwen2.5:14b',
      wire: 'none', models: ['qwen2.5:14b'] },
    { label: '自定义', base_url: '', model: '', wire: 'default', models: [] }
  ];

  // ─── 状态 ──────────────────────────────────────────────────

  var state = null;      // 当前工作副本（保存前的所有编辑都落在这里）
  var saved = null;      // 服务端已保存的快照，用来算 dirty
  var activeProviderId = ''; // UI tab only; does not change the configured default
  var roleMeta = { order: [], titles: {}, descs: {} };
  var testStatus = {};   // provider_id -> {state:'testing'|'ok'|'err', text:'...'}

  // ─── 工具 ──────────────────────────────────────────────────

  function $(id) { return document.getElementById(id); }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'text') node.textContent = attrs[k];
        else if (k === 'html') node.innerHTML = attrs[k];
        else if (k === 'style') node.setAttribute('style', attrs[k]);
        else if (k.slice(0, 2) === 'on') node.addEventListener(k.slice(2), attrs[k]);
        else if (attrs[k] === true) node.setAttribute(k, '');
        else if (attrs[k] !== false && attrs[k] != null) node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  function deepEqual(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  /** 从 URL 里取 host（卡片上只展示域名，不展示完整密钥路径）。 */
  function hostOf(url) {
    try {
      var u = new URL(url);
      return u.host + (u.pathname && u.pathname !== '/' ? u.pathname : '');
    } catch (e) { return url || '—'; }
  }

  function toast(msg, type) {
    if (typeof window.showToast === 'function') window.showToast(msg, type);
    else console.log('[' + (type || 'info') + '] ' + msg);
  }

  function newId(seed) {
    var base = String(seed || 'provider').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-|-$/g, '');
    if (!base) base = 'provider';
    var id = base, n = 2;
    while (state.providers[id]) { id = base + '-' + n; n++; }
    return id;
  }

  function providerOf(providerId) { return state.providers[providerId] || null; }

  function modelsOf(providerId) {
    var p = providerOf(providerId);
    return (p && p.models) || [];
  }

  // ─── 渲染：头部 ────────────────────────────────────────────

  function renderBadges() {
    var host = $('st-badges');
    if (host) host.innerHTML = '';

    var isDirty = !deepEqual(state, saved);
    var saveBtn = $('st-save');
    if (saveBtn) {
      saveBtn.disabled = !isDirty;
      saveBtn.textContent = isDirty ? '💾 保存设置 (未保存)' : '✓ 保存设置';
    }

    var countEl = $('st-provider-count');
    if (countEl) countEl.textContent = Object.keys(state.providers).length + ' 个';
  }

  function renderBanner() {
    var host = $('st-banner');
    host.innerHTML = '';
    var enabled = Object.keys(state.providers).filter(function (k) { return state.providers[k].enabled; });
    if (!enabled.length) {
      host.appendChild(el('div', { class: 'st-alert err' },
        ['⚠️ 没有任何启用的供应商，写作流程无法启动。请先添加并填写 API Key。']));
      return;
    }
    var missing = enabled.filter(function (k) { return !state.providers[k].api_key_configured; });
    if (missing.length) {
      host.appendChild(el('div', { class: 'st-alert warn' }, [
        '⚠️ 以下供应商尚未配置 API Key：' + missing.map(function (k) {
          return state.providers[k].name;
        }).join('、')
      ]));
    }
    // 默认供应商被禁用是常见误操作（改了默认却没启用新供应商）
    var def = providerOf(state.default_provider);
    if (def && !def.enabled) {
      host.appendChild(el('div', { class: 'st-alert warn' },
        ['⚠️ 默认供应商「' + def.name + '」处于禁用状态，未显式指定供应商的角色会回退到其它启用供应商。']));
    }
  }

  // ─── 渲染：供应商卡片 ──────────────────────────────────────

  function renderProviders() {
    var host = $('st-providers');
    host.innerHTML = '';
    var ids = Object.keys(state.providers);

    ids.forEach(function (pid) {
      var p = state.providers[pid];
      var isDefault = pid === state.default_provider;
      var ts = testStatus[pid] || {};

      var card = el('div', {
        class: 'st-card st-provider-card'
          + (p.enabled ? '' : ' is-disabled')
          + (isDefault ? ' is-default' : '')
      });

      // 头部：名称 + 状态标签
      var tags = [];
      if (isDefault) tags.push(el('span', { class: 'tag blue' }, ['默认']));
      tags.push(el('span', { class: 'tag ' + (p.enabled ? 'green' : '') }, [p.enabled ? '已启用' : '已禁用']));

      card.appendChild(el('div', { class: 'st-provider-head' }, [
        el('div', { class: 'st-provider-name', text: p.name }),
        el('div', { class: 'st-provider-tags' }, tags)
      ]));

      // 元信息：仅保留关键实用信息，不暴露任何 sk 字符串
      var rows = [
        el('div', { class: 'st-meta-row' }, [
          el('span', { class: 'k', text: '地址' }),
          el('span', { class: 'v', text: hostOf(p.base_url), title: p.base_url })
        ]),
        el('div', { class: 'st-meta-row' }, [
          el('span', { class: 'k', text: '密钥' }),
          el('span', {
            class: 'st-key-badge ' + (p.api_key_configured ? 'ok' : 'warn'),
            text: p.api_key_configured ? '✓ 已配置' : '未配置'
          })
        ]),
        el('div', { class: 'st-meta-row' }, [
          el('span', { class: 'k', text: '模型' }),
          el('span', { class: 'v', text: (p.models || []).length + ' 个 · 默认: ' + (p.default_model || '—') })
        ])
      ];
      card.appendChild(el('div', { class: 'st-provider-meta' }, rows));

      // 测试状态行
      if (ts.state) {
        card.appendChild(el('div', { class: 'st-meta-row' }, [
          el('span', { class: 'st-dot ' + ts.state }),
          el('span', {
            class: 'st-hint',
            text: ts.state === 'testing' ? '测试中…' : (ts.text || '')
          })
        ]));
      }

      // 操作
      var foot = el('div', { class: 'st-provider-foot' }, [
        el('button', {
          class: 'btn small', type: 'button', text: '🔌 测试',
          onclick: function () { testProvider(pid); }
        }),
        el('button', {
          class: 'btn small secondary', type: 'button', text: '✏️ 编辑',
          onclick: function () { openProviderModal(pid); }
        }),
        el('button', {
          class: 'btn small secondary', type: 'button', text: p.enabled ? '停用' : '启用',
          onclick: function () {
            p.enabled = !p.enabled;
            render();
          }
        }),
        el('button', {
          class: 'btn small secondary', type: 'button', text: isDefault ? '已默认' : '设为默认',
          disabled: isDefault || !p.enabled,
          onclick: function () {
            state.default_provider = pid;
            render();
          }
        })
      ]);
      if (ids.length > 1) {
        foot.appendChild(el('button', {
          class: 'btn small secondary', type: 'button', text: '🗑 删除',
          onclick: function () { removeProvider(pid); }
        }));
      }
      card.appendChild(foot);
      host.appendChild(card);
    });
  }

  // ─── 渲染：路由卡片 (Image #2 风格) ──────────────────────────

  var openRoutePicker = null;

  function closeRoutePickers(except) {
    document.querySelectorAll('.st-custom-select.open').forEach(function (sel) {
      if (sel !== except) {
        sel.classList.remove('open');
        var card = sel.closest('.st-tier-card, .st-route-card');
        if (card) card.classList.remove('open');
        var panel = sel.querySelector('.sel-panel');
        if (panel) {
          panel.classList.remove('open');
          panel.classList.remove('model');
        }
      }
    });
    if (!except) openRoutePicker = null;
  }

  function positionRoutePicker(sel, panel) {
    panel.classList.remove('flip-left');
    var rect = sel.getBoundingClientRect();
    var width = Math.min(440, Math.max(320, rect.width));
    if (rect.left + width > window.innerWidth - 14) panel.classList.add('flip-left');
  }

  function modelEfforts(model) {
    var declared = model && Array.isArray(model.reasoning_efforts) ? model.reasoning_efforts : [];
    return declared.length ? declared : EFFORTS.map(function (x) { return x[0]; });
  }

  function effortLabel(value) {
    var found = EFFORTS.find(function (x) { return x[0] === value; });
    return found ? found[1] : (value || '默认');
  }

  function renderRouteCard(role) {
    var route = state.subagents[role];
    if (!route) return null;
    var card = el('div', {
      class: 'st-card st-tier-card st-route-card' + (role === 'backend' ? ' st-advanced-route' : ''),
      'data-route-role': role
    });

    var prov = providerOf(route.provider_id);

    // 头部：左侧角色标题 + 副标1 + 副标2；右侧供应商药丸 (Image #2 格式)
    var provPill = el('button', {
      class: 'st-tier-pill active',
      type: 'button',
      text: prov ? prov.name : '未绑定供应商',
      title: '点击切换供应商与模型'
    });

    var headWrap = el('div', { class: 'st-tier-head' }, [
      el('div', { class: 'st-tier-title-wrap' }, [
        el('div', { class: 'st-tier-title', text: roleMeta.titles[role] || role }),
        el('div', { class: 'st-tier-sub1', text: role === 'backend' ? 'PYTHON_SDK · 直连服务' : 'AGENT_SDK · 子代理' }),
        el('div', { class: 'st-tier-sub2', text: roleMeta.descs[role] || '架构规划 · 专属独立分流' })
      ]),
      el('div', { class: 'st-tier-pills' }, [provPill])
    ]);
    card.appendChild(headWrap);

    var currentModel = modelsOf(route.provider_id).find(function (m) { return m.id === route.model; });
    var efforts = modelEfforts(currentModel);
    if (efforts.indexOf(route.reasoning_effort) === -1) {
      route.reasoning_effort = efforts[0] || 'default';
    }

    // 主选择器条 (仿输入框圆角胶囊按钮)
    var sel = el('div', { class: 'st-custom-select', 'data-route-control': 'model-effort' });
    var selBtn = el('button', {
      class: 'sel-btn',
      type: 'button',
      'data-route-action': 'open-picker'
    });

    var selEffPill = el('span', { class: 'sel-eff-pill', text: effortLabel(route.reasoning_effort) });
    var selModelName = el('span', { class: 'sel-model-name', text: route.model || '选择模型' });
    var selContent = el('div', { class: 'sel-btn-content' }, [selEffPill, selModelName]);
    var chevron = el('span', { class: 'sel-chevron', text: '⌄' });

    selBtn.appendChild(selContent);
    selBtn.appendChild(chevron);
    sel.appendChild(selBtn);

    // 弹出面板
    var panel = el('div', { class: 'sel-panel' });

    // ── 1. 强度视图 (view-effort，点击按钮后首先展示) ──
    var effortView = el('div', { class: 'view-effort' });

    // 顶部点击条：[强度] 模型名称 🔄
    var veEff = el('span', { class: 've-eff', text: effortLabel(route.reasoning_effort) });
    var veModel = el('span', { class: 've-model', text: route.model || '选择模型' });
    var veSwitch = el('span', { class: 've-switch', text: '🔄' });
    var veHead = el('div', {
      class: 've-head',
      'data-route-action': 'open-models',
      title: '点击切换模型'
    }, [
      el('div', { class: 've-left' }, [veEff, veModel]),
      veSwitch
    ]);
    effortView.appendChild(veHead);

    // 提示文案（像素级对齐 Image #2）
    effortView.appendChild(el('div', {
      class: 've-hint',
      text: '点击模型名可切换模型 · 拖动调节推理强度'
    }));

    // 滑块
    var range = el('input', {
      class: 'eff-rng',
      type: 'range',
      min: '0',
      max: String(Math.max(0, efforts.length - 1)),
      value: String(Math.max(0, efforts.indexOf(route.reasoning_effort))),
      'data-route-action': 'effort-preview'
    });
    effortView.appendChild(range);

    var ticks = el('div', { class: 'eff-ticks' });
    ticks.innerHTML = efforts.map(function () { return '<i></i>'; }).join('');
    effortView.appendChild(ticks);

    var labels = el('div', { class: 'eff-labels' });
    labels.innerHTML = efforts.map(function (v) { return '<span>' + effortLabel(v) + '</span>'; }).join('');
    effortView.appendChild(labels);

    // ── 2. 模型列表视图 (view-model，再次点击 ve-head 后切换展示) ──
    var modelView = el('div', { class: 'view-model' });

    var vmHead = el('div', { class: 'vm-head' });
    var backBtn = el('button', {
      class: 'vm-back-btn',
      type: 'button',
      text: '返回',
      onclick: function (e) {
        e.stopPropagation();
        panel.classList.remove('model');
      }
    });
    vmHead.appendChild(backBtn);

    var provSel = el('select', {
      class: 'vm-prov-select',
      title: '切换供应商',
      onchange: function (e) {
        e.stopPropagation();
        var pid = e.target.value;
        route.provider_id = pid;
        var p = providerOf(pid);
        var nextModels = modelsOf(pid);
        route.model = (p && p.default_model) || (nextModels[0] && nextModels[0].id) || '';
        render();
      }
    });
    Object.keys(state.providers).forEach(function (pid) {
      var p = state.providers[pid];
      var opt = el('option', { value: pid, text: p.name + (p.enabled ? '' : '（已禁用）') });
      if (pid === route.provider_id) opt.selected = true;
      provSel.appendChild(opt);
    });
    vmHead.appendChild(provSel);
    modelView.appendChild(vmHead);

    var optList = el('div', { class: 'opt-list' });
    modelView.appendChild(optList);

    function updateModelList() {
      optList.innerHTML = '';
      var listModels = modelsOf(route.provider_id);
      if (!listModels.length) {
        optList.appendChild(el('div', { class: 'opt-item', text: '该供应商未配置模型' }));
        return;
      }
      listModels.forEach(function (m) {
        var isSel = m.id === route.model;
        var item = el('div', {
          class: 'opt-item' + (isSel ? ' selected' : ''),
          onclick: function (e) {
            e.stopPropagation();
            route.model = m.id;
            var curM = modelsOf(route.provider_id).find(function (x) { return x.id === route.model; });
            efforts = modelEfforts(curM);
            if (efforts.indexOf(route.reasoning_effort) === -1) {
              route.reasoning_effort = efforts[0] || 'default';
            }
            range.max = String(Math.max(0, efforts.length - 1));
            range.value = String(Math.max(0, efforts.indexOf(route.reasoning_effort)));
            ticks.innerHTML = efforts.map(function () { return '<i></i>'; }).join('');
            labels.innerHTML = efforts.map(function (v) { return '<span>' + effortLabel(v) + '</span>'; }).join('');
            veEff.textContent = effortLabel(route.reasoning_effort);
            veModel.textContent = route.model;
            selEffPill.textContent = effortLabel(route.reasoning_effort);
            selModelName.textContent = route.model;
            updateModelList();
            // 选择模型后自动返回强度视图
            panel.classList.remove('model');
            renderBadges();
          }
        }, [
          el('span', { class: 'opt-id', text: m.name && m.name !== m.id ? (m.name + ' · ' + m.id) : m.id }),
          el('span', { class: 'opt-check', text: isSel ? '✓' : '' })
        ]);
        optList.appendChild(item);
      });
    }
    updateModelList();

    panel.appendChild(effortView);
    panel.appendChild(modelView);
    sel.appendChild(panel);

    // 交互事件绑定：
    // 点击主按钮：展开/收起浮层，默认始终首先展示强度视图！
    selBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      var wasOpen = sel.classList.contains('open');
      closeRoutePickers(null);
      if (!wasOpen) {
        sel.classList.add('open');
        card.classList.add('open');
        panel.classList.add('open');
        panel.classList.remove('model'); // 保证先弹出选择修改强度
        positionRoutePicker(sel, panel);
        openRoutePicker = sel;
      }
    });

    // 点击顶部药丸也可以快速打开选择模型视图
    provPill.addEventListener('click', function (e) {
      e.stopPropagation();
      closeRoutePickers(null);
      sel.classList.add('open');
      card.classList.add('open');
      panel.classList.add('open');
      panel.classList.add('model');
      positionRoutePicker(sel, panel);
      openRoutePicker = sel;
    });

    // 再次点击 ve-head 切换为修改模型视图 (再次点击才修改模型等)
    veHead.addEventListener('click', function (e) {
      e.stopPropagation();
      panel.classList.add('model');
    });

    // 拖动滑块即时调节推理强度
    range.addEventListener('input', function (e) {
      var v = efforts[Number(e.target.value)] || efforts[0];
      route.reasoning_effort = v;
      veEff.textContent = effortLabel(v);
      selEffPill.textContent = effortLabel(v);
    });
    range.addEventListener('change', function () {
      renderBadges();
    });

    card.appendChild(sel);

    // 输出上限配置（按需折叠）
    var maxVal = route.max_tokens || 0;
    var adv = el('details', { class: 'st-route-advanced' }, [
      el('summary', { text: '输出上限: ' + (maxVal ? (maxVal + ' tokens') : '默认') }),
      el('div', { class: 'st-route-adv-body' }, [
        el('label', { class: 'st-field' }, [
          el('span', { text: '最大输出 tokens（0 为使用供应商/系统默认）' }),
          el('input', {
            type: 'number', min: '0', step: '1024',
            value: String(maxVal),
            placeholder: '0 = 默认',
            onchange: function (e) {
              var n = parseInt(e.target.value, 10);
              route.max_tokens = isNaN(n) || n < 0 ? 0 : n;
              renderBadges();
            }
          })
        ])
      ])
    ]);
    card.appendChild(adv);

    return card;
  }

  function renderRoutes() {
    var host = $('st-routes');
    host.innerHTML = '';
    var ordered = (roleMeta.order && roleMeta.order.length)
      ? roleMeta.order.filter(function (r) { return r !== 'backend'; })
      : MAIN_ROLES;
    ordered.forEach(function (r) {
      var c = renderRouteCard(r);
      if (c) host.appendChild(c);
    });
    var advHost = $('st-backend-route');
    advHost.innerHTML = '';
    (roleMeta.order && roleMeta.order.length ? roleMeta.order.filter(function (r) { return r === 'backend'; }) : ADVANCED_ROLES).forEach(function (r) {
      // 后端服务不是 dsh 子代理，标签说明清楚
      var c = renderRouteCard(r);
      if (!c) return;
      if (r === 'backend') {
        c.querySelector('.st-route-desc').textContent =
          'Python 后端直连服务（爬虫 / 离线工具 / 连接测试），不走 token 代理。';
      } else {
        c.classList.remove('st-advanced-route');
      }
      advHost.appendChild(c);
    });
  }

  function render() {
    renderBadges();
    renderBanner();
    renderProviders();
    renderRoutes();
  }

  // ─── 供应商：增 / 删 / 测 ──────────────────────────────────

  function removeProvider(pid) {
    var usedBy = Object.keys(state.subagents).filter(function (r) {
      return state.subagents[r] && state.subagents[r].provider_id === pid;
    });
    if (usedBy.length) {
      toast('该供应商仍被以下角色使用：' + usedBy.map(function (r) {
        return roleMeta.titles[r] || r;
      }).join('、') + '。请先把它们改到别的供应商。', 'error');
      return;
    }
    if (Object.keys(state.providers).length <= 1) {
      toast('至少要保留一个供应商。', 'error');
      return;
    }
    if (!window.confirm('确定删除供应商「' + state.providers[pid].name + '」？')) return;
    delete state.providers[pid];
    if (state.default_provider === pid) {
      state.default_provider = Object.keys(state.providers)[0];
    }
    render();
  }

  function testProvider(pid) {
    var p = providerOf(pid);
    if (!p) return;
    testStatus[pid] = { state: 'testing', text: '测试中…' };
    render();

    fetch('/api/settings/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 带上未保存的草稿：用户改了地址想先验证再保存
      body: JSON.stringify({ provider_id: pid, provider: p, model: p.default_model })
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (res.d && res.d.ok) {
          testStatus[pid] = { state: 'ok', text: '连接正常 · ' + (res.d.elapsed_ms || '?') + 'ms · ' + (res.d.model || '') };
        } else {
          testStatus[pid] = { state: 'err', text: (res.d && res.d.error) || '连接失败' };
        }
        render();
      })
      .catch(function (e) {
        testStatus[pid] = { state: 'err', text: '请求失败：' + e.message };
        render();
      });
  }

  // ─── 供应商编辑抽屉 ────────────────────────────────────────

  var modalDraft = null;   // 编辑中的副本；点确定才写回 state

  function openProviderModal(pid) {
    if (pid) {
      modalDraft = clone(state.providers[pid]);
      modalDraft._isNew = false;
    } else {
      var preset = PRESETS[1];
      modalDraft = {
        id: '', name: '', enabled: true, protocol: 'openai_chat',
        base_url: preset.base_url, api_key: '', api_key_masked: '',
        api_key_configured: false, url_strict: false, verify_ssl: true,
        http_timeout_seconds: 300, default_model: preset.model,
        reasoning_wire: preset.wire,
        chat_completions_endpoint: '', models_endpoint: '', clear_api_key: false,
        models: preset.models.map(function (m) { return { id: m, name: m, source: 'manual' }; }),
        _isNew: true
      };
    }
    $('st-modal-test-result').textContent = '';
    $('st-modal-test-result').className = 'st-test-result';
    renderModal();
    $('st-modal').hidden = false;
    var first = $('st-modal-body').querySelector('input, select');
    if (first) first.focus();
  }

  function closeProviderModal() {
    $('st-modal').hidden = true;
    modalDraft = null;
  }

  function renderModal() {
    var body = $('st-modal-body');
    body.innerHTML = '';
    var d = modalDraft;
    $('st-modal-title').textContent = d._isNew ? '添加供应商' : ('编辑供应商 · ' + d.name);

    // 新建时给一排预设按钮：手填四个字段不如点一下
    if (d._isNew) {
      var presetRow = el('div', { class: 'st-seg' });
      PRESETS.forEach(function (ps) {
        presetRow.appendChild(el('button', {
          type: 'button', class: 'active', text: ps.label,
          onclick: function () {
            d.base_url = ps.base_url;
            d.reasoning_wire = ps.wire;
            d.default_model = ps.model;
            d.models = ps.models.map(function (m) { return { id: m, name: m }; });
            renderModal();
          }
        }));
      });
      body.appendChild(el('div', { class: 'st-field' }, [
        el('span', { text: '快速模板' }), presetRow
      ]));
    }

    body.appendChild(el('div', { class: 'st-form-row' }, [
      el('label', { class: 'st-field' }, [
        el('span', { text: '显示名称' }),
        el('input', {
          type: 'text', value: d.name, placeholder: '例如 DeepSeek 官方',
          oninput: function (e) { d.name = e.target.value; }
        })
      ]),
      el('label', { class: 'st-field' }, [
        el('span', { text: '供应商 ID' + (d._isNew ? '（保存后不可改）' : '') }),
        el('input', {
          type: 'text', value: d.id, placeholder: 'deepseek', disabled: !d._isNew,
          oninput: function (e) { d.id = e.target.value; }
        })
      ])
    ]));

    body.appendChild(el('label', { class: 'st-field' }, [
      el('span', { text: 'API 地址（真实上游，不要填本地代理）' }),
      el('input', {
        type: 'url', value: d.base_url, placeholder: 'https://api.deepseek.com',
        oninput: function (e) { d.base_url = e.target.value; }
      })
    ]));

    body.appendChild(el('label', { class: 'st-field' }, [
      el('span', {
        text: 'API Key' + (d.api_key_configured ? '（已在服务端安全保存，留空表示不修改）' : '（请输入 API Key）')
      }),
      el('input', {
        type: 'password', autocomplete: 'new-password',
        value: '',
        placeholder: d.api_key_configured ? '已配置（留空保持不变）' : '请输入 API Key',
        oninput: function (e) { d.api_key = e.target.value.trim(); d.clear_api_key = false; }
      })
    ]));

    body.appendChild(el('div', { class: 'st-form-row' }, [
      el('label', { class: 'st-field' }, [
        el('span', { text: '模型目录接口（可选）' }),
        el('input', { type: 'text', value: d.models_endpoint || '', placeholder: '留空自动使用 /v1/models',
          oninput: function (e) { d.models_endpoint = e.target.value; } })
      ]),
      el('label', { class: 'st-field' }, [
        el('span', { text: '聊天接口（可选）' }),
        el('input', { type: 'text', value: d.chat_completions_endpoint || '', placeholder: '留空自动使用 /v1/chat/completions',
          oninput: function (e) { d.chat_completions_endpoint = e.target.value; } })
      ])
    ]));

    if (!d._isNew && d.api_key_configured) {
      body.appendChild(el('label', { class: 'st-check st-danger' }, [
        el('input', { type: 'checkbox', checked: !!d.clear_api_key,
          onchange: function (e) { d.clear_api_key = e.target.checked; } }),
        el('span', { text: '清除已保存的 API Key（需保存后生效）' })
      ]));
    }

    body.appendChild(el('div', { class: 'st-form-row' }, [
      el('label', { class: 'st-field' }, [
        el('span', { text: '思考协议接线' }),
        (function () {
          var sel = el('select', {
            onchange: function (e) { d.reasoning_wire = e.target.value; }
          });
          [['default', 'default（按需发 reasoning_effort）'],
           ['none', 'none（剥离一切思考参数）'],
           ['openai', 'openai（只发 reasoning_effort）'],
           ['deepseek', 'deepseek（发 thinking）']].forEach(function (pair) {
            var o = el('option', { value: pair[0], text: pair[1] });
            if (d.reasoning_wire === pair[0]) o.selected = true;
            sel.appendChild(o);
          });
          return sel;
        })()
      ]),
      el('label', { class: 'st-field' }, [
        el('span', { text: '超时（秒）' }),
        el('input', {
          type: 'number', min: '5', max: '1800', value: String(d.http_timeout_seconds || 300),
          oninput: function (e) { d.http_timeout_seconds = parseInt(e.target.value, 10) || 300; }
        })
      ])
    ]));

    body.appendChild(el('div', { class: 'st-form-row' }, [
      el('label', { class: 'st-check' }, [
        el('input', {
          type: 'checkbox', checked: !!d.url_strict,
          onchange: function (e) { d.url_strict = e.target.checked; }
        }),
        el('span', { text: 'URL 严格模式（只补 /chat/completions，不加 /v1）' })
      ]),
      el('label', { class: 'st-check' }, [
        el('input', {
          type: 'checkbox', checked: !!d.verify_ssl,
          onchange: function (e) { d.verify_ssl = e.target.checked; }
        }),
        el('span', { text: '校验 TLS 证书' })
      ])
    ]));

    // 模型清单
    var list = el('div', { class: 'st-models' });
    list.appendChild(el('div', { class: 'st-model-row st-model-head' }, [
      el('span', { text: '模型 ID' }),
      el('span', { text: '显示名' }),
      el('span', { text: '上下文' }),
      el('span', { text: '最大输出' }),
      el('span', { text: '' })
    ]));
    (d.models || []).forEach(function (m, idx) {
      list.appendChild(el('div', { class: 'st-model-row' }, [
        el('input', {
          type: 'text', value: m.id, placeholder: 'model-id',
          oninput: function (e) {
            var oldId = m.id;
            m.id = e.target.value.trim();
            if (!m._original_id) m._original_id = oldId;
            if (d.default_model === oldId || d.default_model === '') d.default_model = m.id;
          }
        }),
        el('input', {
          type: 'text', value: m.name || m.id, placeholder: '显示名',
          oninput: function (e) { m.name = e.target.value; }
        }),
        el('input', {
          type: 'number', value: String(m.context_window || 128000), min: '1000', step: '1000',
          oninput: function (e) { m.context_window = parseInt(e.target.value, 10) || 128000; }
        }),
        el('input', {
          type: 'number', value: String(m.max_tokens || 8192), min: '0', step: '1024',
          oninput: function (e) { m.max_tokens = parseInt(e.target.value, 10) || 0; }
        }),
        el('button', {
          class: 'st-icon-btn', type: 'button', text: '✕', title: '删除该模型',
          onclick: function () {
            var refs = state && state.subagents ? Object.keys(state.subagents).filter(function (role) {
              var route = state.subagents[role];
              return route && route.provider_id === d.id && route.model === m.id;
            }) : [];
            if (refs.length || d.default_model === m.id) {
              toast('模型「' + m.id + '」仍被默认模型或角色使用，请先改派后再删除。', 'error');
              return;
            }
            d.models.splice(idx, 1);
            renderModal();
          }
        })
      ]));
    });
    body.appendChild(el('div', {}, [
      el('div', { class: 'st-models-head' }, [
        el('strong', { text: '模型清单' }),
        el('button', {
          class: 'btn btn-ghost', type: 'button', text: '🔎 检测模型',
          onclick: function () { discoverModels(); }
        }),
        el('button', {
          class: 'btn btn-ghost', type: 'button', text: '➕ 加一行',
          onclick: function () {
            d.models.push({ id: '', name: '', context_window: 128000, max_tokens: 8192 });
            renderModal();
          }
        })
      ]),
      list
    ]));

    // 默认模型：清单里选一个
    body.appendChild(el('label', { class: 'st-field' }, [
      el('span', { text: '该供应商的默认模型（角色未显式指定时使用）' }),
      (function () {
        var sel = el('select', {
          onchange: function (e) { d.default_model = e.target.value; }
        });
        (d.models || []).forEach(function (m) {
          if (!m.id) return;
          var o = el('option', { value: m.id, text: m.id });
          if (m.id === d.default_model) o.selected = true;
          sel.appendChild(o);
        });
        return sel;
      })()
    ]));
  }

  function discoverModels() {
    var d = modalDraft;
    if (!d) return;
    var result = $('st-modal-test-result');
    result.className = 'st-test-result';
    result.textContent = '检测模型目录中…';
    fetch('/api/settings/models/discover', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider_id: d.id || '',
        provider: {
          protocol: d.protocol || 'openai_chat', base_url: d.base_url,
          api_key: d.api_key || d.api_key_masked || '',
          clear_api_key: !!d.clear_api_key,
          enabled: d.enabled !== false, url_strict: !!d.url_strict,
          verify_ssl: d.verify_ssl !== false,
          http_timeout_seconds: d.http_timeout_seconds || 300,
          default_model: d.default_model,
          reasoning_wire: d.reasoning_wire || 'default',
          models_endpoint: d.models_endpoint || '',
          chat_completions_endpoint: d.chat_completions_endpoint || '',
          models: (d.models || []).filter(function (m) { return m.id; })
        }
      })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.j || !res.j.ok) {
          result.className = 'st-test-result err';
          result.textContent = '❌ ' + ((res.j && res.j.error) || '模型检测失败');
          return;
        }
        var old = d.models || [];
        var byId = {};
        old.forEach(function (m) { if (m && m.id) byId[m.id] = m; });
        (res.j.models || []).forEach(function (fresh) {
          if (!fresh || !fresh.id) return;
          if (!byId[fresh.id]) byId[fresh.id] = fresh;
          else {
            // Preserve manual metadata; only refresh discovery-owned fields.
            if (byId[fresh.id].source !== 'manual') byId[fresh.id] = Object.assign({}, byId[fresh.id], fresh);
          }
        });
        d.models = Object.keys(byId).map(function (id) { return byId[id]; });
        if (!d.default_model && d.models.length) d.default_model = d.models[0].id;
        result.className = 'st-test-result ok';
        result.textContent = '✅ 发现 ' + (res.j.count || 0) + ' 个模型' +
          (res.j.truncated ? '（结果已截断）' : '') + '，已合并到草稿；点击“确定”后再保存。';
        renderModal();
      })
      .catch(function (e) {
        result.className = 'st-test-result err';
        result.textContent = '❌ 请求失败：' + e.message;
      });
  }

  function saveProviderModal() {
    var d = modalDraft;
    if (!d.name) { toast('请填写显示名称', 'error'); return; }
    var pid = (d.id || '').trim();
    if (!pid) { toast('请填写供应商 ID', 'error'); return; }
    if (pid.indexOf(ROUTE_PREFIX) === 0) {
      toast('供应商 ID 不得以 ' + ROUTE_PREFIX + ' 开头（那是角色路由的保留前缀）', 'error');
      return;
    }
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(pid)) {
      toast('供应商 ID 只能用字母/数字/下划线/短横线', 'error');
      return;
    }
    if (d._isNew && state.providers[pid]) { toast('供应商 ID 已存在', 'error'); return; }
    if (!d.base_url) { toast('请填写 API 地址', 'error'); return; }
    if (!/^https?:\/\//.test(d.base_url)) { toast('API 地址必须以 http:// 或 https:// 开头', 'error'); return; }

    // 清掉空行，别把空模型写进清单（服务端也会过滤，这里先做一次让预览准确）
    d.models = (d.models || []).filter(function (m) { return m.id && m.id.trim(); });
    if (!d.models.length) { toast('至少需要一个模型', 'error'); return; }
    d.models.forEach(function (m) { m.id = m.id.trim(); m.name = (m.name || m.id).trim() || m.id; });
    var modelIds = d.models.map(function (m) { return m.id; });
    if (new Set(modelIds).size !== modelIds.length) {
      toast('模型 ID 不能重复', 'error'); return;
    }
    if (!d.default_model || !d.models.some(function (m) { return m.id === d.default_model; })) {
      d.default_model = d.models[0].id;
    }

    // 掩码/空 key → 保持原值（服务端也会这么做；这里让本地 state 与之一致，避免假 dirty）
    var oldKey = (state.providers[pid] || {});
    var incoming = (d.api_key || '').trim();
    var keepMasked = (!incoming || incoming.indexOf('****') !== -1) && !d.clear_api_key;
    // Apply model renames to routes only when the modal is confirmed.
    (d.models || []).forEach(function (m) {
      var oldId = m._original_id;
      if (!oldId || oldId === m.id || !state.subagents) return;
      Object.keys(state.subagents).forEach(function (role) {
        var route = state.subagents[role];
        if (route && route.provider_id === pid && route.model === oldId) route.model = m.id;
      });
    });
    delete d._isNew;

    var finalProvider = {
      id: pid,
      name: d.name.trim(),
      enabled: d.enabled !== false,
      protocol: d.protocol || 'openai_chat',
      base_url: d.base_url.trim().replace(/\/+$/, ''),
      api_key: keepMasked ? '' : incoming,
      api_key_masked: keepMasked ? (oldKey.api_key_masked || '') : '',
      api_key_configured: keepMasked ? !!oldKey.api_key_configured : !!incoming,
      url_strict: !!d.url_strict,
      verify_ssl: d.verify_ssl !== false,
      http_timeout_seconds: d.http_timeout_seconds || 300,
      default_model: d.default_model,
      reasoning_wire: d.reasoning_wire || 'default',
      chat_completions_endpoint: d.chat_completions_endpoint || '',
      models_endpoint: d.models_endpoint || '',
      clear_api_key: !!d.clear_api_key,
      models: d.models
    };

    // 新建时把新供应商兜给尚未绑定的角色（否则新加的供应商得手动逐个改）
    var isNew = !state.providers[pid];
    state.providers[pid] = finalProvider;
    if (isNew && Object.keys(state.providers).length === 1) {
      state.default_provider = pid;
    }
    closeProviderModal();
    render();
  }

  // ─── 模型目录刷新 ──────────────────────────────────────────
  var refreshInFlight = null;
  function refreshCatalog(persist) {
    if (refreshInFlight) return refreshInFlight;
    var btn = $('st-refresh-models');
    if (btn) { btn.disabled = true; btn.textContent = '⟳ 刷新中…'; }
    refreshInFlight = fetch('/api/settings/models/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider_id: state.default_provider, persist: persist !== false })
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      if (!res.ok || !res.d || !res.d.ok) {
        toast('模型目录刷新失败：' + ((res.d && res.d.error) || '未知错误'), 'error');
        return false;
      }
      if (res.d.config) {
        applyServerConfig(res.d.config);
        toast('已同步 ' + (res.d.count || 0) + ' 个上游模型（含 gemini-3.8-flash 等可用 ID）', 'success');
      }
      return true;
    }).catch(function (e) {
      toast('模型目录刷新请求失败：' + e.message, 'error');
      return false;
    }).finally(function () {
      refreshInFlight = null;
      if (btn) { btn.disabled = false; btn.textContent = '🔄 刷新目录'; }
    });
    return refreshInFlight;
  }

  // ─── 保存 ──────────────────────────────────────────────────

  function collectPayload() {
    var providers = {};
    Object.keys(state.providers).forEach(function (pid) {
      var p = state.providers[pid];
      providers[pid] = {
        id: p.id, name: p.name, enabled: p.enabled, protocol: p.protocol,
        base_url: p.base_url,
        // 掩码原样回传 = 服务端保留原 key；用户新填的明文才覆盖
        api_key: p.api_key_masked || '',
        default_model: p.default_model,
        url_strict: p.url_strict, verify_ssl: p.verify_ssl,
        http_timeout_seconds: p.http_timeout_seconds,
        reasoning_wire: p.reasoning_wire,
        chat_completions_endpoint: p.chat_completions_endpoint || '',
        models_endpoint: p.models_endpoint || '',
        clear_api_key: !!p.clear_api_key,
        models: (p.models || []).map(function (m) {
          return {
            id: m.id, name: m.name, context_window: m.context_window,
            max_tokens: m.max_tokens,
            reasoning_efforts: m.reasoning_efforts || ['default'],
            source: m.source || 'manual'
          };
        })
      };
      // 用户在抽屉里新填了明文 key 时，state 上会带一份临时的 api_key
      if (p.api_key) providers[pid].api_key = p.api_key;
    });

    var subagents = {};
    Object.keys(state.subagents).forEach(function (r) {
      var x = state.subagents[r];
      subagents[r] = {
        provider_id: x.provider_id, model: x.model,
        max_tokens: x.max_tokens || 0,
        reasoning_effort: x.reasoning_effort || 'default'
      };
    });

    return {
      default_provider: state.default_provider,
      providers: providers,
      subagents: subagents,
      backend: state.backend,
      context_budget_tokens: Number(state.context_budget_tokens || 300000)
    };
  }

  function saveAll() {
    var btn = $('st-save');
    btn.disabled = true;
    btn.textContent = '保存中…';
    fetch('/api/settings/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectPayload())
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        btn.textContent = '💾 保存设置';
        if (res.d && res.d.ok) {
          toast('✅ 设置已保存，下一次模型调用即生效', 'success');
          return reload();
        }
        toast('❌ ' + ((res.d && res.d.error) || '保存失败'), 'error');
        render();
      })
      .catch(function (e) {
        btn.textContent = '💾 保存设置';
        toast('❌ 保存请求失败：' + e.message, 'error');
        render();
      });
  }

  function reload() {
    return fetch('/api/settings/config')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        applyServerConfig(d.config);
      });
  }

  function applyServerConfig(cfg) {
    state = {
      default_provider: cfg.default_provider,
      context_budget_tokens: Number(cfg.context_budget_tokens || 300000),
      providers: clone(cfg.providers),
      subagents: {},
      backend: {
        provider_id: cfg.backend.provider_id,
        model: cfg.backend.model,
        max_tokens: cfg.backend.max_tokens || 0,
        reasoning_effort: cfg.backend.reasoning_effort || 'default'
      }
    };
    Object.keys(cfg.subagents).forEach(function (r) {
      var x = cfg.subagents[r];
      state.subagents[r] = {
        provider_id: x.provider_id, model: x.model,
        max_tokens: x.max_tokens || 0,
        reasoning_effort: x.reasoning_effort || 'default'
      };
    });
    // 后端路由也放进 subagents 视图里，渲染/保存走同一条路径
    state.subagents.backend = state.backend;

    saved = clone(state);
    $('st-context-budget').value = state.context_budget_tokens;
    render();
  }

  // ─── 启动 ──────────────────────────────────────────────────

  function boot() {
    var boot = JSON.parse($('st-bootstrap').textContent);
    roleMeta = JSON.parse($('st-role-meta').textContent);
    applyServerConfig(boot);

    $('st-save').addEventListener('click', saveAll);
    $('st-add-provider').addEventListener('click', function () { openProviderModal(null); });
    $('st-modal-close').addEventListener('click', closeProviderModal);
    $('st-modal-cancel').addEventListener('click', closeProviderModal);
    $('st-modal-ok').addEventListener('click', saveProviderModal);
    $('st-modal-test').addEventListener('click', function () {
      // 用抽屉里的草稿测试（不必先保存）
      var d = modalDraft;
      if (!d) return;
      var out = $('st-modal-test-result');
      out.className = 'st-test-result';
      out.textContent = '测试中…';
      fetch('/api/settings/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider_id: d.id || '',
          provider: {
            base_url: d.base_url, api_key: d.api_key || d.api_key_masked || '',
            url_strict: d.url_strict, verify_ssl: d.verify_ssl,
            reasoning_wire: d.reasoning_wire,
            models: (d.models || []).filter(function (m) { return m.id; })
          },
          model: d.default_model
        })
      }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (res.j && res.j.ok) {
            out.className = 'st-test-result ok';
            out.textContent = '✅ 连接正常 · ' + (res.j.elapsed_ms || '?') + 'ms · 模型 ' + (res.j.model || '');
          } else {
            out.className = 'st-test-result err';
            out.textContent = '❌ ' + ((res.j && res.j.error) || '连接失败');
          }
        })
        .catch(function (e) {
          out.className = 'st-test-result err';
          out.textContent = '❌ 请求失败：' + e.message;
        });
    });

    if ($('st-test-default')) {
      $('st-test-default').addEventListener('click', function () { testProvider(state.default_provider); });
    }
    if ($('st-refresh-models')) {
      $('st-refresh-models').addEventListener('click', function () { refreshCatalog(true); });
    }
    if ($('st-model-search-all')) {
      $('st-model-search-all').addEventListener('input', function () { renderRoutes(); });
    }

    // 预算字段不是每次键入都该重渲染（会打断输入）——只在 change 时同步
    $('st-context-budget').addEventListener('input', function (e) {
      var n = parseInt(e.target.value, 10);
      state.context_budget_tokens = isNaN(n) ? 300000 : n;
      renderBadges();
    });

    // 遮罩点击 / Esc 关闭抽屉与浮层
    $('st-modal').addEventListener('mousedown', function (e) {
      if (e.target === $('st-modal')) closeProviderModal();
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.st-custom-select') && !e.target.closest('.st-tier-pill')) {
        closeRoutePickers(null);
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        closeRoutePickers(null);
        if (!$('st-modal').hidden) closeProviderModal();
      }
    });
    window.addEventListener('resize', function () {
      if (openRoutePicker) {
        var panel = openRoutePicker.querySelector('.sel-panel');
        if (panel) positionRoutePicker(openRoutePicker, panel);
      }
    });
    // 有未保存修改时拦一下关页
    window.addEventListener('beforeunload', function (e) {
      if (state && saved && !deepEqual(state, saved)) {
        e.preventDefault();
        e.returnValue = '';
      }
    });

    render();

    // Legacy v1 configurations contain only their default model. Automatically
    // enrich them once on page load so the route matrix immediately shows the
    // provider's complete catalog; explicit refresh remains available anytime.
    var initialProvider = state.providers[state.default_provider];
    if (initialProvider && (initialProvider.models || []).length <= 1 && initialProvider.base_url) {
      window.setTimeout(function () { refreshCatalog(true); }, 80);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
