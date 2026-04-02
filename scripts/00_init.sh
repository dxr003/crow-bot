#!/bin/bash
# ============================================================
# 00_init.sh — 日本VPS 全新环境初始化
# 执行: bash 00_init.sh
# ============================================================
set -e

echo ">>> [1/4] 系统更新 + 基础依赖..."
apt-get update -y
apt-get install -y \
    python3 python3-pip python3-venv \
    ffmpeg git curl wget \
    build-essential libssl-dev libffi-dev python3-dev

echo ">>> [2/4] 创建三Bot目录..."
mkdir -p /root/damao/{logs,data,.claude/rules}
mkdir -p /root/maomao/{logs,data}
mkdir -p /root/baobao/{logs,data}

echo ">>> [3/4] 创建虚拟环境 + 安装依赖..."

# 大猫
python3 -m venv /root/damao/venv
/root/damao/venv/bin/pip install -q --upgrade pip
/root/damao/venv/bin/pip install -q \
    'python-telegram-bot[job-queue]==22.7' \
    anthropic python-dotenv

# 毛毛
python3 -m venv /root/maomao/venv
/root/maomao/venv/bin/pip install -q --upgrade pip
/root/maomao/venv/bin/pip install -q \
    'python-telegram-bot[job-queue]==22.7' \
    anthropic python-binance openai \
    pydub edge-tts python-dotenv httpx

# 播报
python3 -m venv /root/baobao/venv
/root/baobao/venv/bin/pip install -q --upgrade pip
/root/baobao/venv/bin/pip install -q \
    'python-telegram-bot[job-queue]==22.7' \
    python-dotenv

echo ">>> [4/4] 验证..."
/root/damao/venv/bin/python  -c "import telegram; print('大猫  PTB:', telegram.__version__)"
/root/maomao/venv/bin/python -c "import telegram; print('毛毛  PTB:', telegram.__version__)"
/root/baobao/venv/bin/python -c "import telegram; print('播报  PTB:', telegram.__version__)"
ffmpeg -version | head -1

echo ""
echo "========================================"
echo "✅ 环境就绪"
echo "   下一步: 填写三个 .env，执行 01_deploy.sh"
echo "========================================"
