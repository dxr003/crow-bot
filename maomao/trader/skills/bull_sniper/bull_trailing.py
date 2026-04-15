#!/usr/bin/env python3
"""
bull_trailing.py — 做多阻击仓位生命周期管理（币安2专用）
由 scanner 主循环每轮调用。

职责：
  1. check_all() — 移动止盈观察模式（25%回撤通知，不平仓）
  2. check_positions() — 仓位生命周期管理：
     a. 仓位消失检测 → 标记止盈/止损 → 冷却48h
     b. 超时平仓（24h未爆发）→ 市价平 → 冷却24h
     c. 正常持仓 → 更新峰值浮盈
"""
import hashlib
import hmac
import json
import logging
import os
import time

import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("/root/.qixing_env"))

logger = logging.getLogger("bull_trailing")

FAPI_BASE = "https://fapi.binance.com"
STATE_FILE = Path("/root/maomao/trader/skills/bull_sniper/data/trailing_state.json")

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


def _get_bn2_positions() -> dict:
    """拉币安2所有多头持仓 {symbol: {amt, entryPrice, markPrice}}"""
    key = os.getenv("BINANCE2_API_KEY", "")
    secret = os.getenv("BINANCE2_API_SECRET", "")
    if not key:
        return {}
    from binance.um_futures import UMFutures
    c = UMFutures(key=key, secret=secret)
    result = {}
    for p in c.get_position_risk():
        amt = float(p["positionAmt"])
        if amt > 0:
            result[p["symbol"]] = {
                "amt": amt,
                "entry_price": float(p["entryPrice"]),
                "mark_price": float(p["markPrice"]),
            }
    return result


def _get_mark_price(symbol: str) -> float:
    try:
        resp = requests.get(
            f"{FAPI_BASE}/fapi/v1/premiumIndex",
            params={"symbol": symbol}, timeout=5,
        )
        return float(resp.json()["markPrice"])
    except Exception:
        return 0


def _close_position(symbol: str, qty: float) -> str:
    """市价平多（币安2，双向持仓 SELL+LONG）"""
    key = os.getenv("BINANCE2_API_KEY", "")
    secret = os.getenv("BINANCE2_API_SECRET", "")
    ts = int(time.time() * 1000)
    params = {
        "symbol": symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": str(qty),
        "timestamp": str(ts),
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    resp = requests.post(
        f"{FAPI_BASE}/fapi/v1/order",
        params=f"{qs}&signature={sig}",
        headers={"X-MBX-APIKEY": key},
        timeout=10,
    )
    if resp.status_code == 200:
        oid = resp.json().get("orderId", "?")
        return f"平仓成功 orderId:{oid}"
    return f"平仓失败: {resp.text[:200]}"


def _cancel_algo_by_id(algo_id) -> bool:
    """按 algoId 撤单，成功返回 True"""
    if not algo_id:
        return False
    key = os.getenv("BINANCE2_API_KEY", "")
    secret = os.getenv("BINANCE2_API_SECRET", "")
    ts = int(time.time() * 1000)
    qs = f"algoId={algo_id}&timestamp={ts}"
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    resp = requests.delete(
        f"{FAPI_BASE}/fapi/v1/algoOrder",
        params=f"{qs}&signature={sig}",
        headers={"X-MBX-APIKEY": key},
        timeout=10,
    )
    return resp.status_code == 200


def _cancel_algo_orders(symbol: str):
    """撤销该币所有algo条件单（止损等）"""
    key = os.getenv("BINANCE2_API_KEY", "")
    secret = os.getenv("BINANCE2_API_SECRET", "")
    ts = int(time.time() * 1000)

    qs = f"timestamp={ts}"
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    resp = requests.get(
        "https://api.binance.com/sapi/v1/algo/futures/openOrders",
        params=f"{qs}&signature={sig}",
        headers={"X-MBX-APIKEY": key},
        timeout=10,
    )
    if resp.status_code != 200:
        return

    for o in resp.json().get("orders", []):
        if o.get("symbol") == symbol:
            algo_id = o.get("algoId")
            if not algo_id:
                continue
            ts2 = int(time.time() * 1000)
            qs2 = f"algoId={algo_id}&timestamp={ts2}"
            sig2 = hmac.new(secret.encode(), qs2.encode(), hashlib.sha256).hexdigest()
            requests.delete(
                f"{FAPI_BASE}/fapi/v1/algoOrder",
                params=f"{qs2}&signature={sig2}",
                headers={"X-MBX-APIKEY": key},
                timeout=10,
            )


def check_all() -> list:
    """
    检查所有追踪仓位，满足条件只发通知不平仓（观察模式）。
    实际平仓由币安原生10%回撤负责。由 scanner 主循环每轮调用。
    """
    state = _load()
    if not state:
        return []

    positions = _get_bn2_positions()
    triggered = []
    changed = False

    for symbol, entry in list(state.items()):
        try:
            pos = positions.get(symbol)
            if not pos:
                del state[symbol]
                changed = True
                coin = symbol.replace("USDT", "")
                logger.info(f"[trailing] {coin} 持仓消失，追踪清除")
                _route("position_gone", f"ℹ️ {coin} 持仓已消失，移动止盈追踪自动清除")
                continue

            cur_price = pos["mark_price"] or _get_mark_price(symbol)
            entry_price = entry["entry_price"]
            activation_pct = entry["activation_pct"]
            pullback_pct = entry["pullback_pct"]
            peak = entry["peak_price"]

            float_pnl = (cur_price - entry_price) / entry_price * 100

            # ── 未激活：等浮盈达标 ──
            if not entry["activated"]:
                if float_pnl >= activation_pct:
                    entry["activated"] = True
                    entry["activated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    entry["peak_price"] = cur_price
                    changed = True
                    coin = symbol.replace("USDT", "")
                    act_msg = (
                        f"🟢 <b>{coin} 移动止盈激活！</b>\n"
                        f"浮盈 <b>+{float_pnl:.1f}%</b> 达到 +{activation_pct}%\n"
                        f"峰值: {cur_price:.4f}  回撤{pullback_pct}%触发平仓"
                    )
                    _route("tp_activated", act_msg)
                    logger.info(f"[trailing] {coin} 激活 浮盈+{float_pnl:.1f}%")
                continue

            # ── 已激活：更新峰值 ──
            if cur_price > peak:
                entry["peak_price"] = cur_price
                changed = True
                peak = cur_price

            # ── 计算利润回撤 ──
            peak_pnl = (peak - entry_price) / entry_price * 100
            if peak_pnl > 0:
                profit_drawdown = (peak_pnl - float_pnl) / peak_pnl * 100
            else:
                profit_drawdown = 0

            # ── 回撤达标 → 只通知不平仓（观察模式） ──
            if profit_drawdown >= pullback_pct and not entry.get("notified_25pct"):
                coin = symbol.replace("USDT", "")
                logger.info(
                    f"[trailing] {coin} 25%回撤达标(观察) 回撤{profit_drawdown:.1f}% "
                    f"峰值:{peak:.4f} 现价:{cur_price:.4f}"
                )

                dd_msg = (
                    f"👁 <b>自建25%回撤达标（观察模式）— {coin}</b>\n"
                    f"入场: {entry_price:.4f}  峰值: {peak:.4f}  现价: {cur_price:.4f}\n"
                    f"峰值收益: +{peak_pnl:.1f}%  当前: +{float_pnl:.1f}%\n"
                    f"利润回撤: -{profit_drawdown:.1f}%\n"
                    f"⚠️ 不执行平仓，由币安原生10%回撤负责"
                )
                _route("tp_activated", dd_msg)

                entry["notified_25pct"] = True
                changed = True

                triggered.append({
                    "symbol": symbol,
                    "pnl_pct": round(float_pnl, 1),
                    "drawdown": round(profit_drawdown, 1),
                })

        except Exception as e:
            coin = symbol.replace("USDT", "")
            logger.warning(f"[trailing] {coin} 检查异常: {e}")

    if changed:
        _save(state)

    return triggered


def check_positions(scanner_state: dict, cfg: dict = None) -> bool:
    """
    仓位生命周期管理，由 scanner 主循环每轮调用。
    直接操作 scanner_state["positions"] 和 scanner_state["cooldowns"]。
    读写失败跳过本轮，下轮再来。
    返回 True 表示 state 有变更需要保存。
    """
    positions = scanner_state.get("positions", {})
    if not positions:
        return False

    cfg = cfg or {}
    try:
        bn2_positions = _get_bn2_positions()
    except Exception as e:
        logger.warning(f"[仓位] 拉币安2持仓失败，跳过本轮: {e}")
        return False

    now = time.time()
    changed = False
    leverage = cfg.get("default_leverage", 5)
    tp_cooldown = cfg.get("cooldown_after_tp_hours", 12)
    sl_cooldown = cfg.get("cooldown_after_sl_hours", 24)
    timeout_cooldown = cfg.get("cooldown_after_timeout_hours", 6)
    timeout_hours = cfg.get("position_timeout_hours", 24)

    for symbol, pos_info in list(positions.items()):
        try:
            coin = symbol.replace("USDT", "")
            entry_price = pos_info["entry_price"]
            entry_time = pos_info["entry_time"]
            bn2_pos = bn2_positions.get(symbol)

            # ── 4a. 仓位消失 = 被止盈或止损平掉 ──
            if not bn2_pos:
                # 撤残留 algo 挂单：先按 id 精确撤，失败则兜底全撤
                sl_id = pos_info.get("sl_algo_id")
                tp_id = pos_info.get("tp_algo_id")
                try:
                    ok1 = _cancel_algo_by_id(sl_id)
                    ok2 = _cancel_algo_by_id(tp_id)
                    if not (ok1 and ok2):
                        _cancel_algo_orders(symbol)
                except Exception:
                    try:
                        _cancel_algo_orders(symbol)
                    except Exception:
                        pass
                logger.info(f"[仓位] {coin} 消失，已撤残留挂单 sl={sl_id} tp={tp_id}")

                mark = _get_mark_price(symbol) or entry_price
                pnl_pct = (mark - entry_price) / entry_price * 100
                margin_pnl = pnl_pct * leverage

                if margin_pnl >= 0:
                    label, emoji = "✅ 成功", "✅"
                    cooldown_hours = tp_cooldown
                    cooldown_type = "tp"
                    event = "tp_closed"
                else:
                    label, emoji = "❌ 失败", "❌"
                    cooldown_hours = sl_cooldown
                    cooldown_type = "sl"
                    event = "sl_closed"

                scanner_state.setdefault("cooldowns", {})[symbol] = {
                    "expire_at": now + cooldown_hours * 3600,
                    "type": cooldown_type,
                    "last_entry_price": entry_price,
                }

                close_msg = (
                    f"{emoji} <b>仓位结束 — {coin}</b>\n"
                    f"结果: {label}\n"
                    f"入场: {entry_price:.4f}  估算盈亏: {margin_pnl:+.1f}%\n"
                    f"冷却: {cooldown_hours}小时"
                )
                _route(event, close_msg)
                logger.info(f"[仓位] {coin} {label} 保证金盈亏{margin_pnl:+.1f}% 冷却{cooldown_hours}h")

                del positions[symbol]
                changed = True
                continue

            # 当前价和浮盈
            cur_price = bn2_pos["mark_price"]
            pnl_pct = (cur_price - entry_price) / entry_price * 100
            margin_pnl = pnl_pct * leverage

            # ── 4b. 超时平仓 ──
            hours_held = (now - entry_time) / 3600
            if hours_held >= timeout_hours:
                logger.info(f"[仓位] {coin} 持仓{hours_held:.1f}h超时，执行平仓")

                try:
                    _cancel_algo_orders(symbol)
                except Exception:
                    pass
                close_result = _close_position(symbol, bn2_pos["amt"])

                scanner_state.setdefault("cooldowns", {})[symbol] = {
                    "expire_at": now + timeout_cooldown * 3600,
                    "type": "timeout",
                    "last_entry_price": entry_price,
                }

                timeout_msg = (
                    f"⏰ <b>因故平仓 — {coin}</b>\n"
                    f"持仓 {hours_held:.1f} 小时未爆发\n"
                    f"入场: {entry_price:.4f}  现价: {cur_price:.4f}\n"
                    f"保证金盈亏: {margin_pnl:+.1f}%\n"
                    f"{close_result}\n"
                    f"冷却: {timeout_cooldown}小时"
                )
                _route("forced_close", timeout_msg)
                logger.info(f"[仓位] {coin} {label} 保证金盈亏{margin_pnl:+.1f}% {close_result}")

                del positions[symbol]
                changed = True
                continue

            # ── 4c. 正常持仓：更新峰值 ──
            if margin_pnl > pos_info.get("peak_pnl_pct", 0):
                pos_info["peak_pnl_pct"] = round(margin_pnl, 1)
                changed = True

        except Exception as e:
            coin = symbol.replace("USDT", "")
            logger.warning(f"[仓位] {coin} 检查异常: {e}")

    return changed
