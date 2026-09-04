// markdown.js — 侧栏 Agent 回复的最小 Markdown 渲染器（零依赖、离线可用）。
// 安全模型：先整段 escapeHtml（原始 <script>/<img onerror> 转义后到不了 DOM），
// 再做块/行内解析；链接/图片 URL 仅放行 http/https/mailto/tel 与相对路径，
// 阻断 javascript:/data:/vbscript: 及实体混淆。escapeHtml 由 base.js（head）提供。
(function () {
    'use strict';

    // URL 白名单：解回转义实体后校验 scheme，仅 http/https/mailto/tel 或相对路径放行
    function sanitizeUrl(u) {
        u = String(u == null ? '' : u)
            .replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
            .replace(/&#x27;/gi, "'").replace(/&#x2F;/gi, '/');
        var m = u.match(/^\s*([a-zA-Z][a-zA-Z0-9+.-]*)\s*:/);
        if (!m) return u;                                   // 无 scheme → 相对路径
        var s = m[1].toLowerCase();
        return (s === 'http' || s === 'https' || s === 'mailto' || s === 'tel') ? u : '';
    }

    // 行内结构：输入已转义。顺序：行内代码 → 粗/斜体 → 删除线 → 图片 → 链接。
    function inline(t) {
        return t
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/\*([^*]+)\*/g, '<em>$1</em>')
            .replace(/~~([^~]+)~~/g, '<del>$1</del>')
            .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, function (m, alt, url) {
                var u = sanitizeUrl(url);
                return u ? '<img src="' + u + '" alt="' + alt + '" loading="lazy">' : alt;
            })
            .replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, function (m, txt, url) {
                var u = sanitizeUrl(url);
                return u ? '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + txt + '</a>' : txt;
            });
    }

    // 表格分隔行：| --- | --- |（或 :---: 对齐记号，仅识别不做对齐类）
    function isTableSep(line) {
        var s = line.trim();
        if (s.charAt(0) !== '|') return false;
        var inner = s.slice(1);
        if (inner.charAt(inner.length - 1) === '|') inner = inner.slice(0, -1);
        var cells = inner.split('|');
        return cells.length > 0 && cells.every(function (c) {
            return /^[\s:|-]*$/.test(c) && c.indexOf('-') >= 0;
        });
    }
    // 解析一行表格：\| 转义竖线先用占位符，最后还原。
    // 占位符用 &pipe;：原文本里的 & 已被 escapeHtml 转成 &amp;，转义后文本不可能含原始 &pipe;，不会误替换。
    function parseRow(row) {
        var PIPE = '&pipe;';
        var s = row.trim().replace(/\\\|/g, PIPE);
        if (s.charAt(0) === '|') s = s.slice(1);
        if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
        return s.split('|').map(function (c) { return c.split(PIPE).join('|').trim(); });
    }
    function renderTableAt(lines, i) {
        var head = parseRow(lines[i]);
        var body = [], j = i + 2;
        while (j < lines.length) {
            var r = lines[j].trim();
            if (r.charAt(0) !== '|' || /^[\s:|-]+$/.test(r)) break;   // 非表格行或第二个分隔行
            body.push(parseRow(r)); j++;
        }
        var h = '<div class="agent-msg-table"><table><thead><tr>';
        for (var x = 0; x < head.length; x++) h += '<th>' + inline(head[x]) + '</th>';
        h += '</tr></thead><tbody>';
        for (var b = 0; b < body.length; b++) {
            h += '<tr>';
            var cells = body[b];
            for (var c = 0; c < Math.max(cells.length, head.length); c++) h += '<td>' + inline(cells[c] || '') + '</td>';
            h += '</tr>';
        }
        return { html: h + '</tbody></table></div>', next: j };
    }

    function renderMarkdown(src) {
        var lines = escapeHtml(src == null ? '' : String(src)).split('\n');
        var html = [], i = 0, inCode = false, codeBuf = [];
        while (i < lines.length) {
            var line = lines[i];
            if (/^\s*```/.test(line)) {                          // 围栏代码
                if (!inCode) { inCode = true; codeBuf = []; }
                else { inCode = false; html.push('<pre><code>' + codeBuf.join('\n') + '</code></pre>'); }
                i++; continue;
            }
            if (inCode) { codeBuf.push(line); i++; continue; }
            if (/^\s*$/.test(line)) { i++; continue; }           // 空行

            var h = line.match(/^(#{1,3})\s+(.*)$/);              // 标题
            if (h) { var lv = h[1].length; html.push('<h' + lv + '>' + inline(h[2]) + '</h' + lv + '>'); i++; continue; }

            if (/^\s*\|/.test(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
                var t = renderTableAt(lines, i); html.push(t.html); i = t.next; continue;
            }

            var bm = line.match(/^>\s?(.*)$/);                    // 引用
            if (bm) {
                var bq = [];
                while (i < lines.length) { var b = lines[i].match(/^>\s?(.*)$/); if (!b) break; bq.push(b[1]); i++; }
                html.push('<blockquote>' + inline(bq.join('<br>')) + '</blockquote>'); continue;
            }

            if (/^(\s*)[-*·]\s+/.test(line)) {                    // 无序列表（含 ·）
                var uitems = [];
                while (i < lines.length && /^(\s*)[-*·]\s+/.test(lines[i])) { uitems.push(lines[i].replace(/^(\s*)[-*·]\s+/, '')); i++; }
                html.push('<ul>' + uitems.map(function (x) { return '<li>' + inline(x) + '</li>'; }).join('') + '</ul>'); continue;
            }
            if (/^(\s*)\d+\.\s+/.test(line)) {                    // 有序列表
                var oitems = [];
                while (i < lines.length && /^(\s*)\d+\.\s+/.test(lines[i])) { oitems.push(lines[i].replace(/^(\s*)\d+\.\s+/, '')); i++; }
                html.push('<ol>' + oitems.map(function (x) { return '<li>' + inline(x) + '</li>'; }).join('') + '</ol>'); continue;
            }

            var para = [];                                        // 段落：单换行 → <br>（保欢迎语/错误消息换行）
            while (i < lines.length) {
                var pl = lines[i];
                if (/^\s*$/.test(pl) || /^(#{1,3})\s/.test(pl) || /^\s*```/.test(pl) || /^\s*>/.test(pl)) break;
                if (/^\s*\|/.test(pl) && i + 1 < lines.length && isTableSep(lines[i + 1])) break;
                if (/^(\s*)[-*·]\s+/.test(pl) || /^(\s*)\d+\.\s+/.test(pl)) break;
                para.push(pl); i++;
            }
            html.push(para.length ? '<p>' + para.map(inline).join('<br>') + '</p>' : '');
        }
        if (inCode) html.push('<pre><code>' + codeBuf.join('\n') + '</code></pre>');
        return html.join('\n');
    }

    window.renderMarkdown = renderMarkdown;
})();
