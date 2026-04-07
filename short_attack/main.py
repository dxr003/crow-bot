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


def _notify_private(text: str):
    """推私信给乌鸦"""
    token   = os.getenv("BOT_TOKEN", "")
    chat_id = os.getenv("OWNER_CHAT_ID", "509640925")
    if not token:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def _auto_trade(sig: dict):
    """
    自动下单入口（AUTO_TRADE=true 时调用）
    开空 → 绑定移动止盈 → 登记滚仓
    """
    symbol    = sig["symbol"]
    liq_price = sig["liq_price"]
    usdt      = float(os.getenv("AUTO_TRADE_USDT",    100))
    leverage  = int(os.getenv("AUTO_TRADE_LEVERAGE",  10))
    logger.info(f"[AUTO_TRADE] 准备做空 {symbol} 强平价:{liq_price} {usdt}U {leverage}x")

    sys.path.insert(0, "/root/maomao")
    from trader.order import execute
    from trader.trailing import activate as trailing_activate
    from trader.rolling import ROLL_FILE
    import json, pathlib

    # 1. 开空（逐仓，强平价反推，暗单）
    order = {
        "action":      "open_short",
        "symbol":      symbol,
        "price_type":  "liq",
        "liq_target":  liq_price,
        "margin_mode": "isolated",
        "usdt":        usdt,
        "leverage":    leverage,
        "dark_order":  True,
    }
    result = execute(order)
    logger.info(f"[AUTO_TRADE] 开仓结果: {result}")

    if "❌" in result:
        logger.error(f"[AUTO_TRADE] 开仓失败，跳过技能绑定")
        return

    # 2. 推私信回执
    _notify_private(
        f"🤖 自动开空执行 — {symbol.replace('USDT','')}\n"
        f"金额: {usdt}U  杠杆: {leverage}x  逐仓\n"
        f"强平价: {liq_price}\n"
        f"{result}"
    )

    # 3. 绑定移动止盈（默认40%激活）
    try:
        msg = trailing_activate(symbol)
        logger.info(f"[AUTO_TRADE] 移动止盈: {msg}")
        _notify_private(f"📌 移动止盈已绑定\n{msg}")
    except Exception as e:
        logger.warning(f"[AUTO_TRADE] 移动止盈绑定失败: {e}")

    # 4. 登记滚仓（写入滚仓监控名单）
    try:
        roll_list_file = pathlib.Path("/root/short_attack/data/roll_watch.json")
        roll_list = json.loads(roll_list_file.read_text()) if roll_list_file.exists() else []
        if symbol not in roll_list:
            roll_list.append(symbol)
        roll_list_file.write_text(json.dumps(roll_list, ensure_ascii=False))
        logger.info(f"[AUTO_TRADE] 滚仓登记: {symbol}")
    except Exception as e:
        logger.warning(f"[AUTO_TRADE] 滚仓登记失败: {e}")


if __name__ == "__main__":
    main()
