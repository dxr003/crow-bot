#!/usr/bin/env python3
"""
bull_sniper buyer.py — 买入执行模块 v3.3
多账户并发执行（config.yaml accounts），双向持仓模式（positionSide=LONG）
流程：遍历启用账户 → 风控检查 → 设杠杆 → 滑点检查 → 市价开多 → 挂止损 → 挂移动止盈 → 返回结果
"""
import hashlib
import hmac
import logging
import math
import os
import time
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from pathlib import Path
from binance.um_futures import UMFutures

load_dotenv(Path("/root/.qixing_env"))

logger = logging.getLogger("bull_buyer")

from notifier import route as _route
from _atomic import atomic_write_json

FAPI_BASE = "https://fapi.binance.com"

_clients = {}       # key: (key_env, secret_env) → UMFutures
_exchange_info_cache = None


def _get_client(key_env: str = "BINANCE2_API_KEY",
                secret_env: str = "BINANCE2_API_SECRET") -> UMFutures:
    cache_key = (key_env, secret_env)
    if cache_key not in _clients:
        key = os.getenv(key_env, "")
        secret = os.getenv(secret_env, "")
        if not key or not secret:
            raise RuntimeError(f"{key_env}/{secret_env} 未配置")
        _clients[cache_key] = UMFutures(key=key, secret=secret)
    return _clients[cache_key]


def _get_exchange_info(key_env: str = "BINANCE2_API_KEY",
                       secret_env: str = "BINANCE2_API_SECRET") -> dict:
    global _exchange_info_cache
    if _exchange_info_cache is None:
        c = _get_client(key_env, secret_env)
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


def _get_balance(key_env: str = "BINANCE2_API_KEY",
                 secret_env: str = "BINANCE2_API_SECRET") -> dict:
    c = _get_client(key_env, secret_env)
    info = c.account()
    return {
        "total":     float(info["totalWalletBalance"]),
        "available": float(info["availableBalance"]),
        "upnl":      float(info["totalUnrealizedProfit"]),
    }


def _get_long_positions(key_env: str = "BINANCE2_API_KEY",
                        secret_env: str = "BINANCE2_API_SECRET") -> list:
    c = _get_client(key_env, secret_env)
    result = []
    for p in c.get_position_risk():
        if float(p["positionAmt"]) > 0:
            result.append(p)
    return result


def _algo_order(params: dict, key_env: str = "BINANCE2_API_KEY",
                secret_env: str = "BINANCE2_API_SECRET") -> dict:
    """币安algoOrder统一封装（止损用）"""
    key = os.getenv(key_env, "")
    secret = os.getenv(secret_env, "")
    ts = int(time.time() * 1000)
    params["timestamp"] = str(ts)
    query = urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    resp = requests.post(
        f"{FAPI_BASE}/fapi/v1/algoOrder",
        headers={"X-MBX-APIKEY": key},
        data=query + "&signature=" + sig,
        timeout=10,
    )
    return {"status_code": resp.status_code, "data": resp.json()}


def _fapi_order(params: dict, key_env: str = "BINANCE2_API_KEY",
                secret_env: str = "BINANCE2_API_SECRET") -> dict:
    """币安合约普通端点 /fapi/v1/order（移动止盈用）"""
    key = os.getenv(key_env, "")
    secret = os.getenv(secret_env, "")
    ts = int(time.time() * 1000)
    params["timestamp"] = str(ts)
    query = urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    resp = requests.post(
        f"{FAPI_BASE}/fapi/v1/order",
        headers={"X-MBX-APIKEY": key},
        data=query + "&signature=" + sig,
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
    atomic_write_json(TRAILING_STATE, state)


def execute(symbol: str, price: float, analyze_result: dict, cfg: dict) -> dict:
    """多账户入口：遍历 cfg["accounts"]，各账户独立执行，返回 {账户名: result}"""
    mode = cfg.get("mode", "off")

    if mode == "off":
        logger.info(f"[buyer] {symbol} mode=off, 纯记录不下单")
        return {"status": "skipped", "reason": "mode=off纯记录阶段", "order_id": None}

    if mode == "alert":
        logger.info(f"[buyer] {symbol} mode=alert, 推送等待人工确认")
        return {"status": "skipped", "reason": "等待人类确认", "order_id": None}

    accounts = cfg.get("accounts", {})
    if not accounts:
        # 兼容无accounts配置：回退到默认币安2
        accounts = {"币安2": {"enabled": True, "api_key_env": "BINANCE2_API_KEY", "secret_env": "BINANCE2_API_SECRET"}}

    results = {}
    for acct_name, acct_cfg in accounts.items():
        if not acct_cfg.get("enabled", False):
            logger.info(f"[buyer] {symbol} [{acct_name}] 未启用，跳过")
            results[acct_name] = {"status": "skipped", "reason": f"{acct_name}未启用", "order_id": None}
            continue

        key_env = acct_cfg.get("api_key_env", "BINANCE2_API_KEY")
        secret_env = acct_cfg.get("secret_env", "BINANCE2_API_SECRET")
        try:
            r = _execute_auto(symbol, price, analyze_result, cfg, acct_name, key_env, secret_env)
            results[acct_name] = r
        except Exception as e:
            logger.error(f"[buyer] {symbol} [{acct_name}] 执行异常: {e}")
            results[acct_name] = {"status": "error", "reason": str(e), "order_id": None}

    # 兼容：如果只有一个账户，同时返回顶层字段供旧代码读取
    if len(results) == 1:
        single = list(results.values())[0]
        single["_accounts"] = results
        return single

    # 多账户：取第一个executed的作为主结果，附上全部
    for r in results.values():
        if r["status"] == "executed":
            r["_accounts"] = results
            return r
    # 全部未执行：返回第一个
    first = list(results.values())[0]
    first["_accounts"] = results
    return first


def _execute_auto(symbol: str, price: float, analyze_result: dict, cfg: dict,
                  acct_name: str = "币安2",
                  key_env: str = "BINANCE2_API_KEY",
                  secret_env: str = "BINANCE2_API_SECRET") -> dict:

    # ── 1. 持仓数检查 ──
    max_positions = cfg.get("max_concurrent_positions", 5)
    long_positions = _get_long_positions(key_env, secret_env)

    if len(long_positions) >= max_positions:
        return {
            "status": "skipped",
            "reason": f"[{acct_name}] 多头持仓已达上限{max_positions}个",
            "order_id": None,
        }

    # ── 2. 重复持仓检查 ──
    for p in long_positions:
        if p["symbol"] == symbol:
            return {
                "status": "skipped",
                "reason": f"[{acct_name}] {symbol}已有多头持仓",
                "order_id": None,
            }

    # ── 3. 余额检查 ──
    balance = _get_balance(key_env, secret_env)
    position_usd = cfg.get("position_usd", 50)
    min_available = cfg.get("min_available_balance", 150)

    if balance["available"] < min_available:
        return {
            "status": "skipped",
            "reason": f"[{acct_name}] 可用余额{balance['available']:.1f}U<最低{min_available}U",
            "order_id": None,
        }

    if balance["available"] < position_usd:
        return {
            "status": "skipped",
            "reason": f"[{acct_name}] 可用余额{balance['available']:.1f}U不足{position_usd}U",
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
    c = _get_client(key_env, secret_env)
    try:
        c.change_margin_type(symbol=symbol, marginType="CROSSED")
    except Exception:
        pass
    try:
        c.change_leverage(symbol=symbol, leverage=leverage)
    except Exception as e:
        logger.warning(f"[buyer] {symbol} [{acct_name}] change_leverage 失败: {e}，放弃本次下单")
        return {
            "status": "skipped",
            "reason": f"[{acct_name}] 设置杠杆失败: {e}",
            "order_id": None,
        }

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
        f"[buyer] [{acct_name}] {symbol} 开多 {qty} @ ~{mark} "
        f"{leverage}x {position_usd}U 订单:{order_id}"
    )

    # ── 9. 读实际成交均价（止损/止盈基准） ──
    time.sleep(1)
    actual_entry = mark
    try:
        pos_list = c.get_position_risk(symbol=symbol)
        for _p in pos_list:
            if float(_p.get("positionAmt", 0)) > 0:
                actual_entry = float(_p["entryPrice"])
                break
        logger.info(f"[buyer] [{acct_name}] {symbol} 实际入场价:{actual_entry} (mark:{mark})")
    except Exception as _e:
        logger.warning(f"[buyer] [{acct_name}] {symbol} 读入场价失败,用mark:{mark}: {_e}")

    # ── 10. 挂止损（保证金亏损50%，基于实际入场价）──
    # 2026-04-25 10:22 老大：震荡市场收紧 50→30，单笔最大亏损 25U→15U
    # 2026-04-26 10:11 老大：32h 实战 SL 30% 反弹率 67-83%（小币太敏感）→ 回退 50%
    #                  下次新单生效，已开仓位的 30% SL 不动（在币安服务器上挂着）
    sl_margin_pct = 50
    sl_price_drop = sl_margin_pct / leverage / 100
    sl_price = _fix_price(symbol, actual_entry * (1 - sl_price_drop))

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
        }, key_env, secret_env)
        if sl_resp["status_code"] == 200:
            sl_order_id = str(sl_resp["data"].get("algoId", "?"))
            logger.info(f"[buyer] [{acct_name}] {symbol} 止损 @ {sl_price} algoId:{sl_order_id}")
        else:
            logger.warning(f"[buyer] [{acct_name}] {symbol} 止损挂单失败: {sl_resp['data']}")
            coin = symbol.replace("USDT", "")
            sl_fail_msg = f"❌ <b>[{acct_name}] {coin} 止损挂单失败</b>\n入场: {actual_entry}\n需人工检查"
            _route("order_fail", sl_fail_msg)
    except Exception as e:
        logger.warning(f"[buyer] [{acct_name}] {symbol} 止损挂单异常: {e}")
        coin = symbol.replace("USDT", "")
        sl_fail_msg = f"❌ <b>[{acct_name}] {coin} 止损挂单异常</b>\n{e}\n需人工检查"
        _route("order_fail", sl_fail_msg)

    # ── 11. 分级移动止盈（trailing_layered，profile=sniper_bull_limit）──
    # 2026-04-25：下掉币安原生 callback + trailing_limit 分支，统一走 trader.trailing_layered
    trailing_order_id = None
    tp_profile = cfg.get("layered_trailing_profile", "sniper_bull_limit")
    try:
        import sys as _sys
        if "/root/maomao" not in _sys.path:
            _sys.path.insert(0, "/root/maomao")
        from trader.trailing_layered import activate as tl_activate, PROFILES as _PROFILES
        r = tl_activate(
            symbol.replace("USDT", ""),
            profile=tp_profile,
            account=acct_name,
            leverage=leverage,
            note=f"bull_sniper@{acct_name}",
        )
        if isinstance(r, str) and r.startswith("❌"):
            raise RuntimeError(r)
        trailing_order_id = f"layered:{tp_profile}"
        logger.info(f"[buyer] [{acct_name}] {symbol} 分级移动止盈已激活 profile={tp_profile}")
    except Exception as e:
        logger.warning(f"[buyer] [{acct_name}] {symbol} 分级移动止盈激活失败: {e}")
        coin = symbol.replace("USDT", "")
        tp_fail_msg = f"❌ <b>[{acct_name}] {coin} 分级移动止盈激活失败</b>\n{e}\n需人工检查"
        _route("order_fail", tp_fail_msg)

    try:
        _p = _PROFILES.get(tp_profile, {})
        tp_desc = (f"layered[{tp_profile}] "
                   f"{_p.get('activate_pct','?')}%激活/"
                   f"{_p.get('retrace_pct','?')}%回撤/"
                   f"减{_p.get('reduce_ratio','?')}%")
    except NameError:
        tp_desc = f"layered[{tp_profile}] (profile 读取失败)"

    return {
        "status": "executed",
        "reason": (
            f"[{acct_name}] 开多 {symbol} {qty} @ {actual_entry} "
            f"{leverage}x {position_usd}U 止损:{sl_price} "
            f"移动止盈:{tp_desc}"
        ),
        "order_id": order_id,
        "account": acct_name,
        "sl_price": sl_price,
        "sl_margin_pct": sl_margin_pct,
        "sl_algo_id": sl_order_id,
        "tp_order_id": trailing_order_id,
    }
