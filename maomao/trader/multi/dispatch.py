"""
dispatch.py — 对话指令派发器（替代老 trader.router 的对话入口）

定位：
- 玄玄/天天/大猫的 TG 消息进来后，先过本派发器
- 命中 → 解析 + 路由到 trader.multi.executor → 返回回执文本
- 不命中 → 返回 None，由调用方走 AI 对话

设计原则：
- 复用 parser.parse() 解析 action / 数量 / 价格 / 杠杆 / 止盈止损
- **新增**：识别消息中的"账户前缀"（币安1/2/3/4 / main / test / lhb / zgl / 玄玄 / 李红兵 / 专攻组六）
- 没指定账户时按 role 默认（玄玄→币安1 / 天天→币安2 / 大猫→币安1）
- 默认 margin_type=CROSSED（与"全仓"约定一致），与文档对齐
- 全部走 executor，权限层物理兜底，越权抛 PermissionError
- 每次执行自动写 exec_log（executor 装饰器内置）

返回：
- (reply_text, "ok"|"err"|"none") 三元结果
  - "ok"  → 已执行
  - "err" → 已识别为指令但执行失败
  - 不命中（None） → 让调用方走 AI

不做的事：
- 不做"预览-确认"流程（老板原话：平仓不需要交流，开仓直接执行靠 reply 反馈）
- 不动老 trader.router/order/exchange（保留为内部历史兼容）
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Optional

if "/root" not in sys.path:
    sys.path.insert(0, "/root")
from ledger import get_ledger, new_trace_id, set_trace_id, current_trace_id, log_call as trace_call

from trader.parser import parse, is_trade_command
from trader.multi import executor
from trader.multi.permissions import check

logger = logging.getLogger(__name__)

# L0 对话事件账本：dialog/commands.jsonl
_dialog_ledger = get_ledger("dialog", "commands")


# ──────────────────────────────────────────
# 账户识别（消息前缀里的账户名/别名）
# ──────────────────────────────────────────

# 与 accounts.yaml 的 alias 对齐 + 一些自然语言常用词
_ACCOUNT_TOKENS = {
    # 币安1（玄玄主号）
    "币安1": "币安1", "main": "币安1", "玄玄": "币安1", "1号": "币安1", "一号": "币安1",
    # 币安2（震天响 / 测试场）
    "币安2": "币安2", "test": "币安2", "震天响": "币安2", "zts": "币安2",
    "2号": "币安2", "二号": "币安2",
    # 币安3（李红兵）
    "币安3": "币安3", "lhb": "币安3", "李红兵": "币安3", "3号": "币安3", "三号": "币安3",
    # 币安4（专攻组六）
    "币安4": "币安4", "zgl": "币安4", "专攻组六": "币安4", "组六": "币安4",
    "4号": "币安4", "四号": "币安4",
}

# role → 默认账户（无前缀时落点）
_ROLE_DEFAULT_ACCOUNT = {
    "玄玄": "币安1",
    "大猫": "币安1",
    "天天": "币安2",
}


# 中文/数字账户名：命中即剥，不需边界（不会和币种/自然语言冲突）
_ACCOUNT_TOKENS_STRICT = {
    "币安1", "币安2", "币安3", "币安4",
    "1号", "2号", "3号", "4号",
    "一号", "二号", "三号", "四号",
    "玄玄", "震天响", "李红兵", "专攻组六", "组六",
}

_BOUNDARY_CHARS = " \t\u3000，,.:："


def _extract_account(text: str) -> tuple[Optional[str], str]:
    """从消息里识别账户前缀，返回 (账户名|None, 剥离前缀后的文本)。

    匹配规则：从整段文字开头做"最长前缀"匹配，命中后剥离该前缀+紧邻空白/标点。
    不依赖空白分词（避免 parser 的中英拆分把"币安2"切成"币安 2"）。
    - 中文/数字账户名（币安1-4 / 1号~4号 / 玄玄 / 震天响 等）命中即剥
    - 英文别名（main/test/lhb/zgl/zts）后面必须跟空白/标点/结尾，否则跳过
      （防"lhbcoin""mainnet"这类币种名被误当作账户前缀）
    """
    raw = text.strip()
    if not raw:
        return None, raw
    low = raw.lower()
    # 按 key 长度倒序，先匹配最长（避免"币安1号"被"币安1"截断）
    for key in sorted(_ACCOUNT_TOKENS.keys(), key=len, reverse=True):
        kl = key.lower()
        if not low.startswith(kl):
            continue
        tail = raw[len(key):]
        if key not in _ACCOUNT_TOKENS_STRICT:
            # 英文别名走严格 boundary（避免 lhbcoin 等）
            if tail != "" and tail[0] not in _BOUNDARY_CHARS:
                continue
        return _ACCOUNT_TOKENS[key], tail.lstrip(_BOUNDARY_CHARS)
    return None, raw


def _resolve_account(role: str, text: str) -> tuple[str, str, Optional[str]]:
    """决定本次指令落到哪个账户。

    返回 (account, stripped_text, source)
      source: "explicit"=消息里写了 / "default"=按角色默认
    """
    acc, stripped = _extract_account(text)
    if acc:
        return acc, stripped, "explicit"
    return _ROLE_DEFAULT_ACCOUNT.get(role, "币安1"), text, "default"


# ──────────────────────────────────────────
# margin_mode 转换
# ──────────────────────────────────────────

def _norm_margin(parser_value: str | None) -> str:
    """parser 输出 cross/isolated → executor 用 CROSSED/ISOLATED。
    无值默认 CROSSED（全仓，与文档对齐）。"""
    if parser_value == "isolated":
        return "ISOLATED"
    return "CROSSED"


# ──────────────────────────────────────────
# 文本回执格式
# ──────────────────────────────────────────

def _fmt_open(action: str, account: str, symbol: str,
              margin: float, leverage: int, margin_type: str,
              result: dict, source: str) -> str:
    if not result.get("ok"):
        return f"❌ [{account}] {action} {symbol} 失败: {result.get('error', '未知错误')}"
    qty = result.get("qty")
    price = result.get("price")
    notional = result.get("notional")
    mode_text = "全仓" if margin_type == "CROSSED" else "逐仓"
    src_text = " (默认账户)" if source == "default" else ""
    lines = [
        f"✅ [{account}] {action}{src_text}",
        f"   {symbol} {leverage}x | {mode_text}",
        f"   保证金 {margin}U → 数量 {qty}",
    ]
    if price:
        lines.append(f"   入场价 ~{price}")
    if notional:
        lines.append(f"   名义价值 {notional:.2f}U")
    if result.get("orderId"):
        lines.append(f"   订单号 {result['orderId']}")
    return "\n".join(lines)


def _fmt_close(account: str, symbol: str, result: dict, source: str) -> str:
    src_text = " (默认账户)" if source == "default" else ""
    if result.get("no_position"):
        return f"⚠️ [{account}] {symbol} 当前无持仓，未发起平仓动作{src_text}"
    if not result.get("ok"):
        return f"❌ [{account}] 平 {symbol} 失败: {result.get('error', '未知错误')}"
    closed = result.get("closed") or []
    if not closed:
        return f"❌ [{account}] 平 {symbol} 未返回成交"
    lines = [f"✅ [{account}] 已平 {symbol}{src_text}"]
    for c in closed:
        lines.append(f"   {c.get('direction','?')}单 数量 {c.get('qty','?')} 订单 {c.get('orderId','?')}")
    return "\n".join(lines)


def _fmt_simple(account: str, action: str, symbol: str, result: dict, source: str) -> str:
    src_text = " (默认账户)" if source == "default" else ""
    if not result.get("ok"):
        return f"❌ [{account}] {action} {symbol} 失败: {result.get('error', '未知错误')}"
    return f"✅ [{account}] {action} {symbol}{src_text} 订单 {result.get('orderId', '?')}"


# ──────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────

@trace_call
def try_dispatch(role: str, text: str) -> tuple[str | None, str]:
    """尝试解析+派发对话指令。

    返回 (reply, status)
      reply=None / status="none"  → 不是交易指令，调用方走 AI
      reply=str  / status="ok"    → 已执行，把 reply 推给用户
      reply=str  / status="err"   → 已识别但失败/被权限拦截，把 reply 推给用户
    """
    if not text or not text.strip():
        return None, "none"

    # 每次对话生成 trace_id，贯穿到 executor（@log_call 会从 ContextVar 继承）
    tid = new_trace_id()
    set_trace_id(tid)

    # 第一关：是否像交易指令（用 parser 既有判断，避免误吞日常对话）
    # 但 is_trade_command 看前 3 个 token；如果首 token 是账户前缀，
    # 要拨开账户前缀再判断
    _, stripped_for_check = _extract_account(text)
    if not is_trade_command(stripped_for_check or text):
        _dialog_ledger.event("dispatch_miss", {
            "role": role,
            "text": text[:200],
            "reason": "not_trade_command",
        }, trace_id=tid)
        return None, "none"

    # 第二关：决定账户
    account, stripped, src = _resolve_account(role, text)

    # 第三关：解析订单
    order = parse(stripped)
    if order is None:
        _dialog_ledger.event("dispatch_miss", {
            "role": role, "account": account,
            "text": text[:200], "reason": "parse_failed",
        }, trace_id=tid)
        return None, "none"

    action = order.get("action")
    symbol = order.get("symbol")
    if not action:
        _dialog_ledger.event("dispatch_miss", {
            "role": role, "account": account,
            "text": text[:200], "reason": "no_action",
        }, trace_id=tid)
        return None, "none"

    # 第四关：权限快速拦截，给清晰回执
    needed_action = "trade"
    if not check(role, needed_action, account):
        _dialog_ledger.event("dispatch_denied", {
            "role": role, "account": account, "action": action, "symbol": symbol,
            "reason": "permission_deny",
        }, trace_id=tid, level="WARNING")
        return f"⛔ [{account}] {role} 无权 {needed_action}（权限层拦截）", "err"

    _dialog_ledger.event("dispatch_hit", {
        "role": role, "account": account, "action": action, "symbol": symbol,
        "src": src, "text": text[:200],
    }, trace_id=tid)

    # ─── 路由到 executor ───
    try:
        if action in ("open_long", "open_short"):
            return _do_open(role, account, src, order)
        if action in ("close", "close_long", "close_short"):
            return _do_close(role, account, src, order)
        if action == "tp":
            return _do_tp(role, account, src, order)
        if action == "sl":
            return _do_sl(role, account, src, order)
        if action == "cancel_orders":
            return _do_cancel(role, account, src, order)
        if action == "add":
            return _do_add(role, account, src, order)
        # 不支持的动作（roll/trailing 等）→ 不处理，让 AI 接管走专用模块
        return None, "none"
    except PermissionError as e:
        _dialog_ledger.event("dispatch_error", {
            "role": role, "account": account, "action": action, "symbol": symbol,
            "error": str(e), "exception_type": "PermissionError",
        }, trace_id=tid, level="ERROR")
        return f"⛔ [{account}] 权限拒绝: {e}", "err"
    except Exception as e:
        logger.exception(f"[dispatch] {role}@{account} {action} {symbol} 异常")
        _dialog_ledger.event("dispatch_error", {
            "role": role, "account": account, "action": action, "symbol": symbol,
            "error": str(e), "exception_type": type(e).__name__,
        }, trace_id=tid, level="ERROR")
        return f"❌ [{account}] {action} {symbol} 异常: {e}", "err"


# ──────────────────────────────────────────
# 各 action 执行体
# ──────────────────────────────────────────

@trace_call
def _do_open(role: str, account: str, src: str, order: dict) -> tuple[str, str]:
    symbol = order["symbol"]
    side = "BUY" if order["action"] == "open_long" else "SELL"
    side_text = "做多" if side == "BUY" else "做空"
    margin = order.get("usdt")
    leverage = order.get("leverage") or 10
    margin_type = _norm_margin(order.get("margin_mode"))
    price_type = order.get("price_type", "market")
    tp = order.get("tp_price")
    sl = order.get("sl_price")

    if not margin:
        return f"❌ [{account}] 缺少保证金（如：做多 SOL 5x 20U）", "err"

    # 强平价反推（缺 target 直接报错，不许降级市价）
    if price_type == "liq":
        if not order.get("liq_target"):
            return f"❌ [{account}] 强平价路径缺少目标价（如：做多 SOL 强平 68 20U）", "err"
        result = executor.open_liq(role, account, symbol, side,
                                   wallet=margin, liq_price=order["liq_target"],
                                   margin_type=margin_type)
        reply = _fmt_open(f"强平价{side_text}", account, symbol,
                          margin, result.get("leverage", 0), margin_type, result, src)
    elif price_type == "limit":
        if not order.get("price"):
            return f"❌ [{account}] 限价路径缺少挂单价（如：做多 SOL 5x 20U 限价 150）", "err"
        result = executor.open_limit(role, account, symbol, side,
                                     margin=margin, leverage=leverage,
                                     price=order["price"], margin_type=margin_type)
        reply = _fmt_open(f"限价{side_text}", account, symbol,
                          margin, leverage, margin_type, result, src)
    else:
        result = executor.open_market(role, account, symbol, side,
                                      margin=margin, leverage=leverage,
                                      margin_type=margin_type)
        reply = _fmt_open(f"市价{side_text}", account, symbol,
                          margin, leverage, margin_type, result, src)

    if not result.get("ok"):
        return reply, "err"

    # 附加止盈止损：任何异常都吞进 extras，绝不能丢失主开仓成功回执（否则爸爸会手动补开造成双开）
    extras = []
    direction = "多" if side == "BUY" else "空"
    if sl:
        try:
            sl_r = executor.place_stop_loss(role, account, symbol,
                                            stop_price=sl, direction=direction)
            extras.append(f"  止损 {sl}: {'✅' if sl_r.get('ok') else '❌ ' + str(sl_r.get('error', ''))}")
        except Exception as e:
            logger.exception(f"[dispatch] {account} {symbol} 挂止损异常")
            extras.append(f"  止损 {sl}: ❌ 挂单异常 {type(e).__name__}: {e}")
    if tp:
        try:
            tp_r = executor.place_take_profit(role, account, symbol,
                                              tp_price=tp, direction=direction)
            extras.append(f"  止盈 {tp}: {'✅' if tp_r.get('ok') else '❌ ' + str(tp_r.get('error', ''))}")
        except Exception as e:
            logger.exception(f"[dispatch] {account} {symbol} 挂止盈异常")
            extras.append(f"  止盈 {tp}: ❌ 挂单异常 {type(e).__name__}: {e}")
    if extras:
        reply += "\n" + "\n".join(extras)
    return reply, "ok"


@trace_call
def _do_close(role: str, account: str, src: str, order: dict) -> tuple[str, str]:
    symbol = order["symbol"]
    pct = order.get("pct") or 100.0
    direction = None
    if order["action"] == "close_long":
        direction = "多"
    elif order["action"] == "close_short":
        direction = "空"
    result = executor.close_market(role, account, symbol, pct=pct, direction=direction)
    status = "ok" if result.get("ok") else "err"
    return _fmt_close(account, symbol, result, src), status


def _do_tp(role: str, account: str, src: str, order: dict) -> tuple[str, str]:
    symbol = order["symbol"]
    price = order.get("tp_price") or order.get("price")
    if not price:
        return f"❌ [{account}] 缺少止盈价", "err"
    # 自动从持仓判断方向
    direction = _infer_direction(role, account, symbol)
    if not direction:
        return f"⚠️ [{account}] {symbol} 无持仓，无法挂止盈", "err"
    result = executor.place_take_profit(role, account, symbol,
                                        tp_price=price, direction=direction)
    return _fmt_simple(account, f"止盈 @ {price}", symbol, result, src), \
        ("ok" if result.get("ok") else "err")


def _do_sl(role: str, account: str, src: str, order: dict) -> tuple[str, str]:
    symbol = order["symbol"]
    price = order.get("sl_price") or order.get("price")
    if not price:
        return f"❌ [{account}] 缺少止损价", "err"
    direction = _infer_direction(role, account, symbol)
    if not direction:
        return f"⚠️ [{account}] {symbol} 无持仓，无法挂止损", "err"
    result = executor.place_stop_loss(role, account, symbol,
                                      stop_price=price, direction=direction)
    return _fmt_simple(account, f"止损 @ {price}", symbol, result, src), \
        ("ok" if result.get("ok") else "err")


def _do_cancel(role: str, account: str, src: str, order: dict) -> tuple[str, str]:
    symbol = order.get("symbol")
    if not symbol:
        return f"❌ [{account}] 撤单需指定币种（如：撤单 SOL）", "err"
    result = executor.cancel_all(role, account, symbol)
    if result.get("no_orders"):
        return f"⚠️ [{account}] {symbol} 无挂单可撤", "ok"
    return _fmt_simple(account, "撤单", symbol, result, src), \
        ("ok" if result.get("ok") else "err")


def _do_add(role: str, account: str, src: str, order: dict) -> tuple[str, str]:
    symbol = order["symbol"]
    margin = order.get("usdt")
    if not margin:
        return f"❌ [{account}] 加仓需指定金额（如：加 SOL 50U）", "err"
    result = executor.add_to_position(role, account, symbol, margin=margin)
    return _fmt_simple(account, f"加仓 {margin}U", symbol, result, src), \
        ("ok" if result.get("ok") else "err")


def _infer_direction(role: str, account: str, symbol: str) -> str | None:
    """从当前持仓推断方向，用于独立止盈/止损指令。
    多空双持时返回 None（语义不明）。"""
    try:
        positions = executor.get_positions(role, account, symbol=symbol)
    except Exception:
        return None
    if not positions:
        return None
    if len(positions) > 1:
        return None
    amt = float(positions[0].get("positionAmt", 0))
    return "多" if amt > 0 else "空"


# ──────────────────────────────────────────
# 自检
# ──────────────────────────────────────────

if __name__ == "__main__":
    cases = [
        ("玄玄", "做多 SOL 5x 20U 止损 65"),     # 默认币安1，全仓
        ("玄玄", "币安2 做多 SOL 5x 20U"),       # 显式币安2
        ("玄玄", "币安2 zc 做空 BTC 10x 50U"),   # 显式逐仓
        ("玄玄", "平 SOL"),                       # 默认账户全平
        ("玄玄", "币安2 平 SOL"),                 # 显式账户平
        ("玄玄", "币安2 平多 SOL"),               # 仅平多仓
        ("天天", "做多 SOL 5x 20U"),             # 天天默认币安2
        ("天天", "币安1 做多 SOL"),              # 天天对币安1 应权限拒绝
        ("玄玄", "你好"),                          # 不是指令
        ("玄玄", "撤单 SOL"),                     # 撤单
    ]
    print("─" * 60)
    print("dispatch 离线解析自检（dry-run 不真发请求）")
    print("─" * 60)
    for role, text in cases:
        # 只走前 4 步（拆账户/解析），不真调 executor，演示路由结果
        _, stripped_for_check = _extract_account(text)
        if not is_trade_command(stripped_for_check or text):
            print(f"  💬 {role} | {text!r:50s} → 走 AI")
            continue
        account, stripped, src = _resolve_account(role, text)
        order = parse(stripped)
        if order is None or not order.get("action"):
            print(f"  💬 {role} | {text!r:50s} → 走 AI（解析空）")
            continue
        ok = check(role, "trade", account)
        perm = "✅" if ok else "⛔"
        print(f"  {perm} {role} | {text!r:50s} → [{account}/{src}] {order['action']} {order.get('symbol')}"
              f" margin={order.get('usdt')} lev={order.get('leverage')} mode={_norm_margin(order.get('margin_mode'))}")
