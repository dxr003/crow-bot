#!/usr/bin/env python3
"""
trail_manager.py — 自制两层移动止盈 v5.3

两层规则（参数来自 config.yaml）：
  Layer1: 浮盈峰值 >= trail_layer1_activation_pct (15%)
          利润回撤 >= trail_layer1_pullback_pct (10%) → 触发
  Layer2: 浮盈峰值 >= trail_layer2_activation_pct (35%)
          利润回撤 >= trail_layer2_pullback_pct (15%) → 触发
  （layer2 优先于 layer1）

线程模型：
  trail_loop() 在独立 daemon 线程跑，每 10 秒一次。
  只读 state["positions"] 浅拷贝，写操作全部通过 trail_queue 发回主线程。

队列消息格式：
  {"type": "update_peak", "symbol": "XYZUSDT", "peak_pnl_pct": 45.0}
  {"type": "close", "symbol": "XYZUSDT", "layer": "layer2",
   "cur_pnl_pct": 34.0, "peak_pnl_pct": 40.0, "mark_price": 1.34, "qty": 100.0}
"""
import logging
import time

import requests

logger = logging.getLogger("trail_manager")
_FAPI_BASE = "https://fapi.binance.com"


# ─────────────────────────────────────────────
# 纯计算（无副作用，可单测）
# ─────────────────────────────────────────────

def get_active_layer(peak_pnl_pct: float, cfg: dict) -> str | None:
    """
    根据峰值浮盈判断当前激活的层级。
    返回 "layer2" / "layer1" / None（未激活）。
    layer2 优先：peak >= 35% → layer2，peak >= 15% → layer1。
    """
    l2_act = cfg.get("trail_layer2_activation_pct", 35)
    l1_act = cfg.get("trail_layer1_activation_pct", 15)
    if peak_pnl_pct >= l2_act:
        return "layer2"
    if peak_pnl_pct >= l1_act:
        return "layer1"
    return None


def is_trail_triggered(current_pnl_pct: float, peak_pnl_pct: float,
                       layer: str, cfg: dict) -> bool:
    """
    利润回撤比 = (peak - current) / peak * 100 >= 对应层回撤阈值 → True。
    peak <= 0 时不触发（防除零）。
    """
    if peak_pnl_pct <= 0:
        return False
    l1_pb = cfg.get("trail_layer1_pullback_pct", 10)
    l2_pb = cfg.get("trail_layer2_pullback_pct", 15)
    pullback_threshold = l2_pb if layer == "layer2" else l1_pb
    profit_drawdown = (peak_pnl_pct - current_pnl_pct) / peak_pnl_pct * 100
    return profit_drawdown >= pullback_threshold


def compute_float_pnl(mark_price: float, open_price: float) -> float:
    """浮盈百分比 = (mark - open) / open * 100"""
    if open_price <= 0:
        return 0.0
    return (mark_price - open_price) / open_price * 100


# ─────────────────────────────────────────────
# API 层（带 fail-open，失败返回空，不崩）
# ─────────────────────────────────────────────

def _fetch_positions(key: str, secret: str) -> dict:
    """
    拉 positionRisk，返回 {symbol: {"mark_price": float, "qty": float}}。
    任何异常 → 返回 {}（fail-open）。
    """
    try:
        import hmac, hashlib
        from urllib.parse import urlencode

        ts = int(time.time() * 1000)
        params = {"timestamp": ts}
        sig = hmac.new(secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        resp = requests.get(
            f"{_FAPI_BASE}/fapi/v2/positionRisk",
            params=params,
            headers={"X-MBX-APIKEY": key},
            timeout=8,
        )
        if resp.status_code != 200:
            logger.warning(f"[trail] positionRisk {resp.status_code}: {resp.text[:80]}")
            return {}
        result = {}
        for p in resp.json():
            qty = float(p.get("positionAmt", 0))
            if qty > 0:
                result[p["symbol"]] = {
                    "mark_price": float(p["markPrice"]),
                    "qty": qty,
                }
        return result
    except Exception as e:
        logger.warning(f"[trail] _fetch_positions 异常 fail-open: {e}")
        return {}


def _close_position(symbol: str, qty: float, key: str, secret: str) -> bool:
    """市价平多（SELL LONG），失败返回 False 不崩。"""
    try:
        import hmac, hashlib
        from urllib.parse import urlencode

        ts = int(time.time() * 1000)
        params = {
            "symbol": symbol,
            "side": "SELL",
            "positionSide": "LONG",
            "type": "MARKET",
            "quantity": str(qty),
            "timestamp": ts,
        }
        sig = hmac.new(secret.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        resp = requests.post(
            f"{_FAPI_BASE}/fapi/v1/order",
            params=params,
            headers={"X-MBX-APIKEY": key},
            timeout=8,
        )
        ok = resp.status_code == 200
        if ok:
            logger.info(f"[trail] {symbol} 市价平仓 orderId={resp.json().get('orderId')}")
        else:
            logger.warning(f"[trail] {symbol} 平仓失败: {resp.text[:120]}")
        return ok
    except Exception as e:
        logger.warning(f"[trail] {symbol} 平仓异常: {e}")
        return False


# ─────────────────────────────────────────────
# 检查主函数（线程调用，只 put 不写 state）
# ─────────────────────────────────────────────

def check_all(positions_snapshot: dict, cfg: dict, trail_queue, key: str, secret: str) -> None:
    """
    检查所有持仓快照，触发时往 trail_queue 放消息。
    不直接修改 state，由主线程消费队列后写。
    """
    if not positions_snapshot:
        return

    mark_data = _fetch_positions(key, secret)
    if not mark_data:
        return  # fail-open：API 失败不触发任何动作

    for symbol, pos in positions_snapshot.items():
        if pos.get("status") != "holding":
            continue
        open_price = pos.get("position_open_price") or pos.get("entry_price", 0)
        if open_price <= 0:
            continue

        mdata = mark_data.get(symbol)
        if mdata is None:
            continue  # 持仓已消失，跳过（不崩）

        mark_price = mdata["mark_price"]
        qty = mdata["qty"]
        current_pnl = compute_float_pnl(mark_price, open_price)
        prev_peak = pos.get("peak_pnl_pct", 0.0)
        new_peak = max(prev_peak, current_pnl)

        # 峰值上升 → 通知主线程更新
        if new_peak > prev_peak:
            trail_queue.put({
                "type": "update_peak",
                "symbol": symbol,
                "peak_pnl_pct": new_peak,
            })

        layer = get_active_layer(new_peak, cfg)
        if layer is None:
            continue

        if is_trail_triggered(current_pnl, new_peak, layer, cfg):
            trail_queue.put({
                "type": "close",
                "symbol": symbol,
                "layer": layer,
                "cur_pnl_pct": current_pnl,
                "peak_pnl_pct": new_peak,
                "mark_price": mark_price,
                "qty": qty,
            })
            logger.info(
                f"[trail] {symbol} 触发{layer} "
                f"峰值+{new_peak:.1f}% 现+{current_pnl:.1f}% qty={qty}"
            )


# ─────────────────────────────────────────────
# 线程入口
# ─────────────────────────────────────────────

def trail_loop(state_ref: dict, cfg_getter, trail_queue,
               key: str, secret: str, interval: int = 10) -> None:
    """
    daemon 线程入口，每 interval 秒执行一次 check_all。
    state_ref: scanner.py 的 state dict（每次取浅拷贝，不直接写）
    cfg_getter: lambda 返回最新 CFG（支持热重载）
    """
    while True:
        try:
            positions_snapshot = dict(state_ref.get("positions", {}))
            cfg = cfg_getter()
            check_all(positions_snapshot, cfg, trail_queue, key, secret)
        except Exception as e:
            logger.warning(f"[trail_loop] 异常: {e}")
        time.sleep(interval)
