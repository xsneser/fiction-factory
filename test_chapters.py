import sys
from plugins.fanqie_scout import FanqieCrawler

# Windows 控制台默认 GBK，直接 print 中文/emoji/PUA 会崩，强制走 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

c = FanqieCrawler()

# Test download first chapter
ch_id = "7173216089122439711"
book_id = "7143038691944959011"
print(f"Downloading chapter {ch_id} from book {book_id}...")

content = c.download_chapter(book_id, ch_id)
print(f"Content length: {len(content)}")
if content:
    import re
    wc = len(re.findall(r'[\u4e00-\u9fff]', content))
    print(f"Chinese chars: {wc}")
    print(f"Preview: {content[:300]}")
    # Check for PUA
    pua_count = sum(1 for ch in content if 0xE000 <= ord(ch) <= 0xF8FF)
    print(f"PUA chars: {pua_count}")
else:
    print("EMPTY - download failed")
    
    # Debug: try manual fetch
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    r = s.get(f"https://fanqienovel.com/reader/{ch_id}", timeout=15)
    print(f"\nReader page status: {r.status_code}, length: {len(r.text)}")
    
    import re, json
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', r.text, re.DOTALL)
    if m:
        ssr = json.loads(m.group(1))
        print(f"SSR keys: {list(ssr.keys())}")
        reader = ssr.get("reader", {})
        print(f"Reader keys: {list(reader.keys())[:20]}")
        cd = reader.get("chapterData", {})
        if isinstance(cd, dict):
            print(f"chapterData keys: {list(cd.keys())}")
            content_field = cd.get("content", "")
            if content_field:
                print(f"content preview: {content_field[:200]}")
                pua = sum(1 for ch in content_field if 0xE000 <= ord(ch) <= 0xF8FF)
                print(f"PUA chars in content: {pua}")
