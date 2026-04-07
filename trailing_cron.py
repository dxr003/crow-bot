#!/usr/bin/env python3
"""
trailing_cron.py — 移动止盈定时检查入口
cron: */5 * * * * cd /root/maomao && python3 trailing_cron.py >> logs/trailing.log 2>&1
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("trailing_cron")

from trader.trailing import check_all

if __name__ == "__main__":
    logger.info("=== 移动止盈检查 ===")
    triggered = check_all()
    if triggered:
        for t in triggered:
            logger.info(
                f"触发平仓: {t['symbol']} {t['side']} "
                f"浮盈{t['pnl_pct']}% 回撤{t['drawdown']}%"
            )
    else:
        logger.info("无触发")
    logger.info("=== 检查完成 ===")
