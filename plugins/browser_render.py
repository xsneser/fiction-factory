# -*- coding: utf-8 -*-
"""轻量 headless 渲染辅助（playwright，按需 import）：给 webnovel_scraper 抓「正文 JS 填充」的镜像站。

用法：`from plugins.browser_render import render_html; html = render_html(url, wait_sel=...)`

- 每个调用线程懒启动一个持久 chromium（threading.local），避免逐章反复启动；
  Web 抓取由 task_manager.ensure_single 串行，跨线程渲染各自持有浏览器互不干扰。
- 失败返回 ""（调用方回退静态路径）；返回的是渲染后 HTML，正文容器由站点适配器
  （content_div_id / after_title 等）照常解析，续页 {cid}_N 走既有 extra_page_re。
"""
import logging
import threading

logger = logging.getLogger("novel-engine.browser")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_tls = threading.local()


def _renderer():
    pw = getattr(_tls, "_pw", None)
    if pw is None:
        from playwright.sync_api import sync_playwright
        _tls._pw = sync_playwright().start()
        _tls._browser = _tls._pw.chromium.launch(headless=True)
    return _tls._pw, _tls._browser


def render_html(url: str, timeout_ms: int = 20000, wait_sel: str = "") -> str:
    """打开 URL → 等网络空闲 → 返回渲染后 HTML。

    wait_sel 非空时额外等该 CSS 选择器出现（正文容器），到点未出现也返回全文。"""
    try:
        _pw, browser = _renderer()
        page = browser.new_page(user_agent=UA, locale="zh-CN")
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            if wait_sel:
                try:
                    page.wait_for_selector(wait_sel, timeout=8000)
                except Exception:
                    pass
            return page.content()
        finally:
            page.close()
    except Exception as e:
        logger.warning("render_html failed: %s (%s)", url, e)
        return ""
