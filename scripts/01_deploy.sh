#!/bin/bash
# ============================================================
# 01_deploy.sh — 三Bot文件部署 + 服务注册
# 前提: 00_init.sh 已执行，三个 .env 已填写
# ============================================================
set -e

check_env() {
    local f=$1
    if [ ! -f "$f" ]; then
        echo "❌ 缺少 $f，请先创建并填写"
        exit 1
    fi
    if grep -q "【" "$f"; then
        echo "❌ $f 还有未填写的占位符【...】"
        exit 1
    fi
}

echo ">>> [1/5] 检查 .env 文件..."
check_env /root/damao/.env
check_env /root/maomao/.env
check_env /root/baobao/.env
echo "✅ .env 检查通过"

echo ">>> [2/5] 复制 bot.py..."
cp damao/bot.py  /root/damao/bot.py
cp maomao/bot.py /root/maomao/bot.py
cp baobao/bot.py /root/baobao/bot.py

echo ">>> [3/5] 语法检查..."
/root/damao/venv/bin/python  -m py_compile /root/damao/bot.py  && echo "✅ 大猫"
/root/maomao/venv/bin/python -m py_compile /root/maomao/bot.py && echo "✅ 毛毛"
/root/baobao/venv/bin/python -m py_compile /root/baobao/bot.py && echo "✅ 播报"

echo ">>> [4/5] 注册 systemd 服务..."
cat > /etc/systemd/system/damao.service << 'EOF'
[Unit]
Description=大猫 运维Bot
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=/root/damao
EnvironmentFile=/root/damao/.env
ExecStart=/root/damao/venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=damao

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/maomao.service << 'EOF'
[Unit]
Description=毛毛 交易Bot
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=/root/maomao
EnvironmentFile=/root/maomao/.env
ExecStart=/root/maomao/venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=maomao

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/baobao.service << 'EOF'
[Unit]
Description=播报 Bot
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=/root/baobao
EnvironmentFile=/root/baobao/.env
ExecStart=/root/baobao/venv/bin/python bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=baobao

[Install]
WantedBy=multi-user.target
EOF

echo ">>> [5/5] 启动服务..."
systemctl daemon-reload
for svc in damao maomao baobao; do
    systemctl enable $svc
    systemctl restart $svc
    sleep 2
    status=$(systemctl is-active $svc)
    if [ "$status" = "active" ]; then
        echo "✅ $svc: running"
    else
        echo "❌ $svc: $status"
        journalctl -u $svc -n 20 --no-pager
    fi
done

echo ""
echo "========================================"
echo "部署完成，发送 /ping 验证三个Bot响应"
echo ""
echo "查看日志:"
echo "  journalctl -u damao  -f"
echo "  journalctl -u maomao -f"
echo "  journalctl -u baobao -f"
echo "========================================"
