#!/usr/bin/env python3
"""潮汐(Tide) 主入口 — Phase 2+3 数据通道 + 决策引擎"""
import yaml
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "logs" / "main.log"
DEC_LOG = BASE_DIR / "logs" / "decisions.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tide.runner")
dec_log = logging.getLogger("tide.decisions")
dec_log.addHandler(logging.FileHandler(DEC_LOG))
dec_log.propagate = False

BJ = timezone(timedelta(hours=8))

import sys
sys.path.insert(0, str(BASE_DIR))
from modules.data_layer import fetch_price, read_state, write_state
from modules.zone_layer import get_zone, distance_to_center
from modules.decision_engine import make_decision


def main():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    log.info(f"Tide {cfg['system']['name']} v{cfg['system']['version']} mode={cfg['system']['mode']} 启动")

    prev_segment = None

    while True:
        try:
            price, pct = fetch_price()
            zone = get_zone(price)
            dist = distance_to_center(price)
            now = datetime.now(BJ).isoformat()

            state = read_state()
            state["current_price"] = price
            state["price_change_pct"] = pct
            state["current_segment"] = zone["name"]
            state["last_update"] = now
            write_state(state)

            log.info(f"BTC ${price:,.0f} ({pct:+.2f}%) 区段={zone['label']} 偏离中轴={dist:+.1f}%")

            if zone["name"] != prev_segment:
                if prev_segment is not None:
                    log.info(f"[区段变化] {prev_segment} → {zone['name']}")
                # 区段变化时出决策
                decision = make_decision(price, zone, state)
                _POS_CHANGE = {
                    "FORCE_FLAT": "全平",
                    "REDUCE_70": "-70% 持仓",
                    "REDUCE_50": "-50% 持仓",
                    "REDUCE_30": "-30% 持仓",
                    "NO_ACTION": "维持",
                    "ADD_1X":   "+1x base 加仓",
                    "ADD_1_5X": "+1.5x base 加仓",
                    "ADD_2X":   "+2x base 加仓",
                    "ADD_3X":   "+3x base 加仓",
                }
                ts = datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
                pos_change = _POS_CHANGE.get(decision["action"], decision["action"])
                dec_log.info(f"{ts} | {decision['zone_name']} | {decision['action']} | {pos_change}")
                log.info(f"[决策] {decision['label']} — {decision['reason']}")
                prev_segment = zone["name"]

        except Exception as e:
            log.error(f"主循环异常: {e}")

        time.sleep(60)


if __name__ == "__main__":
    main()
