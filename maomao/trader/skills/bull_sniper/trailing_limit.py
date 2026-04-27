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
from urllib.parse import urlencode
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path("/root/.qixing_env"))

logger = logging.getLogger("bull_sniper.trailing_limit")

FAPI_BASE = "https://fapi.binance.com"
STATE_FILE = Path(__file__).parent / "data" / "trailing_limit_state.json"

from notifier import route as _route
from _atomic import atomic_write_json


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(state: dict):
    atomic_write_json(STATE_FILE, state)


def _bn2_signed(method: str, path: str, params: dict,
                key_env: str = "BINANCE2_API_KEY",
                secret_env: str = "BINANCE2_API_SECRET") -> dict:
    key = os.getenv(key_env, "")
    secret = os.getenv(secret_env, "")
    params["timestamp"] = str(int(time.time() * 1000))
    qs = urlencode(params)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    body = qs + "&signature=" + sig
    url = f"{FAPI_BASE}{path}"
    headers = {"X-MBX-APIKEY": key}
    if method == "POST":
        resp = requests.post(url, data=body, headers=headers, timeout=10)
    elif method == "DELETE":
        resp = requests.delete(url, data=body, headers=headers, timeout=10)
    else:
        resp = requests.get(url, params=body, headers=headers, timeout=10)
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


def _cancel_order(symbol: str, order_id: str,
                  key_env: str = "BINANCE2_API_KEY",
                  secret_env: str = "BINANCE2_API_SECRET") -> bool:
    if not order_id:
        return False
    try:
        r = _bn2_signed("DELETE", "/fapi/v1/algoOrder", {
            "algoId": order_id,
        }, key_env, secret_env)
        return r["status_code"] == 200
    except Exception as e:
        logger.warning(f"[移动止盈] {symbol} 撤单失败 {order_id}: {e}")
        return False


def _place_limit_tp(symbol: str, side: str, position_side: str,
                    qty: str, price: str,
                    key_env: str = "BINANCE2_API_KEY",
                    secret_env: str = "BINANCE2_API_SECRET") -> str:
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
        }, key_env, secret_env)
        if r["status_code"] == 200:
            return str(r["data"].get("algoId", ""))
        logger.warning(f"[移动止盈] {symbol} 挂单失败: {r['data']}")
    except Exception as e:
        logger.warning(f"[移动止盈] {symbol} 挂单异常: {e}")
    return ""


def register(symbol: str, entry_price: float, qty: float,
             side: str = "LONG", leverage: int = 5, cfg: dict = None,
             acct_name: str = "", key_env: str = "", secret_env: str = ""):
    cfg = cfg or {}
    tl_cfg = cfg.get("trailing_limit", {})
    activation_pct = tl_cfg.get("activation_profit_pct", 50)
    pullback_pct = tl_cfg.get("pullback_pct", 40)

    # 多账户：key用 symbol:account 作为state key，单账户兼容用 symbol
    state_key = f"{symbol}:{acct_name}" if acct_name else symbol
    state = _load()
    state[state_key] = {
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
        "account": acct_name,
        "key_env": key_env,
        "secret_env": secret_env,
    }
    _save(state)
    acct_tag = f" [{acct_name}]" if acct_name else ""
    logger.info(
        f"[移动止盈]{acct_tag} {symbol} 已注册 入场:{entry_price} "
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

    # 按账户分组拉持仓（去重，同一账户只拉一次）
    _pos_cache = {}  # key: (key_env, secret_env) → {symbol: {amt, entry_price, mark_price}}
    def _get_positions(ke: str, se: str) -> dict:
        ck = (ke, se)
        if ck in _pos_cache:
            return _pos_cache[ck]
        try:
            k = os.getenv(ke, "")
            if not k:
                _pos_cache[ck] = {}
                return {}
            from binance.um_futures import UMFutures
            c = UMFutures(key=k, secret=os.getenv(se, ""))
            pmap = {}
            for p in c.get_position_risk():
                amt = float(p["positionAmt"])
                if amt != 0:
                    pmap[p["symbol"]] = {
                        "amt": amt,
                        "entry_price": float(p["entryPrice"]),
                        "mark_price": float(p["markPrice"]),
                    }
            _pos_cache[ck] = pmap
            return pmap
        except Exception as e:
            logger.warning(f"[移动止盈] 拉持仓失败 ({ke}): {e}")
            _pos_cache[ck] = {}
            return {}

    for state_key, entry in list(state.items()):
        try:
            # state_key 可能是 "BTCUSDT" 或 "BTCUSDT:币安2"
            symbol = state_key.split(":")[0] if ":" in state_key else state_key
            coin = symbol.replace("USDT", "")
            ke = entry.get("key_env", "BINANCE2_API_KEY")
            se = entry.get("secret_env", "BINANCE2_API_SECRET")
            acct = entry.get("account", "")
            acct_tag = f" [{acct}]" if acct else ""
            positions = _get_positions(ke, se)
            pos = positions.get(symbol)
            is_long = entry["side"] == "LONG"

            # ── 仓位消失：判断止盈还是止损 ──
            if not pos:
                entry_price = entry["entry_price"]
                leverage = entry.get("leverage", 5)
                mark = _get_mark_price(symbol) or entry_price
                tp_price = entry.get("current_tp_price", 0)

                if is_long:
                    pnl_pct = (mark - entry_price) / entry_price * 100 if entry_price > 0 else 0
                else:
                    pnl_pct = (entry_price - mark) / entry_price * 100 if entry_price > 0 else 0
                margin_pnl = pnl_pct * leverage

                if margin_pnl >= 0:
                    emoji = "✅"
                    label = "✅ 止盈成交"
                    event = "tp_closed"
                    exit_desc = f"止盈价: {tp_price:.4f}" if tp_price > 0 else f"现价: {mark:.4f}"
                    close_status = "tp"
                else:
                    emoji = "❌"
                    label = "❌ 触发止损已平仓"
                    event = "sl_closed"
                    exit_desc = f"现价: {mark:.4f}"
                    close_status = "sl"

                close_msg = (
                    f"{emoji} <b>小刃 · 幻影成交报告 · {coin}{acct_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"结果: {label}\n"
                    f"入场: {entry_price:.4f}  {exit_desc}\n"
                    f"盈亏: {pnl_pct:+.1f}%(本金) / {margin_pnl:+.1f}%(含杠杆)\n"
                    f"冷却: 12小时"
                )
                _route(event, close_msg)
                logger.info(f"[移动止盈]{acct_tag} {coin} {label} 盈亏{margin_pnl:+.1f}%")

                results.append({
                    "symbol": symbol,
                    "pnl_pct": round(margin_pnl, 1),
                    "account": acct,
                    "status": close_status,      # tp / sl，由仓位消失时的浮盈判断
                    "exit_price": tp_price if (close_status == "tp" and tp_price > 0) else mark,
                })
                del state[state_key]
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

                    oid = _place_limit_tp(symbol, close_side, pos_side, qty_str, tp_str, ke, se)
                    entry["current_order_id"] = oid
                    entry["current_tp_price"] = float(tp_str)

                    act_msg = (
                        f"🟢 <b>{coin}{acct_tag} 限价移动止盈激活！</b>\n"
                        f"保证金浮盈 <b>+{margin_pnl:.1f}%</b>（币价+{float_pnl:.1f}%）\n"
                        f"首挂止盈: {tp_str}（回撤{pullback_pct}%）\n"
                        f"orderId: {oid or '挂单失败'}"
                    )
                    _route("tp_activated", act_msg)
                    logger.info(
                        f"[移动止盈]{acct_tag} {coin} 激活 保证金+{margin_pnl:.1f}% "
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
                    _cancel_order(symbol, old_oid, ke, se)

                    qty_str = str(entry["qty"])
                    close_side = "SELL" if is_long else "BUY"
                    pos_side = "LONG" if is_long else "SHORT"
                    oid = _place_limit_tp(symbol, close_side, pos_side, qty_str, new_tp_str, ke, se)

                    if not oid:
                        oid = _place_limit_tp(symbol, close_side, pos_side, qty_str, new_tp_str, ke, se)
                        if not oid:
                            _route("order_fail", f"⚠️ {coin}{acct_tag} 移动止盈挂单失败，需人工检查")

                    entry["current_order_id"] = oid
                    entry["current_tp_price"] = new_tp_val
                    changed = True

                    logger.info(
                        f"[移动止盈]{acct_tag} {coin} 更新 "
                        f"峰值:{peak:.4f} 旧止盈:{old_tp:.4f}→新:{new_tp_str} oid={oid}"
                    )

        except Exception as e:
            logger.warning(f"[移动止盈] {state_key} 检查异常: {e}")

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
