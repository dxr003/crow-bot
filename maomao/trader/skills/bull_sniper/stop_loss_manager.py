#!/usr/bin/env python3
"""
stop_loss_manager.py — 基础止损分级 v5.2

规则：
  标准入场 (24h涨幅 ≤ high_entry_threshold_24h):
    30min 内: stop_loss_30min_pct   = 3%
    30min 后: stop_loss_after_pct   = 5%
  高位入场 (24h涨幅 > high_entry_threshold_24h):
    30min 内: stop_loss_high_entry_30min = 2%
    30min 后: stop_loss_high_entry_after = 3%

"30分钟"由 sl_upgrade_window_minutes 控制。
升级只做一次，记录在 position["sl_upgraded"] = True。
"""
import logging
import time

import requests

logger = logging.getLogger("stop_loss_manager")

_FAPI_BASE = "https://fapi.binance.com"


# ─────────────────────────────────────────────
# 纯计算（可单测，无副作用）
# ─────────────────────────────────────────────

def is_high_entry(entry_24h_change_pct: float, cfg: dict) -> bool:
    threshold = cfg.get("high_entry_threshold_24h", 30)
    return entry_24h_change_pct > threshold


def compute_initial_sl_pct(entry_24h_change_pct: float, cfg: dict) -> float:
    """开仓时的初始止损幅度（百分比，正数）"""
    if is_high_entry(entry_24h_change_pct, cfg):
        return float(cfg.get("stop_loss_high_entry_30min", 2))
    return float(cfg.get("stop_loss_30min_pct", 3))


def compute_upgraded_sl_pct(entry_24h_change_pct: float, cfg: dict) -> float:
    """30分钟后的宽松止损幅度（百分比，正数）"""
    if is_high_entry(entry_24h_change_pct, cfg):
        return float(cfg.get("stop_loss_high_entry_after", 3))
    return float(cfg.get("stop_loss_after_pct", 5))


def sl_price_from_pct(open_price: float, sl_pct: float) -> float:
    """open_price * (1 - sl_pct/100)"""
    return round(open_price * (1 - sl_pct / 100), 8)


def is_sl_triggered(current_price: float, open_price: float, sl_pct: float) -> bool:
    """当前价 <= 止损价时返回 True（双保险：algoOrder 失效时 VPS 侧检测）"""
    return current_price <= sl_price_from_pct(open_price, sl_pct)


def should_upgrade(position: dict, cfg: dict) -> bool:
    """
    判断是否应该升级止损：
      - 还没升级过 AND
      - 开仓时长 >= sl_upgrade_window_minutes
    """
    if position.get("sl_upgraded"):
        return False
    window_min = cfg.get("sl_upgrade_window_minutes", 30)
    open_time = position.get("position_open_time", 0)
    elapsed_min = (time.time() - open_time) / 60
    return elapsed_min >= window_min


# ─────────────────────────────────────────────
# API 操作（带 fail-open，不阻塞主流程）
# ─────────────────────────────────────────────

def _cancel_algo_order(symbol: str, algo_id, key: str, secret: str) -> bool:
    """撤销一个 algoOrder"""
    try:
        import hmac, hashlib
        from urllib.parse import urlencode

        ts = int(time.time() * 1000)
        params = {"symbol": symbol, "algoId": str(algo_id), "timestamp": ts}
        sig = hmac.new(secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        resp = requests.delete(
            f"{_FAPI_BASE}/fapi/v1/algoOrder",
            params=params,
            headers={"X-MBX-APIKEY": key},
            timeout=8,
        )
        ok = resp.status_code == 200
        if not ok:
            logger.warning(f"[SL撤单] {symbol} algoId={algo_id} 失败: {resp.text[:120]}")
        return ok
    except Exception as e:
        logger.warning(f"[SL撤单] {symbol} 异常: {e}")
        return False


def _place_algo_stop_market(symbol: str, stop_price: float, qty: float,
                            key: str, secret: str) -> str | None:
    """挂 STOP_MARKET algoOrder，返回 algoId 或 None"""
    try:
        import hmac, hashlib
        from urllib.parse import urlencode

        ts = int(time.time() * 1000)
        params = {
            "symbol": symbol,
            "side": "SELL",
            "positionSide": "LONG",
            "type": "STOP",
            "quantity": str(qty),
            "stopPrice": str(stop_price),
            "timestamp": ts,
        }
        sig = hmac.new(secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        resp = requests.post(
            f"{_FAPI_BASE}/fapi/v1/algoOrder",
            params=params,
            headers={"X-MBX-APIKEY": key},
            timeout=8,
        )
        if resp.status_code == 200:
            algo_id = resp.json().get("algoId")
            logger.info(f"[SL挂单] {symbol} 止损@{stop_price} algoId={algo_id}")
            return algo_id
        logger.warning(f"[SL挂单] {symbol} 失败: {resp.text[:120]}")
        return None
    except Exception as e:
        logger.warning(f"[SL挂单] {symbol} 异常: {e}")
        return None


def _get_position_qty(symbol: str, key: str, secret: str) -> float:
    """从 positionRisk 拿当前多头数量"""
    try:
        import hmac, hashlib
        from urllib.parse import urlencode

        ts = int(time.time() * 1000)
        params = {"symbol": symbol, "timestamp": ts}
        sig = hmac.new(secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        resp = requests.get(
            f"{_FAPI_BASE}/fapi/v2/positionRisk",
            params=params,
            headers={"X-MBX-APIKEY": key},
            timeout=8,
        )
        if resp.status_code == 200:
            for p in resp.json():
                if p["symbol"] == symbol and float(p["positionAmt"]) > 0:
                    return float(p["positionAmt"])
        return 0.0
    except Exception as e:
        logger.warning(f"[SL升级] 查询持仓数量失败 {symbol}: {e}")
        return 0.0


# ─────────────────────────────────────────────
# 升级入口
# ─────────────────────────────────────────────

def upgrade_stop_loss(symbol: str, position: dict, cfg: dict,
                      key: str, secret: str) -> bool:
    """
    对单个持仓执行止损升级：
      1. 撤原止损单
      2. 挂新止损单（宽松档位）
      3. 更新 position 字段（sl_upgraded / sl_algo_id / sl_pct）

    返回 True = 升级成功，False = 跳过或失败
    """
    open_price = position.get("position_open_price") or position.get("entry_price", 0)
    entry_24h = position.get("entry_24h_change_pct", 0)
    old_algo_id = position.get("sl_algo_id")

    new_pct = compute_upgraded_sl_pct(entry_24h, cfg)
    new_stop = sl_price_from_pct(open_price, new_pct)

    logger.info(
        f"[SL升级] {symbol} 开仓价={open_price} 24h={entry_24h:.1f}% "
        f"高位={is_high_entry(entry_24h, cfg)} 旧algoId={old_algo_id} → "
        f"新止损@{new_stop}（-{new_pct}%）"
    )

    # 1. 撤旧单（失败不阻断，继续挂新单）
    if old_algo_id:
        _cancel_algo_order(symbol, old_algo_id, key, secret)

    # 2. 拿当前持仓数量
    qty = _get_position_qty(symbol, key, secret)
    if qty <= 0:
        logger.warning(f"[SL升级] {symbol} 持仓数量=0，跳过挂单")
        position["sl_upgraded"] = True  # 标记避免重试
        return False

    # 3. 挂新止损
    new_algo_id = _place_algo_stop_market(symbol, new_stop, qty, key, secret)

    # 4. 更新 position 状态（无论挂单成功与否都标记 upgraded，避免循环重试）
    position["sl_upgraded"] = True
    position["sl_algo_id"] = new_algo_id or old_algo_id
    position["sl_pct"] = new_pct
    return new_algo_id is not None


def upgrade_all_positions(state: dict, cfg: dict,
                          key: str, secret: str) -> int:
    """
    遍历全部持仓，对满足升级条件的执行升级。
    返回本次升级的数量。
    """
    upgraded = 0
    for symbol, pos in state.get("positions", {}).items():
        if pos.get("status") != "holding":
            continue
        if should_upgrade(pos, cfg):
            ok = upgrade_stop_loss(symbol, pos, cfg, key, secret)
            if ok:
                upgraded += 1
    return upgraded
