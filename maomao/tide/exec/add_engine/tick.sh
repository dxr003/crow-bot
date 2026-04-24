#!/bin/bash
# 加仓引擎 cron 入口：每分钟一跑，engine.enabled=false 时直接返回，零影响。
# 启用方法：改 /root/maomao/tide/exec/add_engine/add_rules.yaml 的 engine.enabled: true
cd /root/maomao || exit 1
LOG=/root/maomao/tide/logs/add_engine_cron.log
TS=$(date '+%Y-%m-%d %H:%M:%S')
{
  echo "===== tick @ $TS ====="
  /root/maomao/venv/bin/python -m tide.exec.add_engine 2>&1
} >> "$LOG"
# 日志滚动：>2MB 时截断保留最后 500 行
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 2097152 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
