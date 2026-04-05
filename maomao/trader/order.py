# 下单执行引擎 — 基础交易动作
# v1.1 修复: execute()补tp/sl路由 + _close()支持方向过滤(平多/平空)
from trader.exchange import (
    get_client, get_mark_price, get_positions,
    fix_qty, fix_price, check_min_notional,
    set_leverage, set_margin_mode,
)


def execute(order: dict) -> str:
    action = order.get("action")

    if action == "open_long":
        return _open(order, side="long")
    elif action == "open_short":
        return _open(order, side="short")
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


# ============================================================
# 止盈止损 (新增路由)
# ============================================================

def _set_tp(order: dict) -> str:
    """止盈 — 从 order["price"] 取目标价"""
    symbol = order.get("symbol")
    tp_price = order.get("price")
    if not symbol:
        return "❌ 缺少交易标的（如 止盈 BTC 90000）"
    if not tp_price:
        return "❌ 缺少止盈价格（如 止盈 BTC 90000）"
    return set_take_profit(symbol, tp_price)


def _set_sl(order: dict) -> str:
    """止损 — 从 order["price"] 取目标价"""
    symbol = order.get("symbol")
    sl_price = order.get("price")
    if not symbol:
        return "❌ 缺少交易标的（如 止损 ETH 2800）"
    if not sl_price:
        return "❌ 缺少止损价格（如 止损 ETH 2800）"
    return set_stop_loss(symbol, sl_price)


# ============================================================
# 开仓
# ============================================================

def _open(order: dict, side: str) -> str:
    symbol = order.get("symbol")
    usdt = order.get("usdt")
    leverage = order.get("leverage", 10)
    margin_mode = order.get("margin_mode", "cross")
    price_type = order.get("price_type", "market")
    limit_price = order.get("price")

    if not symbol:
        return "❌ 缺少交易标的（如 BTC）"
    if not usdt:
        return "❌ 缺少投入金额（如 100）"

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
# 止盈 / 止损
# ============================================================

def set_take_profit(symbol: str, tp_price: float) -> str:
    client = get_client()
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

        order = client.new_order(
            symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
            stopPrice=price, quantity=qty, reduceOnly=True, timeInForce="GTE_GTC",
        )
        results.append(f"  ✅ {direction}仓止盈 @ {price} (订单 {order.get('orderId', '?')})")

    return f"🎯 止盈已设 {symbol}\n" + "\n".join(results)


def set_stop_loss(symbol: str, sl_price: float) -> str:
    client = get_client()
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

        order = client.new_order(
            symbol=symbol, side=close_side, type="STOP_MARKET",
            stopPrice=price, quantity=qty, reduceOnly=True, timeInForce="GTE_GTC",
        )
        results.append(f"  🛡️ {direction}仓止损 @ {price} (订单 {order.get('orderId', '?')})")

    return f"🛡️ 止损已设 {symbol}\n" + "\n".join(results)


# ============================================================
# 加仓
# ============================================================

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
    """
    direction=None  → 全平(原逻辑)
    direction="long" → 只平多仓
    direction="short" → 只平空仓
    """
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

        # 方向过滤: 指定了方向就只平对应方向
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

    return f"✅ 已平仓 {symbol}\n" + "\n".join(results)
