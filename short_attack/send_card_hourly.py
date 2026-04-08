#!/usr/bin/env python3
"""
send_card_hourly.py — 整点推送状态卡片
cron: 0 * * * * cd /root/short_attack && python3 send_card_hourly.py >> logs/card.log 2>&1
"""
import sys
import json
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parent))

import state as state_mgr
import notifier

snap = state_mgr.get_snapshot()
notifier.send_card(snap)

# 重置 last_card_at，避免 main.py 在整点后5分钟内重复推
s = state_mgr.load()
s["last_card_at"] = time.time()
state_mgr.save(s)

print(f"[{time.strftime('%H:%M:%S')}] 整点卡片已推送")
