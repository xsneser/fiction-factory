"""
NovelEngine E2E 页面一致性测试
测试所有页面路由、链接、内容完整性
"""
import requests
import re
from urllib.parse import urljoin

BASE = "http://localhost:58080"
s = requests.Session()
errors = []

def check(label, condition, detail=""):
    if not condition:
        msg = f"FAIL [{label}]: {detail}"
        errors.append(msg)
        print(f"  [FAIL] {msg}")
    else:
        print(f"  [OK] {label}")

def get(path):
    r = s.get(urljoin(BASE, path), timeout=10)
    return r

print("=" * 60)
print("NovelEngine E2E Page Test")
print("=" * 60)

# ═══ Check server alive ═══
r = get("/")
check("Server alive", r.status_code == 200, f"status={r.status_code}")
if r.status_code != 200:
    print("Server not running - aborting")
    exit(1)

# ═══ Test all pages ═══
pages = [
    ("Dashboard", "/", ["NovelEngine", "仪表盘"]),
    ("Books", "/books", ["书库", "book"]),
    ("Start New Book", "/books/start", ["启动新书", "form"]),
    ("Plots", "/plots", ["桥段库", "plot"]),
    ("Structures", "/structures", ["大纲库", "structure"]),
    ("Gags", "/gags", ["笑点库", "gag"]),
    ("Themes", "/themes", ["内涵库", "theme"]),
    ("Profiles", "/profiles", ["笔名档案", "profile"]),
    ("New Profile", "/profiles/new", ["创建笔名", "form"]),
    ("DeAI Test", "/deai", ["去AI", "测试"]),
    ("Review Test", "/review-test", ["审查", "测试"]),
    ("Scout", "/scout", ["侦察兵", "scout"]),
]

print("\n--- Page Routes ---")
for name, path, keywords in pages:
    r = get(path)
    check(f"{name} ({path}) → {r.status_code}",
          r.status_code == 200,
          f"got {r.status_code}")
    if r.status_code == 200:
        for kw in keywords:
            check(f"  '{kw}' in {name}",
                  kw.lower() in r.text.lower(),
                  f"keyword '{kw}' not found")

# ═══ API endpoints ═══
print("\n--- API Endpoints ---")
apis = [
    ("Engine Status", "/api/engine/status"),
]

for name, path in apis:
    r = get(path)
    check(f"{name} ({path}) → {r.status_code}",
          r.status_code == 200,
          f"got {r.status_code}")

# ═══ Sidebar consistency ═══
print("\n--- Sidebar Navigation ---")
expected_links = [
    ("/", "仪表盘"),
    ("/books/start", "启动新书"),
    ("/books", "书库"),
    ("/plots", "桥段库"),
    ("/structures", "大纲库"),
    ("/gags", "笑点库"),
    ("/themes", "内涵库"),
    ("/profiles", "笔名档案"),
    ("/scout", "侦察兵"),
    ("/deai", "去AI测试"),
    ("/review-test", "审查测试"),
]

# Check sidebar exists on dashboard
r = get("/")
sidebar_links = re.findall(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', r.text)
found_links = {href: text.strip() for href, text in sidebar_links}

for href, expected_text in expected_links:
    check(f"Sidebar link: {expected_text}",
          href in found_links or any(expected_text in t for t in found_links.values()),
          f"link to {href} with text '{expected_text}' not in sidebar")

# ═══ Book workflow consistency ═══
print("\n--- Book Workflow ---")
# Check if any books exist
r = get("/api/engine/status")
books = r.json() if r.status_code == 200 else []

if books:
    for book in books[:3]:
        bid = book["book_id"]
        # Check book detail page
        r = get(f"/books/{bid}")
        check(f"Book detail ({bid})", r.status_code == 200,
              f"got {r.status_code}")

        # Check continue page
        r = get(f"/books/{bid}/continue")
        check(f"Continue page ({bid})", r.status_code == 200,
              f"got {r.status_code}")

        # Check book title consistency
        if r.status_code == 200:
            check(f"Book title in continue page",
                  book["title"] in r.text or bid in r.text,
                  f"'{book['title']}' not found")
else:
    print("  (no books found - skipping book tests)")

# ═══ CSS/JS consistency ═══
print("\n--- Style Consistency ---")
css_classes = ['.card', '.btn-primary', '.tag', '.progress-bar', '.accordion']
for page_path in ["/", "/books", "/scout"]:
    r = get(page_path)
    if r.status_code == 200:
        for cls in css_classes:
            check(f"  {page_path} has {cls}",
                  cls in r.text,
                  f"missing {cls}")

# ═══ Report ═══
print("\n" + "=" * 60)
if errors:
    print(f"FAILED: {len(errors)} issues found")
    for e in errors:
        print(f"  {e}")
else:
    print("ALL CHECKS PASSED")
print("=" * 60)
