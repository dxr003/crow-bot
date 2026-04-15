"""
trailing_limit.py — 限价单移动止盈 v1.0
用限价止盈单模拟移动止盈，突破币安原生10%回撤上限。

默认：浮盈50%激活，回撤40%止盈。一档走天下，config可调。

由 scanner 主循环每轮调用 check_all()。
buyer.py 开仓后调用 register() 注册监控。
"""
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path("/root/.qixing_env"))

logger = logging.getLogger("bull_sniper.trailing_limit")

FAPI_BASE = "https://fapi.binance.com"
STATE_FILE = Path(__file__).parent / "data" / "trailing_limit_state.json"

from notifier import route as _route


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _bn2_signed(method: str, path: str, params: dict) -> dict:
    key = os.getenv("BINANCE2_API_KEY", "")
    secret = os.getenv("BINANCE2_API_SECRET", "")
    params["timestamp"] = str(int(time.time() * 1000))
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{FAPI_BASE}{path}"
    headers = {"X-MBX-APIKEY": key}
    if method == "POST":
        resp = requests.post(url, params=f"{qs}&signature={sig}", headers=headers, timeout=10)
    elif method == "DELETE":
        resp = requests.delete(url, params=f"{qs}&signature={sig}", headers=headers, timeout=10)
    else:
        resp = requests.get(url, params=f"{qs}&signature={sig}", headers=headers, timeout=10)
    return {"status_code": resp.status_code, "data": resp.json()}


def _get_mark_price(symbol: str) -> float:
    try:
        resp = requests.get(
            f"{FAPI_BASE}/fapi/v1/premiumIndex",
            params={"symbol": symbol}, timeout=5,
        )
        return float(resp.json()["markPrice"])
    except Exception:
        return 0


def _get_tick_size(symbol: str) -> float:
    try:
        resp = requests.get(
            f"{FAPI_BASE}/fapi/v1/exchangeInfo", timeout=10,
        )
        for s in resp.json()["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "PRICE_FILTER":
                        return float(f["tickSize"])
    except Exception:
        pass
    return 0.0001


def _fix_price(symbol: str, price: float, tick_size: float = None) -> str:
    ts = tick_size or _get_tick_size(symbol)
    if ts >= 1:
        return str(int(round(price / ts) * ts))
    decimals = len(str(ts).rstrip('0').split('.')[-1])
    fixed = round(round(price / ts) * ts, decimals)
    return f"{fixed:.{decimals}f}"


def _cancel_order(symbol: str, order_id: str) -> bool:
    if not order_id:
        return False
    try:
        r = _bn2_signed("DELETE", "/fapi/v1/algoOrder", {
            "algoId": order_id,
        })
        return r["status_code"] == 200
    except Exception as e:
        logger.warning(f"[移动止盈] {symbol} 撤单失败 {order_id}: {e}")
        return False


def _place_limit_tp(symbol: str, side: str, position_side: str,
                    qty: str, price: str) -> str:
    """通过Algo条件单挂STOP_MARKET，价格到达时触发市价平仓"""
    try:
        r = _bn2_signed("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": "STOP_MARKET",
            "triggerPrice": price,
            "quantity": qty,
        })
        if r["status_code"] == 200:
            return str(r["data"].get("algoId", ""))
        logger.warning(f"[移动止盈] {symbol} 挂单失败: {r['data']}")
    except Exception as e:
        logger.warning(f"[移动止盈] {symbol} 挂单异常: {e}")
    return ""


def register(symbol: str, entry_price: float, qty: float,
             side: str = "LONG", leverage: int = 5, cfg: dict = None):
    cfg = cfg or {}
    tl_cfg = cfg.get("trailing_limit", {})
    activation_pct = tl_cfg.get("activation_profit_pct", 50)
    pullback_pct = tl_cfg.get("pullback_pct", 40)

    state = _load()
    state[symbol] = {
        "entry_price": entry_price,
        "qty": qty,
        "side": side,
        "leverage": leverage,
        "activation_pct": activation_pct,
        "pullback_pct": pullback_pct,
        "peak_price": entry_price,
        "activated": False,
        "current_order_id": "",
        "current_tp_price": 0,
        "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save(state)
    logger.info(
        f"[移动止盈] {symbol} 已注册 入场:{entry_price} "
        f"激活:{activation_pct}% 回撤:{pullback_pct}%"
    )


def check_all(cfg: dict = None) -> list:
    """
    主检查函数，由scanner每轮调用。
    返回已成交的止盈列表。
    """
    state = _load()
    if not state:
        return []

    cfg = cfg or {}
    results = []
    changed = False

    # 拉币安2所有持仓
    try:
        key = os.getenv("BINANCE2_API_KEY", "")
        if not key:
            return []
        from binance.um_futures import UMFutures
        c = UMFutures(key=key, secret=os.getenv("BINANCE2_API_SECRET", ""))
        positions = {}
        for p in c.get_position_risk():
            amt = float(p["positionAmt"])
            if amt != 0:
                positions[p["symbol"]] = {
                    "amt": amt,
                    "entry_price": float(p["entryPrice"]),
                    "mark_price": float(p["markPrice"]),
                }
    except Exception as e:
        logger.warning(f"[移动止盈] 拉持仓失败: {e}")
        return []

    for symbol, entry in list(state.items()):
        try:
            coin = symbol.replace("USDT", "")
            pos = positions.get(symbol)
            is_long = entry["side"] == "LONG"

            # ── 仓位消失：检查是否限价单成交 ──
            if not pos:
                order_id = entry.get("current_order_id", "")
                tp_price = entry.get("current_tp_price", 0)
                entry_price = entry["entry_price"]
                leverage = entry.get("leverage", 5)

                if is_long:
                    pnl_pct = (tp_price - entry_price) / entry_price * 100 if tp_price > 0 else 0
                else:
                    pnl_pct = (entry_price - tp_price) / entry_price * 100 if tp_price > 0 else 0
                margin_pnl = pnl_pct * leverage

                tp_msg = (
                    f"✅ <b>移动止盈成交 — {coin}</b>\n"
                    f"入场: {entry_price:.4f}  止盈价: {tp_price:.4f}\n"
                    f"盈利: +{pnl_pct:.1f}%(本金) / +{margin_pnl:.1f}%(含杠杆)\n"
                    f"触发48h冷却"
                )
                _route("tp_closed", tp_msg)
                logger.info(f"[移动止盈] {coin} 成交 盈亏+{margin_pnl:.1f}%")

                results.append({"symbol": symbol, "pnl_pct": round(margin_pnl, 1)})
                del state[symbol]
                changed = True
                continue

            cur_price = pos["mark_price"]
            entry_price = entry["entry_price"]
            activation_pct = entry["activation_pct"]
            pullback_pct = entry["pullback_pct"]
            peak = entry["peak_price"]

            # ── 计算浮盈 ──
            if is_long:
                float_pnl = (cur_price - entry_price) / entry_price * 100
            else:
                float_pnl = (entry_price - cur_price) / entry_price * 100

            leverage = entry.get("leverage", 5)
            margin_pnl = float_pnl * leverage

            # ── 未激活（按保证金收益判断） ──
            if not entry["activated"]:
                if margin_pnl >= activation_pct:
                    entry["activated"] = True
                    entry["activated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    entry["peak_price"] = cur_price
                    changed = True

                    # 首次挂单
                    tp_price = _calc_tp_price(cur_price, pullback_pct, entry_price, is_long)
                    tick = _get_tick_size(symbol)
                    tp_str = _fix_price(symbol, tp_price, tick)
                    qty_str = str(entry["qty"])
                    close_side = "SELL" if is_long else "BUY"
                    pos_side = "LONG" if is_long else "SHORT"

                    oid = _place_limit_tp(symbol, close_side, pos_side, qty_str, tp_str)
                    entry["current_order_id"] = oid
                    entry["current_tp_price"] = float(tp_str)

                    act_msg = (
                        f"🟢 <b>{coin} 限价移动止盈激活！</b>\n"
                        f"保证金浮盈 <b>+{margin_pnl:.1f}%</b>（币价+{float_pnl:.1f}%）\n"
                        f"首挂止盈: {tp_str}（回撤{pullback_pct}%）\n"
                        f"orderId: {oid or '挂单失败'}"
                    )
                    _route("tp_activated", act_msg)
                    logger.info(
                        f"[移动止盈] {coin} 激活 保证金+{margin_pnl:.1f}% "
                        f"挂单@{tp_str} oid={oid}"
                    )
                continue

            # ── 已激活：检查是否创新高/新低 或 补挂失败单 ──
            new_peak = False
            need_repair = not entry.get("current_order_id")
            if is_long and cur_price > peak:
                new_peak = True
                peak = cur_price
            elif not is_long and cur_price < peak:
                new_peak = True
                peak = cur_price

            if new_peak or need_repair:
                entry["peak_price"] = peak
                changed = True

                new_tp = _calc_tp_price(peak, pullback_pct, entry_price, is_long)
                tick = _get_tick_size(symbol)
                new_tp_str = _fix_price(symbol, new_tp, tick)
                new_tp_val = float(new_tp_str)

                old_tp = entry.get("current_tp_price", 0)
                should_update = need_repair or \
                                (is_long and new_tp_val > old_tp) or \
                                (not is_long and (old_tp == 0 or new_tp_val < old_tp))

                if should_update:
                    old_oid = entry.get("current_order_id", "")
                    _cancel_order(symbol, old_oid)

                    qty_str = str(entry["qty"])
                    close_side = "SELL" if is_long else "BUY"
                    pos_side = "LONG" if is_long else "SHORT"
                    oid = _place_limit_tp(symbol, close_side, pos_side, qty_str, new_tp_str)

                    if not oid:
                        oid = _place_limit_tp(symbol, close_side, pos_side, qty_str, new_tp_str)
                        if not oid:
                            _route("order_fail", f"⚠️ {coin} 移动止盈挂单失败，需人工检查")

                    entry["current_order_id"] = oid
                    entry["current_tp_price"] = new_tp_val
                    changed = True

                    logger.info(
                        f"[移动止盈] {coin} 更新 "
                        f"峰值:{peak:.4f} 旧止盈:{old_tp:.4f}→新:{new_tp_str} oid={oid}"
                    )

        except Exception as e:
            coin = symbol.replace("USDT", "")
            logger.warning(f"[移动止盈] {coin} 检查异常: {e}")

    if changed:
        _save(state)

    return results


def _calc_tp_price(peak: float, pullback_pct: float,
                   entry_price: float, is_long: bool) -> float:
    if is_long:
        peak_profit = (peak - entry_price) / entry_price
        remaining = peak_profit * (1 - pullback_pct / 100)
        tp = entry_price * (1 + max(remaining, 0))
    else:
        peak_profit = (entry_price - peak) / entry_price
        remaining = peak_profit * (1 - pullback_pct / 100)
        tp = entry_price * (1 - max(remaining, 0))
    return tp
