"""
番茄小说 PUA 字体解码器
破解自定义字体防爬机制：PUA码点 → 真实汉字。

番茄把正文汉字替换为 Unicode PUA 私用区码点，靠 @font-face 自定义字体在浏览器渲染成正常汉字。
解码用维护好的字体映射表 plugins/font_charset.json（源自开源项目 ckenkuo/fanqie-cdp-downloader 的
charset.json，实测《十日终焉》1496 章 329 万字还原率 100%）。两套字体映射（mode0/mode1）对应
私用区 [0xE3E8,0xE55B] / [0xE3E9,0xE55C]，解码时双试取残留最少者。

历史：旧版靠 glyph 名推断 + 字形比对，但现字体 glyph 名已加固为 gidNNNN 随机命名、且光栅比对
判别性不足；改用维护好的全局映射表最稳。
"""
import json
import re
from pathlib import Path

# 番茄正文私用区码位范围（两套字体映射，与 ckenkuo/fanqie-cdp-downloader 的 config.py 一致）
CODE = [[58344, 58715], [58345, 58716]]   # mode0: 0xE3E8+ / mode1: 0xE3E9+


class FanqieDecoder:
    """番茄小说 PUA 字体解码器（全局映射表还原）"""

    def __init__(self, cache_dir: str = "storage/font_cache", verify: bool = True):
        base = Path(__file__).resolve().parent.parent
        self.cache_dir = Path(cache_dir) if Path(cache_dir).is_absolute() else base / cache_dir
        self.verify = verify
        self._charset = None    # 惰性加载 storage/font_charset.json

    # ─── 解码入口：把正文里的 PUA 密文还原为汉字 ───
    def decode_content(self, content: str) -> str:
        """按 charset 双 mode 解码，返回残留 PUA 最少的结果（无 PUA 的文本原样返回）。"""
        if not content or self._count_pua(content) == 0:
            return content   # 无 PUA：早退，省两遍逐字拷贝（榜单/正文大量无密文输入）
        charset = self._load_charset()
        if not charset:
            return content
        candidates = []
        for mode in (0, 1):
            lo, hi = CODE[mode]
            cs = charset[mode] if mode < len(charset) else []
            out = []
            for ch in content:
                u = ord(ch)
                if lo <= u <= hi:
                    bias = u - lo
                    if 0 <= bias < len(cs) and cs[bias] != "?":
                        out.append(cs[bias])
                    else:
                        out.append(ch)
                else:
                    out.append(ch)
            candidates.append("".join(out))
        # 选残留 PUA 最少的
        return min(candidates, key=lambda t: self._count_pua(t))

    def decode_page(self, html: str) -> str:
        """解码页面（提取 SSR 正文后还原）。"""
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', html, re.DOTALL)
        if m:
            try:
                ssr = json.loads(m.group(1))
                content = ssr.get("reader", {}).get("chapterData", {}).get("content",
                          ssr.get("reader", {}).get("content", ""))
                if content:
                    return self.decode_content(re.sub(r"<[^>]+>", "", content))
            except Exception:
                pass
        return html

    def _count_pua(self, text: str) -> int:
        lo = min(CODE[0][0], CODE[1][0]); hi = max(CODE[0][1], CODE[1][1])
        return sum(1 for ch in text if lo <= ord(ch) <= hi)

    def _load_charset(self):
        if self._charset is None:
            base = Path(__file__).resolve().parent.parent
            # 仓库内随代码分发（plugins/font_charset.json）；storage 可覆盖（更新版）
            for cand in (Path(__file__).resolve().parent / "font_charset.json",
                         base / "storage" / "font_charset.json"):
                try:
                    if cand.exists():
                        self._charset = json.loads(cand.read_text(encoding="utf-8"))
                        break
                except Exception:
                    pass
            if self._charset is None:
                self._charset = []
        return self._charset


# ═══════════════════════════════════════
# 映射表工具（兼容旧 load_mapping 格式）
# ═══════════════════════════════════════

def load_mapping(json_path: str) -> dict:
    """加载 PUA → 汉字映射表（{"U+XXXX": "汉", ...}）"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    mapping = {}
    for k, v in data.items():
        try:
            mapping[chr(int(k.replace("U+", ""), 16))] = v
        except Exception:
            continue
    return mapping


def decode_with_mapping(text: str, mapping: dict) -> str:
    """用映射表解码"""
    return "".join(mapping.get(c, c) for c in text)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from plugins.fanqie_scout import FanqieCrawler
    import json as _j

    # 统一书库：取第一本有 book_id 的书做解码自检（不再硬编码旧 platform 子目录路径）
    from plugins.novel_storage import NOVELS_DIR
    _info_file = next(iter(NOVELS_DIR.glob("*/info.json")), None)
    if not _info_file:
        print("书库为空，跳过自检"); sys.exit(0)
    info = _j.loads(_info_file.read_text(encoding="utf-8"))
    if not info.get("book_id"):
        print("无可用 book_id 书，跳过自检"); sys.exit(0)
    crawler = FanqieCrawler()
    ch = crawler.get_chapter_list(info["book_id"], 1)[0]
    r = crawler.session.get(f"https://fanqienovel.com/reader/{ch['id']}", timeout=15,
                            headers={"Accept": "text/html,application/xhtml+xml"})
    dec = FanqieDecoder()
    out = dec.decode_page(r.text)
    left = dec._count_pua(out)
    print("leftover PUA:", left, "/", len(out))
    print("decoded head:", repr(out[:160]))
