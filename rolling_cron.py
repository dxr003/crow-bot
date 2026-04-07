#!/usr/bin/env python3
"""
rolling_cron.py — 滚仓自动巡检
cron: */30 * * * * cd /root/maomao && python3 rolling_cron.py >> logs/rolling.log 2>&1
"""
import os
import sys
import json
import logging
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("rolling_cron")

WATCH_FILE = Path("/root/short_attack/data/roll_watch.json")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "509640925")


def _notify(text: str):
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass


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
            if not get_positions(symbol):
                logger.info(f"{symbol} 持仓消失，移出滚仓名单")
                _notify(f"ℹ️ {symbol.replace('USDT','')} 持仓已消失，滚仓监控自动解除")
                to_remove.append(symbol)
                continue

            result = execute_roll(symbol)
            logger.info(f"{symbol}: {result[:80]}")

            if result.startswith("✅"):
                _notify(f"🔄 自动滚仓执行\n{result}")
                to_remove.append(symbol)

        except Exception as e:
            logger.warning(f"{symbol} 滚仓检查出错: {e}")

    if to_remove:
        watch = [s for s in watch if s not in to_remove]
        WATCH_FILE.write_text(json.dumps(watch, ensure_ascii=False))

    logger.info("=== 巡检完成 ===")


if __name__ == "__main__":
    main()
