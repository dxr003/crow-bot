"""
开仓预览+确认流程 v2.0

流程：
  解析 → build_preview() → 发预览卡片 → 等待文本「确认」/「取消」
  确认 → execute() → 回执
  取消/超时60s → 取消

使用方式（在 core.py maomao 路由段调用）：
  from trader.preview import parse_for_preview, pop_pending, pop_latest_pending
"""

import uuid
from trader.parser import parse, is_trade_command
from trader.exchange import get_mark_price, fix_qty


# ── 待确认订单 {uid: order_dict} ──
_pending: dict[str, dict] = {}
_latest_uid: str | None = None   # 最近一笔待确认的 uid


def build_preview(order: dict) -> str:
    """
    生成预览卡片文本，包含：
    - 投入保证金 / 名义价值 / 标记价 / 预估数量 / 预估强平价 / 止盈止损
    """
    action      = order.get("action", "?")
    symbol      = order.get("symbol", "?")
    usdt        = order.get("usdt") or 0
    leverage    = order.get("leverage") or 10
    margin_mode = order.get("margin_mode", "cross")
    price_type  = order.get("price_type", "market")
    limit_price = order.get("price")
    tp          = order.get("tp_price")
    sl          = order.get("sl_price")

    direction_map = {
        "open_long":  "做多 📈",
        "open_short": "做空 📉",
        "add":        "加仓",
        "close":      "全平",
        "close_long": "平多",
        "close_short":"平空",
        "tp":         "设止盈",
        "sl":         "设止损",
    }
    direction   = direction_map.get(action, action)
    margin_text = "全仓" if margin_mode == "cross" else "逐仓"
    coin        = symbol.replace("USDT", "")

    lines = [f"📋 <b>开单预览</b>", ""]
    lines.append(f"<b>{symbol}</b>  {direction}  {leverage}x {margin_text}")
    lines.append("──────────────")

    liq_target = order.get("liq_target")

    if action in ("open_long", "open_short", "add") and liq_target:
        # ── 强平价反推模式 ──
        lines.append(f"强平目标：{liq_target}")
        try:
            from trader.exchange import get_balance
            entry = get_mark_price(symbol)
            lines.append(f"标记价：{entry}")
            mmr = 0.005
            if usdt:
                margin_usdt = usdt
                margin_label = f"{usdt} USDT"
            elif margin_mode == "cross":
                bal = get_balance()
                margin_usdt = bal["total"] * 0.95
                margin_label = f"{margin_usdt:.1f} USDT（余额×95%）"
            else:
                lines.append("⚠️ 逐仓模式请指定保证金金额（如加 100u）")
                margin_usdt = 0
                margin_label = "未指定"
            lines.append(f"保证金：{margin_label}")

            if action in ("open_long", "add"):
                denom = entry - liq_target * (1 - mmr)
            else:
                denom = liq_target * (1 + mmr) - entry

            if denom > 0 and margin_usdt > 0:
                qty_raw = margin_usdt / denom
                qty = fix_qty(symbol, qty_raw)
                nominal = qty * entry
                lev = max(1, min(125, round(nominal / margin_usdt)))
                lines.append(f"名义价值：~{nominal:.1f} USDT")
                lines.append(f"预估数量：{qty} {coin}")
                lines.append(f"自动杠杆：~{lev}x")
            else:
                lines.append("⚠️ 强平价设置无效（方向与价格矛盾）")
        except Exception as e:
            lines.append(f"⚠️ 预估失败: {e}")

    elif action in ("open_long", "open_short", "add") and usdt:
        # ── 普通模式（金额+杠杆）──
        nominal = usdt * leverage
        lines.append(f"投入保证金：{usdt} USDT")
        lines.append(f"名义价值：~{nominal:.1f} USDT")

        try:
            if price_type == "limit" and limit_price:
                ref_price = float(limit_price)
                lines.append(f"挂单价：{ref_price}")
            else:
                ref_price = get_mark_price(symbol)
                lines.append(f"标记价：{ref_price}")

            if ref_price:
                raw_qty = (usdt * leverage) / ref_price
                qty = fix_qty(symbol, raw_qty)
                lines.append(f"预估数量：{qty} {coin}")

                mmr = 0.005
                if action in ("open_long", "add"):
                    liq_est = ref_price * (1 - 1/leverage + mmr)
                    lines.append(f"预估强平：≈ {round(liq_est, 4)}")
                elif action == "open_short":
                    liq_est = ref_price * (1 + 1/leverage - mmr)
                    lines.append(f"预估强平：≈ {round(liq_est, 4)}")
        except Exception as e:
            lines.append(f"⚠️ 行情获取失败: {e}")

    elif action in ("close", "close_long", "close_short"):
        pct = order.get("pct")
        if pct:
            lines.append(f"平仓比例：{pct}%")
        else:
            lines.append(f"平仓：全部")

    if tp or sl:
        lines.append("──────────────")
        if tp:
            lines.append(f"止盈：{tp}")
        if sl:
            lines.append(f"止损：{sl}")

    lines.append("──────────────")
    lines.append("<i>回复「确认」执行，「取消」放弃，60s 超时自动取消</i>")

    return "\n".join(lines)


def register_pending(order: dict) -> str:
    """注册待确认订单，返回 uid，并记录为最新"""
    global _latest_uid
    uid = str(uuid.uuid4())[:8]
    _pending[uid] = order
    _latest_uid = uid
    return uid


def pop_pending(uid: str) -> dict | None:
    """取出并删除指定 uid 的订单"""
    global _latest_uid
    order = _pending.pop(uid, None)
    if order is not None and _latest_uid == uid:
        _latest_uid = None
    return order


def pop_latest_pending() -> dict | None:
    """取出并删除最新一笔待确认订单（文本确认用）"""
    global _latest_uid
    if _latest_uid is None:
        return None
    return pop_pending(_latest_uid)


def parse_for_preview(text: str):
    """
    解析交易指令，返回 (preview_text, uid) 或 None
    """
    if not is_trade_command(text):
        return None

    order = parse(text)
    if order is None:
        return None

    preview_text = build_preview(order)
    uid = register_pending(order)
    return preview_text, uid
