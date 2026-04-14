#!/usr/bin/env python3
"""
bull_sniper buyer.py — 买入执行模块 v3.2
对接币安2账户（BINANCE2_API_KEY），双向持仓模式（positionSide=LONG）
流程：风控检查 → 设杠杆 → 滑点检查 → 市价开多 → 挂止损 → 挂移动止盈 → 返回结果
"""
import hashlib
import hmac
import logging
import math
import os
import time

import requests
from dotenv import load_dotenv
from pathlib import Path
from binance.um_futures import UMFutures

load_dotenv(Path("/root/.qixing_env"))

logger = logging.getLogger("bull_buyer")

FAPI_BASE = "https://fapi.binance.com"

_client = None
_exchange_info_cache = None


def _get_client() -> UMFutures:
    global _client
    if _client is None:
        key = os.getenv("BINANCE2_API_KEY", "")
        secret = os.getenv("BINANCE2_API_SECRET", "")
        if not key or not secret:
            raise RuntimeError("BINANCE2_API_KEY/SECRET 未配置")
        _client = UMFutures(key=key, secret=secret)
    return _client


def _get_exchange_info() -> dict:
    global _exchange_info_cache
    if _exchange_info_cache is None:
        c = _get_client()
        _exchange_info_cache = {}
        for s in c.exchange_info()["symbols"]:
            _exchange_info_cache[s["symbol"]] = s
    return _exchange_info_cache


def _get_precision(symbol: str) -> dict:
    info = _get_exchange_info()
    sym = info.get(symbol, {})
    step_size = "0.01"
    tick_size = "0.01"
    min_notional = 5.0
    for f in sym.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step_size = f["stepSize"]
        elif f["filterType"] == "PRICE_FILTER":
            tick_size = f["tickSize"]
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f.get("notional", 5))
    return {
        "step_size": float(step_size),
        "tick_size": float(tick_size),
        "min_notional": min_notional,
    }


def _fix_qty(symbol: str, qty: float) -> float:
    p = _get_precision(symbol)
    step = p["step_size"]
    if step <= 0:
        return qty
    decimals = max(0, -int(math.log10(step))) if step < 1 else 0
    return round(math.floor(qty / step) * step, decimals)


def _fix_price(symbol: str, price: float) -> float:
    p = _get_precision(symbol)
    tick = p["tick_size"]
    if tick <= 0:
        return price
    decimals = max(0, -int(math.log10(tick))) if tick < 1 else 0
    return round(round(price / tick) * tick, decimals)


def _get_balance() -> dict:
    c = _get_client()
    info = c.account()
    return {
        "total":     float(info["totalWalletBalance"]),
        "available": float(info["availableBalance"]),
        "upnl":      float(info["totalUnrealizedProfit"]),
    }


def _get_long_positions() -> list:
    c = _get_client()
    result = []
    for p in c.get_position_risk():
        if float(p["positionAmt"]) > 0:
            result.append(p)
    return result


def _algo_order(params: dict) -> dict:
    """币安algoOrder统一封装（止损用）"""
    key = os.getenv("BINANCE2_API_KEY", "")
    secret = os.getenv("BINANCE2_API_SECRET", "")
    ts = int(time.time() * 1000)
    params["timestamp"] = str(ts)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    resp = requests.post(
        f"{FAPI_BASE}/fapi/v1/algoOrder",
        params={**params, "signature": sig},
        headers={"X-MBX-APIKEY": key},
        timeout=10,
    )
    return {"status_code": resp.status_code, "data": resp.json()}


def _fapi_order(params: dict) -> dict:
    """币安合约普通端点 /fapi/v1/order（移动止盈用）"""
    key = os.getenv("BINANCE2_API_KEY", "")
    secret = os.getenv("BINANCE2_API_SECRET", "")
    ts = int(time.time() * 1000)
    params["timestamp"] = str(ts)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    resp = requests.post(
        f"{FAPI_BASE}/fapi/v1/order",
        params={**params, "signature": sig},
        headers={"X-MBX-APIKEY": key},
        timeout=10,
    )
    return {"status_code": resp.status_code, "data": resp.json()}


TRAILING_STATE = Path("/root/maomao/trader/skills/bull_sniper/data/trailing_state.json")


def _register_trailing(symbol: str, entry_price: float, qty: float,
                        activation_pct: float, pullback_pct: float):
    """注册仓位到移动止盈追踪状态文件"""
    import json
    state = {}
    if TRAILING_STATE.exists():
        try:
            state = json.loads(TRAILING_STATE.read_text())
        except Exception:
            state = {}

    state[symbol] = {
        "entry_price": entry_price,
        "qty": qty,
        "activation_pct": activation_pct,
        "pullback_pct": pullback_pct,
        "peak_price": entry_price,
        "activated": False,
        "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    TRAILING_STATE.parent.mkdir(parents=True, exist_ok=True)
    TRAILING_STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def execute(symbol: str, price: float, analyze_result: dict, cfg: dict) -> dict:
    mode = cfg.get("mode", "off")

    if mode == "off":
        logger.info(f"[buyer] {symbol} mode=off, 纯记录不下单")
        return {"status": "skipped", "reason": "mode=off纯记录阶段", "order_id": None}

    if mode == "alert":
        logger.info(f"[buyer] {symbol} mode=alert, 推送等待人工确认")
        return {"status": "skipped", "reason": "mode=alert等待人工确认", "order_id": None}

    try:
        return _execute_auto(symbol, price, analyze_result, cfg)
    except Exception as e:
        logger.error(f"[buyer] {symbol} 执行异常: {e}")
        return {"status": "error", "reason": str(e), "order_id": None}


def _execute_auto(symbol: str, price: float, analyze_result: dict, cfg: dict) -> dict:

    # ── 1. 持仓数检查 ──
    max_positions = cfg.get("max_concurrent_positions", 10)
    long_positions = _get_long_positions()

    if len(long_positions) >= max_positions:
        return {
            "status": "skipped",
            "reason": f"多头持仓已达上限{max_positions}个",
            "order_id": None,
        }

    # ── 2. 重复持仓检查 ──
    for p in long_positions:
        if p["symbol"] == symbol:
            return {
                "status": "skipped",
                "reason": f"{symbol}已有多头持仓",
                "order_id": None,
            }

    # ── 3. 余额检查 ──
    balance = _get_balance()
    position_usd = cfg.get("position_usd", 50)
    min_available = cfg.get("min_available_balance", 150)

    if balance["available"] < min_available:
        return {
            "status": "skipped",
            "reason": f"可用余额{balance['available']:.1f}U<最低{min_available}U",
            "order_id": None,
        }

    if balance["available"] < position_usd:
        return {
            "status": "skipped",
            "reason": f"可用余额{balance['available']:.1f}U不足{position_usd}U",
            "order_id": None,
        }

    # ── 4. 安全阀 ──
    max_position = cfg.get("max_position_usd", 50000)
    if position_usd > max_position:
        return {
            "status": "error",
            "reason": f"仓位{position_usd}U超安全阀{max_position}U",
            "order_id": None,
        }

    # ── 5. 设置杠杆和保证金模式 ──
    leverage = cfg.get("default_leverage", 5)
    c = _get_client()
    try:
        c.change_margin_type(symbol=symbol, marginType="CROSSED")
    except Exception:
        pass
    c.change_leverage(symbol=symbol, leverage=leverage)

    # ── 6. 滑点检查（>2%直接放弃） ──
    mark_resp = requests.get(
        f"{FAPI_BASE}/fapi/v1/premiumIndex",
        params={"symbol": symbol}, timeout=5,
    )
    mark = float(mark_resp.json()["markPrice"])
    max_slippage = cfg.get("max_slippage_pct", 2)

    try:
        book = requests.get(
            f"{FAPI_BASE}/fapi/v1/depth",
            params={"symbol": symbol, "limit": 5},
            timeout=5,
        ).json()
        best_ask = float(book["asks"][0][0])
        spread_pct = (best_ask - mark) / mark * 100
    except Exception:
        spread_pct = 0

    if spread_pct > max_slippage:
        return {
            "status": "skipped",
            "reason": f"滑点{spread_pct:.2f}%>{max_slippage}%放弃",
            "order_id": None,
        }

    # ── 7. 计算数量 ──
    raw_qty = (position_usd * leverage) / mark
    qty = _fix_qty(symbol, raw_qty)

    prec = _get_precision(symbol)
    if qty * mark < prec["min_notional"]:
        return {"status": "error", "reason": f"名义值{qty*mark:.1f}<最低{prec['min_notional']}", "order_id": None}

    # ── 8. 市价开多（双向持仓：positionSide=LONG） ──
    try:
        result = c.new_order(
            symbol=symbol,
            side="BUY",
            type="MARKET",
            quantity=qty,
            positionSide="LONG",
        )
        order_id = str(result.get("orderId", "?"))
    except Exception as e:
        return {"status": "error", "reason": f"下单失败: {e}", "order_id": None}

    logger.info(
        f"[buyer] {symbol} 开多 {qty} @ ~{mark} "
        f"{leverage}x {position_usd}U 订单:{order_id}"
    )

    # ── 9. 挂止��（保证金亏损30%） ──
    time.sleep(1)
    sl_margin_pct = 30
    sl_price_drop = sl_margin_pct / leverage / 100
    sl_price = _fix_price(symbol, mark * (1 - sl_price_drop))

    sl_order_id = None
    try:
        sl_resp = _algo_order({
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": "SELL",
            "positionSide": "LONG",
            "type": "STOP_MARKET",
            "triggerPrice": str(sl_price),
            "closePosition": "true",
        })
        if sl_resp["status_code"] == 200:
            sl_order_id = str(sl_resp["data"].get("algoId", "?"))
            logger.info(f"[buyer] {symbol} 止损 @ {sl_price} algoId:{sl_order_id}")
        else:
            logger.warning(f"[buyer] {symbol} 止损挂单失败: {sl_resp['data']}")
    except Exception as e:
        logger.warning(f"[buyer] {symbol} 止损挂单异常: {e}")

    # ── 10. 币安原生移动止盈（/fapi/v1/order + quantity） ──
    trailing_cfg = cfg.get("trailing", {})
    activation_pct = trailing_cfg.get("activation_pct", 50)
    activate_price = _fix_price(symbol, mark * (1 + activation_pct / 100))
    callback_rate = 10  # 币安原生最大回撤10%

    trailing_order_id = None
    try:
        tp_resp = _fapi_order({
            "symbol": symbol,
            "side": "SELL",
            "positionSide": "LONG",
            "type": "TRAILING_STOP_MARKET",
            "activationPrice": str(activate_price),
            "callbackRate": str(callback_rate),
            "quantity": str(qty),
        })
        if tp_resp["status_code"] == 200:
            trailing_order_id = str(tp_resp["data"].get("orderId", "?"))
            logger.info(
                f"[buyer] {symbol} 币安原生移动止盈 "
                f"激活:{activate_price} 回撤:{callback_rate}% orderId:{trailing_order_id}"
            )
        else:
            logger.warning(f"[buyer] {symbol} 移动止盈挂单失败: {tp_resp['data']}")
    except Exception as e:
        logger.warning(f"[buyer] {symbol} 移动止盈挂单异常: {e}")

    # 自建监控（可通过config开关，默认关闭）
    if cfg.get("custom_trailing_enabled", False):
        pullback_pct = trailing_cfg.get("pullback_trigger", 25)
        try:
            _register_trailing(symbol, mark, qty, activation_pct, pullback_pct)
            logger.info(f"[buyer] {symbol} 自建监控已注册（观察模式）")
        except Exception as e:
            logger.warning(f"[buyer] {symbol} 自建监控注册失败: {e}")

    return {
        "status": "executed",
        "reason": (
            f"[币安2] 开多 {symbol} {qty} @ ~{mark} "
            f"{leverage}x {position_usd}U 止损:{sl_price} "
            f"移动止盈:币安原生+{activation_pct}%激活/{callback_rate}%回撤"
        ),
        "order_id": order_id,
        "sl_price": sl_price,
        "sl_algo_id": sl_order_id,
        "tp_order_id": trailing_order_id,
    }
