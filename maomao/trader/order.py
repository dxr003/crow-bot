"""
下单执行引擎 v1.2.2
止盈止损走 conditional order 端点

v1.2.2 变更:
  - set_take_profit / set_stop_loss 改用 place_conditional_order()
  - 修复 leverage 默认值 (or 10)
"""
from trader.exchange import (
    get_client, get_mark_price, get_positions, get_balance,
    fix_qty, fix_price, check_min_notional,
    set_leverage, set_margin_mode,
    place_conditional_order, cancel_all_orders,
)


def execute(order: dict) -> str:
    action = order.get("action")

    if action == "open_long":
        if order.get("liq_target"):
            return _open_liq(order, side="long")
        return _open_with_extras(order, side="long")
    elif action == "open_short":
        if order.get("liq_target"):
            return _open_liq(order, side="short")
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


def _dark_split(symbol: str, side: str, total_qty: float,
                reduce_only: bool = False) -> list[str]:
    """
    暗单：把 total_qty 随机拆成 3~5 份顺序执行，每份间随机延迟 1~3 秒。
    返回每笔子单的简短回执列表。
    """
    import random, time
    client  = get_client()
    n       = random.randint(3, 5)
    # 生成 n 个随机权重，归一化后得到各份比例
    weights = [random.random() for _ in range(n)]
    total_w = sum(weights)
    results = []
    remaining = total_qty
    for i, w in enumerate(weights):
        if i == n - 1:
            chunk = remaining          # 最后一份用剩余量，避免精度误差
        else:
            chunk = total_qty * w / total_w
        chunk = fix_qty(symbol, chunk)
        if float(chunk) <= 0:
            continue
        try:
            kwargs = dict(symbol=symbol, side=side, type="MARKET", quantity=chunk)
            if reduce_only:
                kwargs["reduceOnly"] = True
            resp = client.new_order(**kwargs)
            results.append(f"✅ 第{i+1}份 {chunk} (订单 {resp.get('orderId','?')})")
        except Exception as e:
            results.append(f"❌ 第{i+1}份失败: {e}")
        remaining = fix_qty(symbol, remaining - float(chunk))
        if i < n - 1:
            time.sleep(random.uniform(1, 3))
    return results


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

    dark = order.get("dark_order", False)
    if dark:
        sub = _dark_split(symbol, order_side, qty)
        sub_text = "\n".join(f"    {r}" for r in sub)
        return (
            f"🌑 暗单市价开{direction} {symbol}\n"
            f"   杠杆: {leverage}x | {mode_text}\n"
            f"   投入: {usdt}U | 总量: {qty}\n"
            f"   标记价: {mark}\n"
            f"{sub_text}"
        )

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
    pct    = order.get("pct")        # 平仓百分比，如 50 表示平50%
    dark   = order.get("dark_order", False)
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
        total_qty  = abs(amt)

        # 百分比平仓
        if pct and 0 < pct <= 100:
            total_qty = total_qty * pct / 100
        qty = fix_qty(symbol, total_qty)
        dir_text = "多" if amt > 0 else "空"
        pct_text = f" {pct}%" if pct and pct < 100 else ""

        if dark:
            # 暗单：随机拆 3~5 份顺序执行
            sub = _dark_split(symbol, close_side, qty, reduce_only=True)
            results.append(f"  🌑 暗单平{dir_text}{pct_text} {qty}（{len(sub)}份）")
            for r in sub:
                results.append(f"    {r}")
        else:
            resp = client.new_order(
                symbol=symbol, side=close_side, type="MARKET",
                quantity=qty, reduceOnly=True,
            )
            results.append(f"  ✅ 平{dir_text}{pct_text} {qty} (订单 {resp.get('orderId','?')})")

    if not results:
        dir_text = "多" if direction == "long" else "空"
        return f"⚠️ {symbol} 无{dir_text}仓可平"

    # 全平时撤关联挂单（百分比平仓不撤）
    if not pct or pct >= 100:
        try:
            cancel_all_orders(symbol)
            results.append(f"  🗑️ 关联挂单已全部撤销")
        except Exception as e:
            results.append(f"  ⚠️ 撤单失败: {e}")

    return f"✅ 已平仓 {symbol}\n" + "\n".join(results)


# ============================================================
# 强平价反推开单
# ============================================================

def _open_liq(order: dict, side: str) -> str:
    """
    强平价反推开单：
    - 不指定杠杆，由公式反推数量和实际杠杆
    - 默认保证金 = 总余额 × 95%，也可通过 usdt 字段指定
    - 公式（MMR=0.005）:
        做多: qty = margin / (entry - liq_target × (1 - MMR))
        做空: qty = margin / (liq_target × (1 + MMR) - entry)
    """
    symbol      = order.get("symbol")
    liq_target  = order.get("liq_target")
    usdt_spec   = order.get("usdt")
    margin_mode = order.get("margin_mode", "cross")
    MMR         = 0.005
    SLIPPAGE    = 0.01    # 1% 容错缓冲，抵消市价波动导致的实际强平偏差

    if not symbol:
        return "❌ 请指定币种"
    if not liq_target:
        return "❌ 请指定强平目标价（如：做多 SOL 强平 65）"

    try:
        entry = get_mark_price(symbol)
    except Exception as e:
        return f"❌ 获取价格失败: {e}"

    # 保证金
    if usdt_spec:
        margin = float(usdt_spec)
    elif margin_mode == "cross":
        bal    = get_balance()
        margin = bal["total"] * 0.95
    else:
        # 逐仓必须指定金额
        return "❌ 逐仓模式请指定保证金金额（如：逐仓 做多 SOL 强平 65 100u）"

    if margin <= 0:
        return "❌ 账户余额不足"

    # 反推数量（对 liq_target 加容错缓冲，保守方向偏移 0.5%）
    # 做多：把目标强平价压低一点 → 实际强平价 ≤ 用户设定值，留波动余量
    # 做空：把目标强平价抬高一点 → 实际强平价 ≥ 用户设定值，留波动余量
    if side == "long":
        liq_eff = liq_target * (1 - SLIPPAGE)
        denom   = entry - liq_eff * (1 - MMR)
        if denom <= 0:
            return f"❌ 强平价 {liq_target} 须低于当前价 {entry}（做多）"
    else:
        liq_eff = liq_target * (1 + SLIPPAGE)
        denom   = liq_eff * (1 + MMR) - entry
        if denom <= 0:
            return f"❌ 强平价 {liq_target} 须高于当前价 {entry}（做空）"

    qty_raw  = margin / denom
    qty      = fix_qty(symbol, qty_raw)
    nominal  = float(qty) * entry
    leverage = max(1, min(125, round(nominal / margin)))

    # 设置仓位模式 + 杠杆
    client = get_client()
    set_margin_mode(symbol, margin_mode)
    set_leverage(symbol, leverage)

    # 市价开单
    open_side = "BUY" if side == "long" else "SELL"
    try:
        resp = client.new_order(
            symbol=symbol, side=open_side,
            type="MARKET", quantity=qty,
        )
    except Exception as e:
        return f"❌ 开单失败: {e}"

    order_id   = resp.get("orderId", "?")
    direction  = "多" if side == "long" else "空"

    result = (
        f"✅ 强平价开{direction} {symbol}\n"
        f"   保证金: {margin:.1f}U | 数量: {qty}\n"
        f"   入场价: ~{entry} | 强平目标: {liq_target}\n"
        f"   自动杠杆: {leverage}x\n"
        f"   订单号: {order_id}"
    )

    # 附加止盈止损
    extras = []
    if order.get("tp_price"):
        try:
            r = set_take_profit(symbol, order["tp_price"])
            extras.append(r)
        except Exception as e:
            extras.append(f"  ⚠️ 止盈失败: {e}")
    if order.get("sl_price"):
        try:
            r = set_stop_loss(symbol, order["sl_price"])
            extras.append(r)
        except Exception as e:
            extras.append(f"  ⚠️ 止损失败: {e}")

    if extras:
        result += "\n---\n" + "\n".join(extras)

    return result
