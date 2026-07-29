"""
测试番茄阅读器页面爬取
用法：python test_reader.py [reader_url]
"""
import sys, json, re, requests

url = sys.argv[1] if len(sys.argv) > 1 else "https://fanqienovel.com/reader/7173216089122439711"

s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
})

print(f"Fetching: {url}")
r = s.get(url, timeout=15)
print(f"Status: {r.status_code}, Size: {len(r.text)} bytes")

# Extract SSR data
m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', r.text, re.DOTALL)
if m:
    ssr = json.loads(m.group(1))
    print(f"\nSSR keys: {list(ssr.keys())}")

    # Try to find chapter content
    reader = ssr.get("reader", ssr.get("page", {}))
    if isinstance(reader, dict):
        print(f"Reader keys: {list(reader.keys())[:20]}")

        # Look for content
        for key in ["content", "chapterContent", "chapterData"]:
            val = reader.get(key)
            if val:
                if isinstance(val, str) and len(val) > 50:
                    print(f"\nFound content at reader.{key}:")
                    print(f"  Length: {len(val)} chars")
                    print(f"  Preview: {val[:300]}")
                elif isinstance(val, dict):
                    print(f"\nreader.{key} is dict with keys: {list(val.keys())[:10]}")
                    for sub in val:
                        sv = val[sub]
                        if isinstance(sv, str) and len(sv) > 50:
                            print(f"  reader.{key}.{sub}: {sv[:200]}...")
                            break

    # Also check page
    page = ssr.get("page", {})
    if isinstance(page, dict):
        print(f"\nPage keys: {list(page.keys())[:20]}")
        for key in ["content", "chapterContent", "text"]:
            val = page.get(key)
            if val and isinstance(val, str) and len(val) > 50:
                print(f"Page.{key}: {val[:200]}...")

    # Scan for any large text fields
    print("\nScanning for text content...")
    def scan(obj, path=""):
        if isinstance(obj, str) and len(obj) > 200:
            print(f"  {path}: {len(obj)} chars — {obj[:100]}...")
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k not in ("thumbUri", "avatarUri", "cssLinks", "jsLinks"):
                    scan(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list) and len(obj) > 0:
            if isinstance(obj[0], dict):
                print(f"  {path}: list of {len(obj)} dicts, keys={list(obj[0].keys())[:8]}")
                # Scan first item for text
                scan(obj[0], f"{path}[0]")
    scan(ssr)

else:
    print("No SSR data found")
    # Look for other patterns
    for pattern in ["content", "chapterContent", "chapterText", "articleBody"]:
        m2 = re.search(rf'["\']{pattern}["\']\s*:\s*"([^"]{{50,}})"', r.text)
        if m2:
            print(f"Found {pattern}: {m2.group(1)[:200]}")
