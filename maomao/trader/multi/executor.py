"""
executor.py — 多账户执行适配层 v1.0（2026-04-19）

职责：
- 按账户下单（市价开/平、挂止损止盈、撤单、查余额/持仓）
- 每个公开函数第一步 require(role, action, account)
- 精度处理自己做，不依赖封板 exchange.py 的全局 client
- 币安1 的复杂能力（algoOrder/移动止盈/滚仓）仍由封板模块独占，不在这里重做

不做的事：
- algoOrder 条件单、移动止盈、滚仓 → 留在封板 trader/trailing、rolling、exchange 里
- 跨账户划转 → 未来单独做

用法示例：
    from trader.multi.executor import open_market, close_market, get_balance, get_all_balances
    open_market("玄玄", "币安2", "BTCUSDT", side="BUY", margin=50, leverage=10)
    close_market("天天", "币安3", "ETHUSDT", pct=100)
    get_balance("玄玄", "币安1")     # 单账户三项
    get_all_balances("玄玄")         # 全账户聚合
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_DOWN

from trader.multi.registry import (
    get_futures_client, get_spot_client, resolve_name, list_accounts,
)
from trader.multi.permissions import require, check

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# 模块级缓存锁（保护 _filters / _hedge_cache 双重写入）
# ══════════════════════════════════════════
_cache_lock = threading.Lock()


# ══════════════════════════════════════════
# 精度缓存（symbol metadata 全网一致，账户无关）
# ══════════════════════════════════════════
_filters: dict[str, dict] = {}
_filters_negative: dict[str, float] = {}   # symbol → 上次确认不存在的 ts
_FILTERS_NEG_TTL = 60.0   # 秒；防 typo 反复打 exchange_info


def _get_filters(client, symbol: str) -> dict:
    if symbol in _filters:
        return _filters[symbol]
    neg_ts = _filters_negative.get(symbol)
    if neg_ts and time.time() - neg_ts < _FILTERS_NEG_TTL:
        raise ValueError(f"未找到交易对: {symbol}（{int(_FILTERS_NEG_TTL)}秒内已确认不存在）")
    with _cache_lock:
        if symbol in _filters:
            return _filters[symbol]
        neg_ts = _filters_negative.get(symbol)
        if neg_ts and time.time() - neg_ts < _FILTERS_NEG_TTL:
            raise ValueError(f"未找到交易对: {symbol}（{int(_FILTERS_NEG_TTL)}秒内已确认不存在）")
        info = client.exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                d = {}
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        d["stepSize"] = f["stepSize"]
                    elif f["filterType"] == "PRICE_FILTER":
                        d["tickSize"] = f["tickSize"]
                    elif f["filterType"] == "MIN_NOTIONAL":
                        d["minNotional"] = float(f.get("notional", 5))
                _filters[symbol] = d
                _filters_negative.pop(symbol, None)
                return d
        _filters_negative[symbol] = time.time()
    raise ValueError(f"未找到交易对: {symbol}")


def _fix(v: float, step: str) -> float:
    return float(Decimal(str(v)).quantize(Decimal(step), rounding=ROUND_DOWN))


# ══════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════

def _set_leverage(client, symbol: str, lev: int):
    try:
        return client.change_leverage(symbol=symbol, leverage=lev)
    except Exception as e:
        return {"error": str(e)}


def _set_margin_mode(client, symbol: str, mode: str = "ISOLATED"):
    try:
        return client.change_margin_type(symbol=symbol, marginType=mode)
    except Exception as e:
        msg = str(e)
        # "No need to change margin type" 是正常情况
        if "-4046" in msg or "No need to change" in msg:
            return {"ok": "already"}
        return {"error": msg}


def _mark_price(client, symbol: str) -> float:
    return float(client.mark_price(symbol=symbol)["markPrice"])


# ══════════════════════════════════════════
# 持仓模式缓存（hedge mode 自动识别）
# ══════════════════════════════════════════
_hedge_cache: dict[str, bool] = {}


def _is_hedge(client, account: str) -> bool:
    """返回账户是否为双向持仓模式（dualSidePosition=True）。成功才缓存——
    失败时不写 cache 防止首连抖动把 hedge 账户永久判成单向，导致后续下单漏 positionSide。"""
    if account in _hedge_cache:
        return _hedge_cache[account]
    with _cache_lock:
        if account in _hedge_cache:
            return _hedge_cache[account]
        try:
            r = client.get_position_mode()
            hedge = bool(r.get("dualSidePosition", False))
            _hedge_cache[account] = hedge
            return hedge
        except Exception as e:
            logger.warning(f"[_is_hedge] {account} get_position_mode 失败，本次按单向走，不缓存以便下次重试: {e}")
            return False


def clear_hedge_cache(role: str, account: str | None = None):
    """切换持仓模式后清缓存。
    account 指定：按该账户 admin 校验，只清该账户。
    account=None（清空所有）：以全部启用账户为权限闸门，避免空缓存时无校验。"""
    with _cache_lock:
        if account:
            require(role, "admin", account)
            _hedge_cache.pop(resolve_name(account), None)
            return
        for a in list_accounts(enabled_only=True):
            require(role, "admin", a["name"])
        _hedge_cache.clear()


def _pos_side_for_open(side: str) -> str:
    """开仓：BUY→LONG / SELL→SHORT（hedge mode 下必传）"""
    return "LONG" if side.upper() == "BUY" else "SHORT"


def _pos_side_for_close(position_amt: float) -> str:
    """平仓：持有多单→LONG / 持有空单→SHORT"""
    return "LONG" if position_amt > 0 else "SHORT"


# ══════════════════════════════════════════
# 查询
# ══════════════════════════════════════════

def get_balance(role: str, account: str) -> dict:
    """查合约+现货+资金三项（铁律：余额默认三项都查）"""
    return get_full_balance(role, account)


def get_futures_only(role: str, account: str) -> dict:
    """只查合约（guardian/内部调用用）"""
    require(role, "query", account)
    account = resolve_name(account)
    c = get_futures_client(account)
    a = c.account()
    return {
        "account": account,
        "total": float(a.get("totalWalletBalance", 0)),
        "available": float(a.get("availableBalance", 0)),
        "upnl": float(a.get("totalUnrealizedProfit", 0)),
    }


def get_full_balance(role: str, account: str) -> dict:
    """查合约+现货+资金账户（3 路 REST 并行；spot/funding 失败静默跳过，合约是主数据）"""
    require(role, "query", account)
    account = resolve_name(account)
    c = get_futures_client(account)

    out = {"account": account, "spot": {}, "funding": {}}

    # 先同步拿 spot client（首次会建连接，走双检锁；之后命中缓存）
    try:
        s = get_spot_client(account)
    except Exception as e:
        logger.warning(f"[{account}] spot 客户端初始化失败: {e}")
        s = None

    def _fetch_futures():
        return c.account()

    def _fetch_spot():
        return s.account() if s else None

    def _fetch_funding():
        return (s.funding_wallet() or []) if s else []

    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_f = ex.submit(_fetch_futures)
        fut_s = ex.submit(_fetch_spot)
        fut_fw = ex.submit(_fetch_funding)

    # 合约（主，失败也不丢 spot/funding）
    try:
        fa = fut_f.result()
        out["futures"] = {
            "total": float(fa.get("totalWalletBalance", 0)),
            "available": float(fa.get("availableBalance", 0)),
            "upnl": float(fa.get("totalUnrealizedProfit", 0)),
        }
    except Exception as e:
        logger.warning(f"[{account}] futures 查询失败: {e}")
        out["futures"] = {"total": 0, "available": 0, "upnl": 0}
        out["futures_error"] = str(e)

    # 现货（best-effort）
    try:
        sa = fut_s.result()
        if sa:
            out["spot"] = {
                b["asset"]: float(b["free"]) + float(b["locked"])
                for b in sa.get("balances", [])
                if float(b["free"]) + float(b["locked"]) > 0.01
            }
    except Exception as e:
        logger.warning(f"[{account}] spot 查询失败: {e}")
        out["spot_error"] = str(e)

    # 资金（best-effort）
    try:
        fw = fut_fw.result()
        out["funding"] = {
            x["asset"]: float(x["free"]) + float(x["locked"])
            for x in fw
            if float(x["free"]) + float(x["locked"]) > 0.01
        }
    except Exception as e:
        logger.warning(f"[{account}] funding_wallet 查询失败: {e}")
        out["funding_error"] = str(e)

    return out


def get_positions(role: str, account: str, symbol: str | None = None) -> list[dict]:
    """查持仓（仅非零）"""
    require(role, "query", account)
    account = resolve_name(account)
    c = get_futures_client(account)
    raw = c.get_position_risk(symbol=symbol) if symbol else c.get_position_risk()
    return [p for p in raw if float(p.get("positionAmt", 0)) != 0]


def get_open_orders(role: str, account: str, symbol: str | None = None) -> list[dict]:
    require(role, "query", account)
    account = resolve_name(account)
    c = get_futures_client(account)
    return c.get_orders(symbol=symbol) if symbol else c.get_orders()


# ══════════════════════════════════════════
# 下单
# ══════════════════════════════════════════

def open_market(role: str, account: str, symbol: str, side: str,
                margin: float, leverage: int,
                margin_type: str = "ISOLATED") -> dict:
    """
    市价开仓。
    - side: "BUY"(做多) / "SELL"(做空)
    - margin: 保证金 U（不含杠杆）
    - leverage: 杠杆倍数
    - margin_type: "ISOLATED"(逐仓) / "CROSSED"(全仓)
    """
    require(role, "trade", account)
    account = resolve_name(account)
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return {"error": f"非法方向: {side}（应为 BUY/SELL）"}

    c = get_futures_client(account)

    # 5 个独立 REST 并行：margin_mode / leverage / filters / mark_price / position_mode
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_mm = ex.submit(_set_margin_mode, c, symbol, margin_type)
        f_lv = ex.submit(_set_leverage, c, symbol, leverage)
        f_ft = ex.submit(_get_filters, c, symbol)
        f_mp = ex.submit(_mark_price, c, symbol)
        f_hg = ex.submit(_is_hedge, c, account)

    lev_res = f_lv.result()
    if lev_res.get("error"):
        return {"error": f"设置杠杆失败: {lev_res['error']}"}
    mm_res = f_mm.result()
    if mm_res.get("error"):
        # marginType 切换失败：已有持仓 / 同 margin 已设（-4046 在 _set_margin_mode 内转 ok）
        # 其它情况（-4048 持仓中无法改 marginType 等）不中止下单，但记日志，避免保证金模式误判
        logger.warning(f"[{account}] {symbol} marginType→{margin_type} 未切换成功，继续下单: {mm_res['error']}")
    try:
        flt = f_ft.result()
    except Exception as e:
        return {"error": f"获取交易对精度失败: {e}"}
    try:
        price = f_mp.result()
    except Exception as e:
        return {"error": f"获取标记价失败: {e}"}
    hedge = f_hg.result()

    # 计算数量
    notional = margin * leverage
    qty = _fix(notional / price, flt["stepSize"])

    if qty <= 0:
        return {"error": f"数量计算为 0（保证金 {margin}U × {leverage}x / 价 {price} / step {flt['stepSize']}）"}

    min_notional = flt.get("minNotional", 5)
    if qty * price < min_notional:
        return {"error": f"下单金额 {qty*price:.2f}U 低于最小 {min_notional}U"}

    kwargs = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
    if hedge:
        kwargs["positionSide"] = _pos_side_for_open(side)

    try:
        order = c.new_order(**kwargs)
        return {
            "ok": True, "account": account, "symbol": symbol, "side": side,
            "qty": qty, "price": price, "margin": margin, "leverage": leverage,
            "notional": qty * price, "orderId": order.get("orderId"),
            "hedge": hedge,
        }
    except Exception as e:
        return {"error": str(e)}


def close_market(role: str, account: str, symbol: str,
                 pct: float = 100.0) -> dict:
    """市价平仓。pct=100 全平，50 平一半。
    hedge mode 下若同时持多空两侧，逐一平掉。"""
    require(role, "trade", account)
    account = resolve_name(account)
    c = get_futures_client(account)

    positions = [p for p in c.get_position_risk(symbol=symbol)
                 if float(p.get("positionAmt", 0)) != 0]
    if not positions:
        return {"error": f"{symbol} 无持仓"}

    flt = _get_filters(c, symbol)
    hedge = _is_hedge(c, account)
    results = []
    errors = []
    for p in positions:
        amt = float(p["positionAmt"])
        close_qty = _fix(abs(amt) * pct / 100, flt["stepSize"])
        if close_qty <= 0:
            continue
        side = "SELL" if amt > 0 else "BUY"
        kwargs = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": close_qty}
        if hedge:
            kwargs["positionSide"] = _pos_side_for_close(amt)
        else:
            kwargs["reduceOnly"] = True
        try:
            order = c.new_order(**kwargs)
            results.append({
                "direction": "多" if amt > 0 else "空",
                "qty": close_qty,
                "orderId": order.get("orderId"),
            })
        except Exception as e:
            errors.append({"direction": "多" if amt > 0 else "空", "error": str(e)})

    if not results:
        return {"error": f"{symbol} 平仓全部失败: {errors}"}
    return {
        "ok": True, "account": account, "symbol": symbol, "pct": pct,
        "closed": results, "errors": errors or None, "hedge": hedge,
        # 兼容旧返回：多侧时取第一条摘要给老代码
        "qty": results[0]["qty"], "direction": results[0]["direction"],
        "orderId": results[0]["orderId"],
    }


_TRIGGER_KIND = {
    "stop": ("STOP_MARKET", "stopPrice"),
    "tp":   ("TAKE_PROFIT_MARKET", "tpPrice"),
}


def _place_close_trigger(role: str, account: str, symbol: str,
                          trigger_price: float, direction: str,
                          kind: str) -> dict:
    """止损/止盈共用实现：方向解析 + tickSize 对齐 + hedge positionSide。"""
    require(role, "trade", account)
    account = resolve_name(account)
    c = get_futures_client(account)

    d = direction.lower()
    if d in ("多", "long", "buy"):
        close_side, pos_side = "SELL", "LONG"
    elif d in ("空", "short", "sell"):
        close_side, pos_side = "BUY", "SHORT"
    else:
        return {"error": f"非法方向: {direction}"}

    order_type, price_field = _TRIGGER_KIND[kind]
    flt = _get_filters(c, symbol)
    trigger_price = _fix(trigger_price, flt["tickSize"])

    kwargs = {"symbol": symbol, "side": close_side, "type": order_type,
              "stopPrice": trigger_price, "closePosition": True}
    if _is_hedge(c, account):
        kwargs["positionSide"] = pos_side

    try:
        order = c.new_order(**kwargs)
        return {"ok": True, "account": account, "symbol": symbol,
                price_field: trigger_price, "orderId": order.get("orderId")}
    except Exception as e:
        return {"error": str(e)}


def place_stop_loss(role: str, account: str, symbol: str,
                    stop_price: float, direction: str) -> dict:
    """挂止损单（STOP_MARKET, closePosition）。

    direction: 多/long/BUY → 保护多单；空/short/SELL → 保护空单。
    """
    return _place_close_trigger(role, account, symbol, stop_price, direction, "stop")


def place_take_profit(role: str, account: str, symbol: str,
                      tp_price: float, direction: str) -> dict:
    """挂止盈单（TAKE_PROFIT_MARKET, closePosition）。"""
    return _place_close_trigger(role, account, symbol, tp_price, direction, "tp")


def cancel_all(role: str, account: str, symbol: str) -> dict:
    """撤销某币种所有挂单（无挂单时也返回 ok）"""
    require(role, "trade", account)
    account = resolve_name(account)
    c = get_futures_client(account)
    try:
        c.cancel_open_orders(symbol=symbol)
        return {"ok": True, "account": account, "symbol": symbol}
    except Exception as e:
        msg = str(e)
        # -2011 / Unknown order：本来就没挂单，视为成功
        if "-2011" in msg or "Unknown order" in msg or "no open orders" in msg.lower():
            return {"ok": True, "account": account, "symbol": symbol, "no_orders": True}
        return {"error": msg}


def open_limit(role: str, account: str, symbol: str, side: str,
               margin: float, leverage: int, price: float,
               margin_type: str = "ISOLATED") -> dict:
    """
    限价开仓（GTC）。
    - side: BUY(做多)/SELL(做空)
    - margin: 保证金 U；数量 = margin*leverage/price
    - price: 挂单价（按 tickSize 对齐）
    """
    require(role, "trade", account)
    account = resolve_name(account)
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return {"error": f"非法方向: {side}（应为 BUY/SELL）"}

    c = get_futures_client(account)

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_mm = ex.submit(_set_margin_mode, c, symbol, margin_type)
        f_lv = ex.submit(_set_leverage, c, symbol, leverage)
        f_ft = ex.submit(_get_filters, c, symbol)
        f_hg = ex.submit(_is_hedge, c, account)

    lev_res = f_lv.result()
    if lev_res.get("error"):
        return {"error": f"设置杠杆失败: {lev_res['error']}"}
    mm_res = f_mm.result()
    if mm_res.get("error"):
        logger.warning(f"[{account}] {symbol} marginType→{margin_type} 未切换成功，继续下单: {mm_res['error']}")
    try:
        flt = f_ft.result()
    except Exception as e:
        return {"error": f"获取交易对精度失败: {e}"}
    hedge = f_hg.result()

    limit_price = _fix(price, flt["tickSize"])
    if limit_price <= 0:
        return {"error": f"挂单价异常: {price} → {limit_price}"}
    notional = margin * leverage
    qty = _fix(notional / limit_price, flt["stepSize"])
    if qty <= 0:
        return {"error": f"数量计算为 0（保证金 {margin}U × {leverage}x / 价 {limit_price} / step {flt['stepSize']}）"}

    min_notional = flt.get("minNotional", 5)
    if qty * limit_price < min_notional:
        return {"error": f"挂单金额 {qty*limit_price:.2f}U 低于最小 {min_notional}U"}

    kwargs = {"symbol": symbol, "side": side, "type": "LIMIT",
              "quantity": qty, "price": limit_price, "timeInForce": "GTC"}
    if hedge:
        kwargs["positionSide"] = _pos_side_for_open(side)

    try:
        order = c.new_order(**kwargs)
        return {
            "ok": True, "account": account, "symbol": symbol, "side": side,
            "qty": qty, "price": limit_price, "margin": margin, "leverage": leverage,
            "notional": qty * limit_price, "orderId": order.get("orderId"),
            "hedge": hedge, "type": "LIMIT",
        }
    except Exception as e:
        return {"error": str(e)}


def add_to_position(role: str, account: str, symbol: str, margin: float) -> dict:
    """加仓：自动识别现有持仓方向和杠杆，按市价加 margin U。
    hedge 模式且双向都持仓时拒绝（语义不明）。"""
    require(role, "trade", account)
    account = resolve_name(account)
    c = get_futures_client(account)

    positions = [p for p in c.get_position_risk(symbol=symbol)
                 if float(p.get("positionAmt", 0)) != 0]
    if not positions:
        return {"error": f"{symbol} 无持仓，无法加仓"}
    if len(positions) > 1:
        return {"error": f"{symbol} 同时持多空两侧，加仓方向不明，请指定"}

    p = positions[0]
    amt = float(p["positionAmt"])
    side = "BUY" if amt > 0 else "SELL"
    leverage = int(float(p.get("leverage", 10)))
    margin_type = "ISOLATED" if p.get("marginType", "").lower() == "isolated" else "CROSSED"
    return open_market(role, account, symbol, side, margin, leverage, margin_type)


_LIQ_MMR = 0.005
_LIQ_SLIPPAGE = 0.01


def open_liq(role: str, account: str, symbol: str, side: str,
             wallet: float, liq_price: float,
             margin_type: str = "ISOLATED") -> dict:
    """
    强平价反推开仓：给定保证金 wallet 和目标强平价 liq_price，
    自动算 qty 和派生杠杆。MMR=0.005，留 1% 滑点缓冲。
    """
    require(role, "trade", account)
    account = resolve_name(account)
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return {"error": f"非法方向: {side}（应为 BUY/SELL）"}
    if wallet <= 0:
        return {"error": f"保证金必须 > 0: {wallet}"}
    if liq_price <= 0:
        return {"error": f"强平价必须 > 0: {liq_price}"}

    c = get_futures_client(account)

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_ft = ex.submit(_get_filters, c, symbol)
        f_mp = ex.submit(_mark_price, c, symbol)
        f_hg = ex.submit(_is_hedge, c, account)

    try:
        flt = f_ft.result()
    except Exception as e:
        return {"error": f"获取交易对精度失败: {e}"}
    try:
        entry = f_mp.result()
    except Exception as e:
        return {"error": f"获取标记价失败: {e}"}
    hedge = f_hg.result()

    if side == "BUY":
        liq_eff = liq_price * (1 - _LIQ_SLIPPAGE)
        denom = entry - liq_eff * (1 - _LIQ_MMR)
        if denom <= 0:
            return {"error": f"强平价 {liq_price} 须低于当前价 {entry}（做多）"}
    else:
        liq_eff = liq_price * (1 + _LIQ_SLIPPAGE)
        denom = liq_eff * (1 + _LIQ_MMR) - entry
        if denom <= 0:
            return {"error": f"强平价 {liq_price} 须高于当前价 {entry}（做空）"}

    qty = _fix(wallet / denom, flt["stepSize"])
    if qty <= 0:
        return {"error": f"数量计算为 0（保证金 {wallet}U / 距离 {denom:.4f} / step {flt['stepSize']}）"}

    notional = qty * entry
    leverage = max(1, min(125, round(notional / wallet)))

    min_notional = flt.get("minNotional", 5)
    if notional < min_notional:
        return {"error": f"下单金额 {notional:.2f}U 低于最小 {min_notional}U"}

    # 设置 marginType 和派生杠杆（并行）
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_mm = ex.submit(_set_margin_mode, c, symbol, margin_type)
        f_lv = ex.submit(_set_leverage, c, symbol, leverage)
    lev_res = f_lv.result()
    if lev_res.get("error"):
        return {"error": f"设置派生杠杆 {leverage}x 失败: {lev_res['error']}"}
    mm_res = f_mm.result()
    if mm_res.get("error"):
        logger.warning(f"[{account}] {symbol} marginType→{margin_type} 未切换成功，继续下单: {mm_res['error']}")

    kwargs = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
    if hedge:
        kwargs["positionSide"] = _pos_side_for_open(side)

    try:
        order = c.new_order(**kwargs)
        return {
            "ok": True, "account": account, "symbol": symbol, "side": side,
            "qty": qty, "entry": entry, "liq_target": liq_price,
            "wallet": wallet, "leverage": leverage, "auto_leverage": True,
            "notional": notional, "orderId": order.get("orderId"),
            "hedge": hedge,
        }
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════
# 全账户聚合
# ══════════════════════════════════════════

def _fanout(role: str, fn) -> dict:
    """并行对所有有权限账户执行 fn(role, account)。失败写入 {"error": ...}。"""
    names = [a["name"] for a in list_accounts(enabled_only=True)
             if check(role, "query", a["name"])]
    if not names:
        return {}

    def _one(name):
        try:
            return name, fn(role, name)
        except Exception as e:
            return name, {"error": str(e)}

    out = {}
    with ThreadPoolExecutor(max_workers=min(len(names), 4)) as ex:
        for name, result in ex.map(_one, names):
            out[name] = result
    return out


def get_all_balances(role: str) -> dict:
    """聚合查询所有角色有权的账户余额（合约+现货+资金），并行"""
    return _fanout(role, get_full_balance)


def get_all_positions(role: str) -> dict:
    """聚合查询所有角色有权的账户持仓，并行"""
    return _fanout(role, get_positions)


# ══════════════════════════════════════════
# 资金划转（合约/现货/资金 三方任意互转）
# ══════════════════════════════════════════

# 中文/英文别名 → 币安内部钱包代号
_WALLET_ALIASES = {
    "现货": "MAIN",  "spot": "MAIN",  "main": "MAIN",  "现金": "MAIN",
    "合约": "UMFUTURE", "u合约": "UMFUTURE", "u本位": "UMFUTURE",
    "futures": "UMFUTURE", "umfuture": "UMFUTURE", "u": "UMFUTURE",
    "资金": "FUNDING", "funding": "FUNDING", "理财": "FUNDING",
}


def _norm_wallet(name: str) -> str:
    k = (name or "").strip().lower()
    if k in _WALLET_ALIASES:
        return _WALLET_ALIASES[k]
    # 原始值再映射一次（处理中文 "现货" 不走 lower）
    if name in _WALLET_ALIASES:
        return _WALLET_ALIASES[name]
    raise ValueError(f"未知钱包: {name}（支持：现货/合约/资金）")


def transfer(role: str, account: str, amount: float,
             from_wallet: str, to_wallet: str, asset: str = "USDT") -> dict:
    """
    同账户内钱包划转。
    - from_wallet / to_wallet: 现货 / 合约 / 资金（中文或英文别名）
    - asset: 默认 USDT
    - amount: 数量（不是 U）

    币安 API `/sapi/v1/asset/transfer` 支持 MAIN/UMFUTURE/FUNDING 两两直达。
    """
    require(role, "trade", account)
    account = resolve_name(account)
    asset = (asset or "USDT").upper()

    try:
        src = _norm_wallet(from_wallet)
        dst = _norm_wallet(to_wallet)
    except ValueError as e:
        return {"error": str(e)}

    if src == dst:
        return {"error": f"来源和目的相同: {from_wallet}"}

    ttype = f"{src}_{dst}"  # 例：MAIN_UMFUTURE / UMFUTURE_FUNDING
    if amount <= 0:
        return {"error": f"划转金额必须 > 0: {amount}"}

    s = get_spot_client(account)
    try:
        r = s.user_universal_transfer(type=ttype, asset=asset, amount=amount)
        return {
            "ok": True, "account": account, "asset": asset,
            "amount": amount, "from": from_wallet, "to": to_wallet,
            "type": ttype, "tranId": r.get("tranId"),
        }
    except Exception as e:
        return {"error": str(e)}
