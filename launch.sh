#!/bin/bash
# ═══════════════════════════════════════
# NovelEngine 一键启动脚本 (Linux / WSL2)
# ═══════════════════════════════════════
set -e

cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   📖 NovelEngine — 小说工厂 v2.0   ║"
echo "╚══════════════════════════════════════╝"
echo ""

# 颜色
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

# ── Python 检查 ──
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}❌ 未找到 python3${NC}"
    exit 1
fi
echo -e "${GREEN}✅${NC} $(python3 --version)"

# ── api.json 检查 ──
if [ ! -f "api.json" ]; then
    echo -e "${YELLOW}⚠️  未检测到 api.json${NC}"
    if [ -f "api.example.json" ]; then
        cp api.example.json api.json
        echo -e "${YELLOW}⚠️  已从模板复制 api.json，请编辑填入 API Key 后重新运行${NC}"
        exit 1
    fi
fi

# ── 依赖安装 ──
echo ""
echo "📦 检查依赖..."
python3 -c "import flask" 2>/dev/null || pip install flask -q
python3 -c "import fastapi" 2>/dev/null || pip install fastapi uvicorn -q
python3 -c "import openai" 2>/dev/null || pip install openai -q
[ -f "requirements.txt" ] && pip install -r requirements.txt -q 2>/dev/null
echo -e "${GREEN}✅ 依赖就绪${NC}"

# ── 启动 ──
echo ""
echo "═══════════════════════════════════════"
echo "  🚀 启动 Web 管理面板"
echo "  📡 http://localhost:58080"
echo "  ⏹  Ctrl+C 停止"
echo "═══════════════════════════════════════"
echo ""

python3 ui/web_ui.py
