"""
交易指令路由器
功能: 判断用户消息是否为交易指令 → 是则直接解析执行(零AI token)

用法:
  from trader.router import try_trade_command
  result = try_trade_command(text)
  if result is not None:
      # 直接回复 result，不走AI
  else:
      # 交给AI对话处理
"""
from trader.parser import parse, is_trade_command
from trader.order import execute
from trader.preview import parse_for_preview

# 这些动作直接执行，不走预览确认流程
_DIRECT_ACTIONS = {"cancel_orders"}


def try_trade_command(text: str):
    """
    尝试解析为交易指令。
    返回 (result_text, None)  → 已直接执行，result_text 是回执
    返回 (preview_text, uid) → 需要预览+等待确认
    返回 None → 不是交易指令，交给AI处理
    """
    if not is_trade_command(text):
        return None
    order = parse(text)
    if order is None:
        return None
    if order.get("action") in _DIRECT_ACTIONS:
        result = execute(order)
        return (result, None)   # uid=None 表示已执行
    return parse_for_preview(text)


def preview_only(text: str) -> str | None:
    """只解析不执行，返回预览文本"""
    if not is_trade_command(text):
        return None

    order = parse(text)
    if order is None:
        return None

    return _format_preview(order)


def _format_preview(order: dict) -> str:
    """格式化指令预览"""
    action = order.get("action", "?")
    symbol = order.get("symbol", "?")
    usdt = order.get("usdt")
    leverage = order.get("leverage")
    margin = order.get("margin_mode", "cross")
    price_type = order.get("price_type", "market")
    price = order.get("price")
    tp = order.get("tp_price")
    sl = order.get("sl_price")

    lines = [f"📋 指令预览: {action} {symbol}"]

    if usdt:
        lines.append(f"   金额: {usdt}U")
    if leverage:
        lines.append(f"   杠杆: {leverage}x")
    lines.append(f"   模式: {'全仓' if margin == 'cross' else '逐仓'}")

    if price_type == "limit" and price:
        lines.append(f"   限价: {price}")
    elif price_type == "liq":
        lines.append(f"   价格: 对手强平价")
    else:
        lines.append(f"   价格: 市价")

    if tp:
        lines.append(f"   止盈: {tp}")
    if sl:
        lines.append(f"   止损: {sl}")

    return "\n".join(lines)


if __name__ == "__main__":
    tests = [
        "做多 SOL 5x 20u 止损 65",
        "开多 BTC 10x 100 限价 85000 止盈 95000 止损 78000",
        "止盈 BTC 90000",
        "平 ETH",
        "做多 btc 5",
        "你好",
        "帮我看看行情",
    ]
    print("=" * 60)
    print("router 路由测试 (preview_only)")
    print("=" * 60)
    for t in tests:
        result = preview_only(t)
        if result:
            print(f"🔀 交易指令: \"{t}\"")
            print(result)
        else:
            print(f"💬 交给AI: \"{t}\"")
        print()
