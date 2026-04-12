#!/usr/bin/env python3
"""
bull_sniper buyer.py — 买入执行模块

直接调用底座 exchange.py 下单，不走 parser/router。
流程：检查风控 → 设杠杆 → 滑点检查 → 市价开多 → 挂止损 → 返回结果

mode 控制：
  off   → 纯记录不下单
  alert → 推送等待人工确认（TBD）
  auto  → 自动执行
"""
import logging
import sys
import os
import time

# 把 /root/maomao 加入 sys.path，让 from trader.xxx 可用
sys.path.insert(0, "/root/maomao")

from trader.exchange import (
    get_client, get_mark_price, get_positions,
    fix_qty, fix_price, check_min_notional,
    set_leverage, set_margin_mode,
    place_conditional_order,
)

logger = logging.getLogger("bull_buyer")

FAPI_BASE = "https://fapi.binance.com"


def _get_balance() -> dict:
    """查合约账户余额（兼容 SDK 4.x account() 方法名）"""
    client = get_client()
    info = client.account()
    return {
        "total":     float(info["totalWalletBalance"]),
        "available": float(info["availableBalance"]),
        "upnl":      float(info["totalUnrealizedProfit"]),
    }


def execute(symbol: str, price: float, analyze_result: dict, cfg: dict) -> dict:
    """
    执行买入

    参数:
      symbol: 交易对 (e.g. XYZUSDT)
      price: 当前价格
      analyze_result: analyzer返回的完整结果
      cfg: bull_sniper配置

    返回:
      {"status": "skipped"/"executed"/"error", "reason": str, "order_id": str|None}
    """
    mode = cfg.get("mode", "off")

    if mode == "off":
        logger.info(f"[buyer] {symbol} mode=off, 纯记录不下单")
        return {"status": "skipped", "reason": "mode=off纯记录阶段", "order_id": None}

    if mode == "alert":
        logger.info(f"[buyer] {symbol} mode=alert, 推送等待人工确认")
        return {"status": "skipped", "reason": "mode=alert等待人工确认", "order_id": None}

    # ── mode == "auto" ──
    try:
        return _execute_auto(symbol, price, analyze_result, cfg)
    except Exception as e:
        logger.error(f"[buyer] {symbol} 执行异常: {e}")
        return {"status": "error", "reason": str(e), "order_id": None}


def _execute_auto(symbol: str, price: float, analyze_result: dict, cfg: dict) -> dict:
    """自动模式执行买入"""

    # ── 1. 持仓数检查 ──
    max_positions = cfg.get("max_concurrent_positions", 3)
    all_positions = get_positions()
    long_positions = [p for p in all_positions if float(p["positionAmt"]) > 0]

    if len(long_positions) >= max_positions:
        return {
            "status": "skipped",
            "reason": f"多头持仓已达上限{max_positions}个",
            "order_id": None,
        }

    # ── 2. 重复持仓检查 ──
    for p in long_positions:
        if p["symbol"] == symbol:
            return {
                "status": "skipped",
                "reason": f"{symbol}已有多头持仓",
                "order_id": None,
            }

    # ── 3. 余额检查 ──
    balance = _get_balance()
    position_usd = cfg.get("position_usd", 20)

    if balance["available"] < position_usd:
        return {
            "status": "skipped",
            "reason": f"可用余额{balance['available']:.1f}U不足{position_usd}U",
            "order_id": None,
        }

    # ── 4. 安全阀 ──
    max_position = cfg.get("max_position_usd", 50000)
    if position_usd > max_position:
        return {
            "status": "error",
            "reason": f"仓位{position_usd}U超安全阀{max_position}U",
            "order_id": None,
        }

    # ── 5. 设置杠杆和保证金模式 ──
    leverage = cfg.get("default_leverage", 5)
    set_margin_mode(symbol, "cross")
    set_leverage(symbol, leverage)

    # ── 6. 滑点检查（orderbook买卖价差） ──
    mark = get_mark_price(symbol)
    max_slippage = cfg.get("max_slippage_pct", 1)

    try:
        import requests as req
        book = req.get(
            f"{FAPI_BASE}/fapi/v1/depth",
            params={"symbol": symbol, "limit": 5},
            timeout=5,
        ).json()
        best_ask = float(book["asks"][0][0])
        spread_pct = (best_ask - mark) / mark * 100
    except Exception:
        spread_pct = 0  # 获取失败不阻止下单

    actual_position_usd = position_usd

    if spread_pct > max_slippage * 2:
        return {
            "status": "skipped",
            "reason": f"滑点{spread_pct:.2f}%超{max_slippage*2}%上限",
            "order_id": None,
        }
    elif spread_pct > max_slippage:
        actual_position_usd = position_usd / 2
        logger.info(f"[buyer] {symbol} 滑点{spread_pct:.2f}%>1%, 仓位减半→{actual_position_usd}U")

    # ── 7. 计算数量 ──
    raw_qty = (actual_position_usd * leverage) / mark
    qty = fix_qty(symbol, raw_qty)

    try:
        check_min_notional(symbol, qty, mark)
    except ValueError as e:
        return {"status": "error", "reason": str(e), "order_id": None}

    # ── 8. 市价开多 ──
    client = get_client()
    try:
        result = client.new_order(
            symbol=symbol,
            side="BUY",
            type="MARKET",
            quantity=qty,
        )
        order_id = str(result.get("orderId", "?"))
    except Exception as e:
        return {"status": "error", "reason": f"下单失败: {e}", "order_id": None}

    logger.info(
        f"[buyer] ✅ {symbol} 开多 {qty} @ ~{mark} "
        f"{leverage}x {actual_position_usd}U 订单:{order_id}"
    )

    # ── 9. 挂止损（保证金亏损30%触发） ──
    # 保证金亏损比例 = 价格跌幅 × 杠杆，所以价格跌幅 = 30% / 杠杆
    sl_margin_pct = 30  # 保证金最大亏损30%（50U亏15U）
    sl_price_drop = sl_margin_pct / leverage / 100  # 5x → 价格跌3%
    sl_price = fix_price(symbol, mark * (1 - sl_price_drop))

    sl_result = None
    try:
        time.sleep(1)  # 等开仓成交
        sl_resp = place_conditional_order(
            symbol=symbol,
            side="SELL",
            order_type="STOP_MARKET",
            trigger_price=sl_price,
            close_position=True,
        )
        sl_result = str(sl_resp.get("algoId", "?"))
        logger.info(f"[buyer] 🛡️ {symbol} 止损挂单 @ {sl_price} algoId:{sl_result}")
    except Exception as e:
        logger.warning(f"[buyer] ⚠️ {symbol} 止损挂单失败: {e}")

    # ── 10. 开启移动止盈追踪（v3.1：浮盈≥40%激活，回撤25%全平） ──
    trailing_result = None
    try:
        from trader.trailing import activate as trailing_activate
        trailing_cfg = cfg.get("trailing", {})
        activation_pct = trailing_cfg.get("activation_pct", 40)
        trailing_result = trailing_activate(symbol, threshold=activation_pct)
        logger.info(f"[buyer] 📊 {symbol} 移动止盈已挂载 激活阈值:{activation_pct}%")
    except Exception as e:
        logger.warning(f"[buyer] ⚠️ {symbol} 移动止盈挂载失败: {e}")

    # ── 11. 开启滚仓监控（v2.0：浮盈≥50%加仓70%利润，全仓模式） ──
    rolling_result = None
    try:
        import json
        from pathlib import Path
        roll_file = Path("/root/short_attack/data/roll_watch.json")
        roll_file.parent.mkdir(parents=True, exist_ok=True)
        watch = json.loads(roll_file.read_text()) if roll_file.exists() else []
        if symbol not in watch:
            watch.append(symbol)
            roll_file.write_text(json.dumps(watch, ensure_ascii=False))
            rolling_result = "已加入监控"
            logger.info(f"[buyer] 🔄 {symbol} 滚仓监控已挂载（浮盈≥50%触发）")
        else:
            rolling_result = "已在监控中"
    except Exception as e:
        logger.warning(f"[buyer] ⚠️ {symbol} 滚仓监控挂载失败: {e}")

    return {
        "status": "executed",
        "reason": (
            f"开多 {symbol} {qty} @ ~{mark} "
            f"{leverage}x {actual_position_usd}U "
            f"止损:{sl_price} 移动止盈:已挂载 滚仓:已挂载"
        ),
        "order_id": order_id,
        "sl_price": sl_price,
        "sl_algo_id": sl_result,
        "trailing": trailing_result,
        "rolling": rolling_result,
    }
