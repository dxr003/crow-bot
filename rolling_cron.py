#!/usr/bin/env python3
"""
rolling_cron.py — 滚仓自动巡检
cron: */5 * * * * cd /root/maomao && python3 rolling_cron.py >> logs/rolling.log 2>&1

读取 /root/short_attack/data/roll_watch.json 里的名单
对每个币调 execute_roll()，浮盈不足50%自动跳过
"""
import sys
import json
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("rolling_cron")

WATCH_FILE = Path("/root/short_attack/data/roll_watch.json")

from trader.rolling import execute_roll
from trader.exchange import get_positions


def main():
    if not WATCH_FILE.exists():
        return

    watch = json.loads(WATCH_FILE.read_text())
    if not watch:
        return

    logger.info(f"=== 滚仓巡检 {len(watch)}个 ===")
    to_remove = []

    for symbol in watch:
        try:
            # 持仓消失则移出名单
            if not get_positions(symbol):
                logger.info(f"{symbol} 持仓消失，移出滚仓名单")
                to_remove.append(symbol)
                continue

            result = execute_roll(symbol)
            logger.info(f"{symbol}: {result[:60]}")

            # 滚仓成功后移出名单（避免无限滚）
            if result.startswith("✅"):
                to_remove.append(symbol)

        except Exception as e:
            logger.warning(f"{symbol} 滚仓检查出错: {e}")

    if to_remove:
        watch = [s for s in watch if s not in to_remove]
        WATCH_FILE.write_text(json.dumps(watch, ensure_ascii=False))

    logger.info("=== 巡检完成 ===")


if __name__ == "__main__":
    main()
