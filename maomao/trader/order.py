# 下单执行引擎 — 基础交易动作（市价/限价/止盈/止损/强平价/平仓）
from trader.exchange import (
    get_client, get_mark_price, get_positions,
    fix_qty, fix_price, check_min_notional,
    set_leverage, set_margin_mode,
)


def execute(order: dict) -> str:
    """
    执行交易指令。
    order 是 parser.parse() 返回的标准 JSON。
    返回人类可读的执行结果字符串。
    """
    action = order.get("action")

    if action == "open_long":
        return _open(order, side="long")
    elif action == "open_short":
        return _open(order, side="short")
    elif action == "add":
        return _add(order)
    elif action == "close":
        return _close(order)
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


# ── 市价开仓 ──────────────────────────────────────────
def _open(order: dict, side: str) -> str:
    """
    市价开多/开空。
    支持：市价 / 限价 / 强平价挂单。
    """
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

    # 设置杠杆和仓位模式
    set_margin_mode(symbol, margin_mode)
    set_leverage(symbol, leverage)

    mode_text = "全仓" if margin_mode == "cross" else "逐仓"
    order_side = "BUY" if side == "long" else "SELL"
    direction = "多" if side == "long" else "空"

    # ── 强平价挂单 ────────────────────────────────────
    if price_type == "liq":
        # 取对方持仓的强平价作为限价
        positions = get_positions(symbol)
        liq_price = None
        for pos in positions:
            amt = float(pos["positionAmt"])
            # 做多找空仓强平价，做空找多仓强平价
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

        result = client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type="LIMIT",
            price=liq_price,
            quantity=qty,
            timeInForce="GTC",
        )
        return (
            f"📌 强平价限价单 开{direction} {symbol}\n"
            f"   挂单价: {liq_price} (对手强平价)\n"
            f"   标记价: {mark}\n"
            f"   杠杆: {leverage}x | {mode_text}\n"
            f"   投入: {usdt} USDT | 数量: {qty}\n"
            f"   订单号: {result.get('orderId', '?')}"
        )

    # ── 限价挂单 ──────────────────────────────────────
    if price_type == "limit":
        if not limit_price:
            return "❌ 限价开单需要指定价格（如 限价 85000）"

        limit_price = fix_price(symbol, limit_price)
        raw_qty = (usdt * leverage) / limit_price
        qty = fix_qty(symbol, raw_qty)
        check_min_notional(symbol, qty, limit_price)

        result = client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type="LIMIT",
            price=limit_price,
            quantity=qty,
            timeInForce="GTC",
        )
        return (
            f"📌 限价单 开{direction} {symbol}\n"
            f"   挂单价: {limit_price}\n"
            f"   杠杆: {leverage}x | {mode_text}\n"
            f"   投入: {usdt} USDT | 数量: {qty}\n"
            f"   订单号: {result.get('orderId', '?')}"
        )

    # ── 市价开仓（默认）──────────────────────────────
    mark = get_mark_price(symbol)
    raw_qty = (usdt * leverage) / mark
    qty = fix_qty(symbol, raw_qty)
    check_min_notional(symbol, qty, mark)

    result = client.futures_create_order(
        symbol=symbol,
        side=order_side,
        type="MARKET",
        quantity=qty,
    )
    return (
        f"✅ 市价开{direction} {symbol}\n"
        f"   杠杆: {leverage}x | {mode_text}\n"
        f"   投入: {usdt} USDT | 数量: {qty}\n"
        f"   标记价: {mark}\n"
        f"   订单号: {result.get('orderId', '?')}"
    )


# ── 止盈单 ────────────────────────────────────────────
def set_take_profit(symbol: str, tp_price: float) -> str:
    """
    设置止盈单。根据现有持仓方向自动判断。
    指令示例：止盈 BTC 90000
    """
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

        order = client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=price,
            quantity=qty,
            reduceOnly=True,
            timeInForce="GTE_GTC",
        )
        results.append(
            f"  ✅ {direction}仓止盈 @ {price} (订单 {order.get('orderId', '?')})"
        )

    return f"🎯 止盈已设 {symbol}\n" + "\n".join(results)


# ── 止损单 ────────────────────────────────────────────
def set_stop_loss(symbol: str, sl_price: float) -> str:
    """
    设置止损单。根据现有持仓方向自动判断。
    指令示例：止损 BTC 80000
    """
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

        order = client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="STOP_MARKET",
            stopPrice=price,
            quantity=qty,
            reduceOnly=True,
            timeInForce="GTE_GTC",
        )
        results.append(
            f"  🛡️ {direction}仓止损 @ {price} (订单 {order.get('orderId', '?')})"
        )

    return f"🛡️ 止损已设 {symbol}\n" + "\n".join(results)


# ── 加仓 ──────────────────────────────────────────────
def _add(order: dict) -> str:
    """加仓：沿用现有持仓方向和杠杆"""
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


# ── 平仓 ──────────────────────────────────────────────
def _close(order: dict) -> str:
    """市价全平当前持仓"""
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
        close_side = "SELL" if amt > 0 else "BUY"
        qty = fix_qty(symbol, abs(amt))
        direction = "多" if amt > 0 else "空"

        result = client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="MARKET",
            quantity=qty,
            reduceOnly=True,
        )
        results.append(
            f"  ✅ 平{direction} {qty} (订单 {result.get('orderId', '?')})"
        )

    return f"✅ 已平仓 {symbol}\n" + "\n".join(results)
