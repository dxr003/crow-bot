#!/usr/bin/env python3
"""
做空阻击系统 — 主入口
cron 每5分钟调用一次：*/5 * * * * cd /root/short_attack && python3 main.py
"""
import os
import sys
import time
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

# 日志
log_dir = Path("/root/short_attack/logs")
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_dir / "short_attack.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("short_attack")

import scanner
import state as state_mgr
import notifier


def main():
    logger.info("=== 做空阻击 扫描开始 ===")
    start = time.time()

    # 1. 获取行情数据
    try:
        tickers = scanner.get_ticker_24h()
        logger.info(f"获取到 {len(tickers)} 个合约行情")
    except Exception as e:
        logger.error(f"获取行情失败: {e}")
        sys.exit(1)

    # 2. 处理状态机，获取本次事件
    try:
        events = state_mgr.process_tick(tickers)
    except Exception as e:
        logger.error(f"状态处理失败: {e}")
        sys.exit(1)

    # 3. 新进入监控的币 — 补充辅助数据 + 立即推通知
    for mon in events["new_monitors"]:
        symbol = mon["symbol"]
        logger.info(f"新监控: {symbol} +{mon['change_pct']}%")
        try:
            # 补充资金费率和OI
            extra = scanner.get_signal_data(symbol)
            # 更新到state
            s = state_mgr.load()
            if symbol in s["monitoring"]:
                s["monitoring"][symbol]["funding_rate"]  = extra["funding_rate"]
                s["monitoring"][symbol]["oi_change_pct"] = extra["oi_change_pct"]
                state_mgr.save(s)
            mon["funding_rate"]  = extra["funding_rate"]
            mon["oi_change_pct"] = extra["oi_change_pct"]
        except Exception as e:
            logger.warning(f"获取{symbol}辅助数据失败: {e}")
        notifier.send_new_monitor(mon)

    # 4. 触发持仓信号的币 — 补充辅助数据 + 立即推醒目通知
    for sig in events["new_signals"]:
        symbol = sig["symbol"]
        logger.info(f"持仓信号: {symbol} 总涨+{sig['total_rise']}% 回撤-{sig['pullback_pct']}%")
        try:
            extra = scanner.get_signal_data(symbol)
            # 获取当前成交量
            ticker = next((t for t in tickers if t["symbol"] == symbol), None)
            sig["funding_rate"]  = extra["funding_rate"]
            sig["oi_change_pct"] = extra["oi_change_pct"]
            sig["volume_usdt"]   = ticker["volume_usdt"] if ticker else 0
            # 更新state
            s = state_mgr.load()
            if symbol in s["signals"]:
                s["signals"][symbol].update({
                    "funding_rate":  extra["funding_rate"],
                    "oi_change_pct": extra["oi_change_pct"],
                    "volume_usdt":   sig["volume_usdt"],
                })
                state_mgr.save(s)
        except Exception as e:
            logger.warning(f"获取{symbol}辅助数据失败: {e}")
        notifier.send_signal(sig)

        # 自动交易开关
        if os.getenv("AUTO_TRADE", "false").lower() == "true":
            try:
                _auto_trade(sig)
            except Exception as e:
                logger.error(f"自动下单失败 {symbol}: {e}")

    # 5. 退出事件日志
    for ex in events["new_exits"]:
        logger.info(f"退出: {ex['symbol']} {ex['reason']} 最高+{ex['max_pct']}% 出场+{ex['exit_pct']}%")

    # 6. 定时卡片
    if events["send_card"]:
        logger.info("推送定时状态卡片")
        snap = state_mgr.get_snapshot()
        notifier.send_card(snap)

    elapsed = round(time.time() - start, 2)
    logger.info(f"=== 扫描完成 {elapsed}s ===")


def _auto_trade(sig: dict):
    """
    自动下单入口（AUTO_TRADE=true 时调用）
    调用玄玄交易底盘的 execute() 函数
    """
    symbol    = sig["symbol"]
    liq_price = sig["liq_price"]
    logger.info(f"[AUTO_TRADE] 准备做空 {symbol} 强平价:{liq_price}")

    sys.path.insert(0, "/root/maomao")
    from trader.order import execute

    order = {
        "action":      "open",
        "symbol":      symbol,
        "side":        "short",
        "price_type":  "liq",
        "liq_target":  liq_price,
        "margin_mode": "cross",
        "usdt":        None,   # 由风控决定仓位大小，后续完善
    }
    result = execute(order)
    logger.info(f"[AUTO_TRADE] 下单结果: {result}")


if __name__ == "__main__":
    main()
