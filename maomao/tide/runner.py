#!/usr/bin/env python3
"""潮汐(Tide) 主入口 v2.0 — 13项核心因子，影子盘模式"""
import yaml
import time
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
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("tide.runner")
dec_log = logging.getLogger("tide.decisions")
dec_log.addHandler(logging.FileHandler(DEC_LOG))
dec_log.propagate = False

BJ = timezone(timedelta(hours=8))

import sys
sys.path.insert(0, str(BASE_DIR))
from modules.data_layer import fetch_price, fetch_klines_1m, fetch_klines_4h, fetch_oi, fetch_funding_rate, read_state, write_state
from modules.zone_layer import get_zone, distance_to_center, calc_small_box
from modules.decision_engine import make_decision


TOTAL_CAPITAL = 500.0


def sync_remaining(state: dict) -> dict:
    """jc4: 每次加仓/减仓后重算 remaining_usd"""
    total_used = state.get("position_structure", {}).get("total_usd", 0)
    state["position_structure"]["remaining_usd"] = round(TOTAL_CAPITAL - total_used, 2)
    return state


def main():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    mode = cfg["system"]["mode"]
    log.info(f"潮汐 v{cfg['system']['version']} mode={mode} 启动")

    prev_segment = None
    last_small_box_update = 0  # epoch秒，0表示从未更新

    while True:
        try:
            # 每4小时更新小箱参数
            now_ts = time.time()
            if now_ts - last_small_box_update >= 4 * 3600:
                try:
                    candles_4h = fetch_klines_4h(limit=20)
                    m_upper = cfg["box"]["mother"]["upper"]
                    m_lower = cfg["box"]["mother"]["lower"]
                    small_box = calc_small_box(candles_4h, mother_upper=m_upper, mother_lower=m_lower)
                    state = read_state()
                    state["small_box"] = {
                        "upper": small_box["small_box_upper"],
                        "lower": small_box["small_box_lower"],
                        "mid":   small_box["small_box_mid"],
                        "updated_at": datetime.now(BJ).isoformat(),
                    }
                    write_state(state)
                    last_small_box_update = now_ts
                    log.info(f"[小箱更新] upper={small_box['small_box_upper']:.0f} lower={small_box['small_box_lower']:.0f} mid={small_box['small_box_mid']:.0f}")
                except Exception as e:
                    log.warning(f"小箱更新失败: {e}")

            # sj1: 价格 + K线
            price, pct = fetch_price()
            klines = fetch_klines_1m(limit=21)

            # sj3: OI
            try:
                _, oi_change_pct = fetch_oi()
            except Exception as e:
                log.warning(f"sj3 OI拉取失败: {e}")
                oi_change_pct = 0.0

            # sj4: 资金费率
            try:
                funding_rate = fetch_funding_rate()
            except Exception as e:
                log.warning(f"sj4 费率拉取失败: {e}")
                funding_rate = 0.0

            # fk1: breach_factors 自动计算
            avg_vol = sum(k["volume"] for k in klines[:-1]) / max(len(klines) - 1, 1)
            volume_ratio = klines[-1]["volume"] / avg_vol if avg_vol > 0 else 0.0
            breach_factors = {
                "price_breach":    abs(pct) >= 1.5,
                "volume_spike":    volume_ratio >= 2.0,
                "oi_change":       abs(oi_change_pct) >= 5.0,
                "funding_extreme": abs(funding_rate) >= 0.05,
            }

            # xt1: 区段判断
            zone = get_zone(price)
            dist = distance_to_center(price)

            state = read_state()
            state["current_price"] = price
            state["price_change_pct"] = pct
            state["current_segment"] = zone["name"]
            state["last_update"] = datetime.now(BJ).isoformat()
            if price > state.get("price_peak", 0):
                state["price_peak"] = price
            state["breach_factors"] = breach_factors
            write_state(state)

            log.info(
                f"BTC ${price:,.0f} ({pct:+.2f}%) "
                f"{zone['emoji']}{zone['label']} "
                f"偏离={dist:+.1f}% "
                f"OI{oi_change_pct:+.1f}% 费率{funding_rate*100:.3f}%"
            )

            # 区段变化时出决策
            if zone["name"] != prev_segment:
                if prev_segment is not None:
                    log.info(f"[区段变化] {prev_segment} → {zone['name']}")

                decision = make_decision(
                    price, zone, state,
                    klines=klines,
                    oi_change_pct=oi_change_pct,
                    funding_rate=funding_rate,
                )

                ts = datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
                dec_log.info(
                    f"{ts} | ${price:,.0f} | {zone['name']} | "
                    f"{decision['action']} | {decision['reason']}"
                )
                log.info(f"[决策] {decision['label']} — {decision['reason']}")

                # 优先级决策：破箱 > 买回 > 减仓 > 加仓
                from modules.close_layer import check_force_flat
                from modules.reduce_layer import should_reduce, calc_reduce_usd
                from modules.add_layer import should_add, should_buyback, calc_add_usd

                flat, flat_reason = check_force_flat(state, cfg)
                if flat:
                    dec_log.info(f"{ts} | DECISION | FK5 | {flat_reason}")

                elif should_buyback(state)[0]:
                    _, usd, bb_reason = should_buyback(state)
                    dec_log.info(f"{ts} | DECISION | BUYBACK | usd={usd} | {bb_reason}")

                elif should_reduce(decision, state)[0]:
                    _, red_reason = should_reduce(decision, state)
                    usd = calc_reduce_usd(decision["action"], state)
                    dec_log.info(f"{ts} | DECISION | JS1 | {red_reason} | usd={usd:.1f}")
                    from modules.reduce_layer import check_trailing_stop
                    trail_ok, trail_reason, trail_usd = check_trailing_stop(state)
                    if trail_ok:
                        dec_log.info(f"{ts} | DECISION | TRAIL_STOP | {trail_reason}")
                    state = sync_remaining(state)
                    write_state(state)

                elif should_add(decision, state)[0]:
                    _, add_reason = should_add(decision, state)
                    usd = calc_add_usd(decision["action"])
                    dec_log.info(f"{ts} | DECISION | JC1 | {add_reason} | usd={usd:.1f}")
                    state = sync_remaining(state)
                    write_state(state)

                else:
                    dec_log.info(f"{ts} | DECISION | WAIT | {decision['action']}")

                if mode == "shadow":
                    log.info("[影子盘] 只记录，不执行")

                prev_segment = zone["name"]

        except Exception as e:
            log.error(f"主循环异常: {e}")

        time.sleep(60)


if __name__ == "__main__":
    main()
