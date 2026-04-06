"""
下单执行引擎 v1.2.2
止盈止损走 conditional order 端点

v1.2.2 变更:
  - set_take_profit / set_stop_loss 改用 place_conditional_order()
  - 修复 leverage 默认值 (or 10)
"""
from trader.exchange import (
    get_client, get_mark_price, get_positions,
    fix_qty, fix_price, check_min_notional,
    set_leverage, set_margin_mode,
    place_conditional_order, cancel_all_orders,
)


def execute(order: dict) -> str:
    action = order.get("action")

    if action == "open_long":
        return _open_with_extras(order, side="long")
    elif action == "open_short":
        return _open_with_extras(order, side="short")
    elif action == "add":
        return _add(order)
    elif action == "close":
        return _close(order)
    elif action == "close_long":
        return _close(order, direction="long")
    elif action == "close_short":
        return _close(order, direction="short")
    elif action == "tp":
        return _set_tp(order)
    elif action == "sl":
        return _set_sl(order)
    elif action == "cancel_orders":
        return _cancel_orders(order)
    elif action == "buy":
        return "⚠️ 现货买入暂未开放"
    elif action == "sell":
        return "⚠️ 现货卖出暂未开放"
    elif action == "transfer":
        return "⚠️ 划转暂未开放"
    elif action in ("trailing", "roll"):
        return "⚠️ 移动止盈/滚仓功能开发中"
    else:
        return f"❌ 未知动作: {action}"


def _cancel_orders(order: dict) -> str:
    symbol = order.get("symbol")
    if not symbol:
        return "❌ 请指定币种，例如：撤单 SOL"
    try:
        cancel_all_orders(symbol)
        return f"✅ {symbol} 所有挂单已撤销"
    except Exception as e:
        return f"❌ 撤单失败: {e}"


# ============================================================
# 开仓 + 自动挂止盈止损
# ============================================================

def _open_with_extras(order: dict, side: str) -> str:
    result = _open(order, side=side)

    if result.startswith("❌"):
        return result

    symbol = order.get("symbol")
    extras = []

    tp = order.get("tp_price")
    if tp and symbol:
        try:
            tp_result = set_take_profit(symbol, tp)
            extras.append(tp_result)
        except Exception as e:
            extras.append(f"⚠️ 止盈挂单失败: {e}")

    sl = order.get("sl_price")
    if sl and symbol:
        try:
            sl_result = set_stop_loss(symbol, sl)
            extras.append(sl_result)
        except Exception as e:
            extras.append(f"⚠️ 止损挂单失败: {e}")

    if extras:
        result += "\n---\n" + "\n".join(extras)

    return result


# ============================================================
# 止盈止损路由
# ============================================================

def _set_tp(order: dict) -> str:
    symbol = order.get("symbol")
    tp_price = order.get("price") or order.get("tp_price")
    if not symbol:
        return "❌ 缺少交易标的（如 止盈 BTC 90000）"
    if not tp_price:
        return "❌ 缺少止盈价格（如 止盈 BTC 90000）"
    return set_take_profit(symbol, tp_price)


def _set_sl(order: dict) -> str:
    symbol = order.get("symbol")
    sl_price = order.get("price") or order.get("sl_price")
    if not symbol:
        return "❌ 缺少交易标的（如 止损 ETH 2800）"
    if not sl_price:
        return "❌ 缺少止损价格（如 止损 ETH 2800）"
    return set_stop_loss(symbol, sl_price)


# ============================================================
# 开仓 (核心)
# ============================================================

def _open(order: dict, side: str) -> str:
    symbol = order.get("symbol")
    usdt = order.get("usdt")
    leverage = order.get("leverage") or 10
    margin_mode = order.get("margin_mode", "cross")
    price_type = order.get("price_type", "market")
    limit_price = order.get("price")

    if not symbol:
        return "❌ 缺少交易标的（如 BTC）"
    if not usdt:
        return "❌ 缺少投入金额（如 100 或 100u）"

    client = get_client()
    set_margin_mode(symbol, margin_mode)
    set_leverage(symbol, leverage)

    mode_text = "全仓" if margin_mode == "cross" else "逐仓"
    order_side = "BUY" if side == "long" else "SELL"
    direction = "多" if side == "long" else "空"

    # ---- 强平价开单 ----
    if price_type == "liq":
        positions = get_positions(symbol)
        liq_price = None
        for pos in positions:
            amt = float(pos["positionAmt"])
            pos_side = "long" if amt > 0 else "short"
            if pos_side != side:
                liq_price = float(pos.get("liquidationPrice", 0))
                break

        if not liq_price or liq_price == 0:
            return "❌ 找不到对手仓位的强平价，请手动指定价格"

        liq_price = fix_price(symbol, liq_price)
        mark = get_mark_price(symbol)
        raw_qty = (usdt * leverage) / liq_price
        qty = fix_qty(symbol, raw_qty)
        check_min_notional(symbol, qty, liq_price)

        result = client.new_order(
            symbol=symbol, side=order_side, type="LIMIT",
            price=liq_price, quantity=qty, timeInForce="GTC",
        )
        return (
            f"📌 强平价限价单 开{direction} {symbol}\n"
            f"   挂单价: {liq_price} (对手强平价)\n"
            f"   标记价: {mark}\n"
            f"   杠杆: {leverage}x | {mode_text}\n"
            f"   投入: {usdt} USDT | 数量: {qty}\n"
            f"   订单号: {result.get('orderId', '?')}"
        )

    # ---- 限价开单 ----
    if price_type == "limit":
        if not limit_price:
            return "❌ 限价开单需要指定价格（如 限价 85000）"

        limit_price = fix_price(symbol, limit_price)
        raw_qty = (usdt * leverage) / limit_price
        qty = fix_qty(symbol, raw_qty)
        check_min_notional(symbol, qty, limit_price)

        result = client.new_order(
            symbol=symbol, side=order_side, type="LIMIT",
            price=limit_price, quantity=qty, timeInForce="GTC",
        )
        return (
            f"📌 限价单 开{direction} {symbol}\n"
            f"   挂单价: {limit_price}\n"
            f"   杠杆: {leverage}x | {mode_text}\n"
            f"   投入: {usdt} USDT | 数量: {qty}\n"
            f"   订单号: {result.get('orderId', '?')}"
        )

    # ---- 市价开单 ----
    mark = get_mark_price(symbol)
    raw_qty = (usdt * leverage) / mark
    qty = fix_qty(symbol, raw_qty)
    check_min_notional(symbol, qty, mark)

    result = client.new_order(
        symbol=symbol, side=order_side, type="MARKET", quantity=qty,
    )
    return (
        f"✅ 市价开{direction} {symbol}\n"
        f"   杠杆: {leverage}x | {mode_text}\n"
        f"   投入: {usdt} USDT | 数量: {qty}\n"
        f"   标记价: {mark}\n"
        f"   订单号: {result.get('orderId', '?')}"
    )


# ============================================================
# 止盈 / 止损 — 走 conditional order 端点
# ============================================================

def set_take_profit(symbol: str, tp_price: float) -> str:
    positions = get_positions(symbol)
    if not positions:
        return f"❌ {symbol} 无持仓，无法设止盈"

    results = []
    for pos in positions:
        amt = float(pos["positionAmt"])
        if amt == 0:
            continue
        side = "long" if amt > 0 else "short"
        close_side = "SELL" if side == "long" else "BUY"
        direction = "多" if side == "long" else "空"
        qty = fix_qty(symbol, abs(amt))
        price = fix_price(symbol, tp_price)

        resp = place_conditional_order(
            symbol=symbol,
            side=close_side,
            order_type="TAKE_PROFIT_MARKET",
            trigger_price=price,
            quantity=qty,
            reduce_only=True,
        )
        algo_id = resp.get("algoId", "?")
        results.append(f"  ✅ {direction}仓止盈 @ {price} (algoId {algo_id})")

    return f"🎯 止盈已设 {symbol}\n" + "\n".join(results)



def set_stop_loss(symbol: str, sl_price: float) -> str:
    positions = get_positions(symbol)
    if not positions:
        return f"❌ {symbol} 无持仓，无法设止损"

    results = []
    for pos in positions:
        amt = float(pos["positionAmt"])
        if amt == 0:
            continue
        side = "long" if amt > 0 else "short"
        close_side = "SELL" if side == "long" else "BUY"
        direction = "多" if side == "long" else "空"
        qty = fix_qty(symbol, abs(amt))
        price = fix_price(symbol, sl_price)

        resp = place_conditional_order(
            symbol=symbol,
            side=close_side,
            order_type="STOP_MARKET",
            trigger_price=price,
            quantity=qty,
            reduce_only=True,
        )
        algo_id = resp.get("algoId", "?")
        results.append(f"  🛡️ {direction}仓止损 @ {price} (algoId {algo_id})")

    return f"🛡️ 止损已设 {symbol}\n" + "\n".join(results)



def _add(order: dict) -> str:
    symbol = order.get("symbol")
    usdt = order.get("usdt")

    if not symbol:
        return "❌ 缺少交易标的"
    if not usdt:
        return "❌ 缺少加仓金额"

    positions = get_positions(symbol)
    if not positions:
        return f"❌ {symbol} 无持仓，无法加仓"

    pos = positions[0]
    amt = float(pos["positionAmt"])
    side = "long" if amt > 0 else "short"
    order["leverage"] = int(float(pos.get("leverage", 10)))

    return _open(order, side=side)


# ============================================================
# 平仓 (支持方向过滤)
# ============================================================

def _close(order: dict, direction: str | None = None) -> str:
    symbol = order.get("symbol")
    if not symbol:
        return "❌ 缺少交易标的"

    client = get_client()
    positions = get_positions(symbol)

    if not positions:
        return f"⚠️ {symbol} 当前无持仓"

    results = []
    for pos in positions:
        amt = float(pos["positionAmt"])
        pos_dir = "long" if amt > 0 else "short"

        if direction and pos_dir != direction:
            continue

        close_side = "SELL" if amt > 0 else "BUY"
        qty = fix_qty(symbol, abs(amt))
        dir_text = "多" if amt > 0 else "空"

        result = client.new_order(
            symbol=symbol, side=close_side, type="MARKET",
            quantity=qty, reduceOnly=True,
        )
        results.append(f"  ✅ 平{dir_text} {qty} (订单 {result.get('orderId', '?')})")

    if not results:
        dir_text = "多" if direction == "long" else "空"
        return f"⚠️ {symbol} 无{dir_text}仓可平"

    # 平仓后自动撤销关联挂单
    try:
        cancel_all_orders(symbol)
        results.append(f"  🗑️ 关联挂单已全部撤销")
    except Exception as e:
        results.append(f"  ⚠️ 撤单失败: {e}")

    return f"✅ 已平仓 {symbol}\n" + "\n".join(results)
