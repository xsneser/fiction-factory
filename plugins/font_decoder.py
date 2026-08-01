"""
番茄小说 PUA 字体解码器
破解自定义字体防爬机制：PUA码点 → 字形 → 真实汉字
"""
import re
import json
import requests
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen


class FanqieDecoder:
    """
    番茄小说 PUA 字体解码器

    原理：
    1. 番茄将正文汉字替换为 Unicode PUA (U+E000-U+F8FF) 码点
    2. 通过 @font-face 加载自定义字体，将 PUA 码点映射到形似汉字
    3. 解码：下载字体 → 提取 glyph → 用字体名称/OCR 还原真实汉字
    """

    def __init__(self, cache_dir: str = "storage/font_cache", verify: bool = True):
        # 锚定项目根目录，避免依赖 CWD
        base = Path(__file__).resolve().parent.parent
        self.cache_dir = Path(cache_dir) if Path(cache_dir).is_absolute() else base / cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._mapping_cache: dict[str, dict] = {}
        self.verify = verify

    def decode_page(self, html: str) -> str:
        """解码页面中 PUA 编码的内容"""
        # 1. 提取字体 URL
        font_url = self._extract_font_url(html)
        if not font_url:
            return self._try_ssr_decode(html)

        # 2. 下载/缓存字体映射
        mapping = self._get_font_mapping(font_url)
        if not mapping:
            return self._try_ssr_decode(html)

        # 3. 解码所有 PUA 字符
        decoded = self._apply_mapping(html, mapping)
        return decoded

    def _extract_font_url(self, html: str) -> str:
        """从页面 CSS 中提取字体文件 URL"""
        # @font-face { font-family: xxx; src: url("https://...woff2") }
        m = re.search(r'@font-face\s*\{[^}]*?url\("([^"]+\.woff2?\d*)"\)', html, re.DOTALL)
        if m:
            return m.group(1)
        m = re.search(r'src:\s*url\("([^"]+\.woff2?\d*)"\)', html)
        if m:
            return m.group(1)
        return ""

    def _get_font_mapping(self, font_url: str) -> dict:
        """下载字体并提取 PUA → 汉字映射"""
        font_key = font_url.split("/")[-1].split("?")[0]
        if font_key in self._mapping_cache:
            return self._mapping_cache[font_key]

        font_path = self.cache_dir / font_key
        if not font_path.exists():
            try:
                r = requests.get(font_url, timeout=30, verify=self.verify,
                                 headers={"User-Agent": "Mozilla/5.0"})
                font_path.write_bytes(r.content)
            except Exception:
                return {}

        try:
            mapping = self._extract_mapping(str(font_path))
            self._mapping_cache[font_key] = mapping
            return mapping
        except Exception:
            return {}

    def _extract_mapping(self, font_path: str) -> dict:
        """从字体文件提取 PUA → 汉字映射"""
        font = TTFont(font_path)
        cmap = font.getBestCmap()
        if not cmap:
            return {}

        glyph_order = font.getGlyphOrder()
        mapping = {}

        # 方法1：glyph 名称中可能直接包含对应汉字
        # 番茄字体的 glyph 命名规则通常是 "uniXXXX" 或包含数字编号
        for pua_code, glyph_id in cmap.items():
            if 0xE000 <= pua_code <= 0xF8FF:  # PUA 范围
                glyph_name = glyph_order[glyph_id] if glyph_id < len(glyph_order) else ""

                # 尝试从 glyph 名称推断
                char = self._glyph_name_to_char(glyph_name)
                if char:
                    mapping[chr(pua_code)] = char

        # 如果方法1得到足够结果(>300)，直接返回
        if len(mapping) > 300:
            font.close()
            return mapping

        # 方法2：使用标准字体比对字形
        # 画每个 PUA glyph → 与标准字体比对
        mapping.update(self._match_by_shape(font, cmap, glyph_order))
        font.close()
        return mapping

    def _glyph_name_to_char(self, name: str) -> str:
        """从 glyph 名称尝试还原汉字"""
        # uniXXXX 格式
        m = re.match(r'^uni([0-9A-Fa-f]{4,})$', name)
        if m:
            code = int(m.group(1), 16)
            if 0x4E00 <= code <= 0x9FFF:  # CJK 范围内
                return chr(code)
        # uXXXXX 格式
        m = re.match(r'^u([0-9A-Fa-f]{4,})$', name)
        if m:
            code = int(m.group(1), 16)
            if 0x4E00 <= code <= 0x9FFF:
                return chr(code)
        # cidXXXXX 格式（数字可能是字符编码）
        m = re.match(r'^cid0*(\d+)$', name)
        if m:
            num = int(m.group(1))
            # 尝试作为 GB2312 或 Unicode 偏移
            # 番茄字体中 cid 编号通常不是直接的 Unicode
            pass
        return ""

    def _match_by_shape(self, font, cmap, glyph_order) -> dict:
        """通过字形形状匹配还原汉字（复杂度高，需要参考字体）"""
        # 降级方案：如果能安装参照字体，可以逐 glyph 比较形状
        # 这里返回空，实际应用可接入 OCR 或字形匹配库
        return {}

    def _try_ssr_decode(self, html: str) -> str:
        """尝试从 SSR 数据解码（备选方案）"""
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', html, re.DOTALL)
        if not m:
            return html

        try:
            ssr = json.loads(m.group(1))
            reader = ssr.get("reader", {})
            content = reader.get("chapterData", {}).get("content", reader.get("content", ""))
            if content and any(0xE000 <= ord(c) <= 0xF8FF for c in content[:50]):
                return content  # 仍然是PUA，交给上层解码
            return content if content else html
        except Exception:
            return html

    def _apply_mapping(self, text: str, mapping: dict) -> str:
        """应用映射表解码文本"""
        result = []
        for ch in text:
            if ch in mapping:
                result.append(mapping[ch])
            else:
                result.append(ch)
        decoded = "".join(result)
        # 如果解码后中文比例极低，说明映射可能无效
        chinese = sum(1 for c in decoded if '\u4e00' <= c <= '\u9fff')
        if chinese < 10 and len(decoded) > 100:
            return text  # 解码失败，返回原文
        return decoded


# ═══════════════════════════════════════
# 简化版：使用预训练映射表
# ═══════════════════════════════════════

def load_mapping(json_path: str) -> dict:
    """加载 PUA → 汉字映射表"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    mapping = {}
    for k, v in data.items():
        code = int(k.replace("U+", ""), 16)
        mapping[chr(code)] = v
    return mapping


def decode_with_mapping(text: str, mapping: dict) -> str:
    """用映射表解码"""
    return "".join(mapping.get(c, c) for c in text)


if __name__ == "__main__":
    # 测试：从 /tmp 加载预生成的映射表
    import sys
    if len(sys.argv) > 1:
        path = sys.argv[1]
        mapping = load_mapping(path)
        print(f"Loaded {len(mapping)} mappings")
        # 测试解码
        test = "旧钨丝灯黑线悬屋央"
        decoded = decode_with_mapping(test, mapping)
        print(f"Test: {decoded}")
