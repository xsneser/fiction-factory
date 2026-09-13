#!/usr/bin/env bash
# NovelEngine 一键启动脚本 (Linux / WSL2)
set -e

cd "$(dirname "$0")"

echo ""
echo "========================================"
echo "   📖 NovelEngine — 小说工厂 v2.0"
echo "========================================"
echo ""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

# ── Python 检查 ──
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}❌ 未找到 python3${NC}"
    exit 1
fi
echo -e "${GREEN}✅${NC} $(python3 --version)"

# ── api.json 检查 ──
# 缺失时复制空模板后**继续启动**：API 地址与 Key 在 /settings 页面填写，
# 引擎与 dsh 都从这一份 api.json 读，不必先手工编辑文件。
if [ ! -f "api.json" ]; then
    if [ -f "api.example.json" ]; then
        cp api.example.json api.json
        echo -e "${YELLOW}ℹ️  已生成 api.json，请启动后在 http://localhost:58080/settings 配置 API 地址与 Key${NC}"
    fi
fi

# ── 依赖安装 ──
echo ""
echo "📦 检查依赖..."
python3 -c "import flask" 2>/dev/null || pip install flask -q
python3 -c "import requests" 2>/dev/null || pip install requests -q
python3 -c "import fontTools" 2>/dev/null || pip install fonttools -q
[ -f "requirements.txt" ] && pip install -r requirements.txt -q 2>/dev/null || true
echo -e "${GREEN}✅ 依赖就绪${NC}"

# ── 启动 ──
echo ""
echo "========================================"
echo "   🚀 启动 Web 管理面板"
echo "   📗 http://localhost:58080"
echo "   按 Ctrl+C 停止"
echo "========================================"
echo ""

python3 ui/web_ui.py
