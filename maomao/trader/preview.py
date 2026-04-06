"""
开仓预览+确认流程 v1.0

流程：
  解析 → preview_order() → 发预览卡片(InlineKeyboard) → 等60s确认
  ✅确认 → execute() → 回执
  ❌取消/超时 → 编辑消息为"已取消"

使用方式（在 core.py maomao 路由段调用）：
  from trader.preview import preview_order
  result = preview_order(text)
  if result is not None:
      # result = (preview_text, order_dict) 或 None
"""

import uuid
from trader.parser import parse, is_trade_command
from trader.exchange import get_mark_price, fix_qty


# ── 存储待确认订单 {uuid: order_dict} ──
_pending: dict[str, dict] = {}


def build_preview(order: dict) -> str:
    """
    生成预览卡片文本（不执行任何交易所操作）
    包含实时标记价格和预估数量
    """
    action  = order.get("action", "?")
    symbol  = order.get("symbol", "?")
    usdt    = order.get("usdt", 0)
    leverage = order.get("leverage", 10)
    margin  = order.get("margin_mode", "cross")
    price_type = order.get("price_type", "market")
    limit_price = order.get("price")
    tp = order.get("tp_price")
    sl = order.get("sl_price")

    # 方向文字
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
    direction = direction_map.get(action, action)
    margin_text = "全仓" if margin == "cross" else "逐仓"

    lines = [f"📋 <b>开单预览</b>"]
    lines.append(f"")
    lines.append(f"币对：<code>{symbol}</code>")
    lines.append(f"方向：{direction}")
    lines.append(f"杠杆：{leverage}x  |  {margin_text}")
    lines.append(f"金额：{usdt} USDT")

    # 实时标记价格 + 预估数量
    if action in ("open_long", "open_short", "add") and symbol and usdt:
        try:
            if price_type == "limit" and limit_price:
                ref_price = float(limit_price)
                lines.append(f"挂单价：{ref_price}")
            elif price_type == "liq":
                lines.append(f"价格：对手强平价")
                ref_price = None
            else:
                ref_price = get_mark_price(symbol)
                lines.append(f"标记价：{ref_price}")

            if ref_price:
                raw_qty = (usdt * leverage) / ref_price
                qty = fix_qty(symbol, raw_qty)
                lines.append(f"预估数量：{qty} {symbol.replace('USDT','')}")

                # 简单预估强平价（全仓逐仓公式近似）
                mmr = 0.005
                if action == "open_long":
                    liq_est = ref_price * (1 - 1/leverage + mmr)
                    lines.append(f"预估强平：≈ {round(liq_est, 4)}")
                elif action == "open_short":
                    liq_est = ref_price * (1 + 1/leverage - mmr)
                    lines.append(f"预估强平：≈ {round(liq_est, 4)}")
        except Exception as e:
            lines.append(f"⚠️ 行情获取失败: {e}")

    if limit_price and price_type == "limit":
        pass  # 已在上面处理
    if tp:
        lines.append(f"止盈：{tp}")
    if sl:
        lines.append(f"止损：{sl}")

    lines.append(f"")
    lines.append(f"<i>60秒内未确认自动取消</i>")

    return "\n".join(lines)


def register_pending(order: dict) -> str:
    """注册待确认订单，返回 uid"""
    uid = str(uuid.uuid4())[:8]
    _pending[uid] = order
    return uid


def pop_pending(uid: str) -> dict | None:
    """取出并删除待确认订单"""
    return _pending.pop(uid, None)


def parse_for_preview(text: str):
    """
    解析交易指令，返回 (preview_text, uid) 或 None
    """
    if not is_trade_command(text):
        return None

    order = parse(text)
    if order is None:
        return None

    # 仅开仓/加仓类才走预览确认流程
    # 止盈/止损/平仓 也走预览，保持一致安全
    preview_text = build_preview(order)
    uid = register_pending(order)
    return preview_text, uid
