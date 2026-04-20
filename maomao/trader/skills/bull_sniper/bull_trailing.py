#!/usr/bin/env python3
"""
bull_trailing.py — 做多阻击仓位生命周期管理（多账户）
由 scanner 主循环每轮调用。

职责：
  1. check_all(cfg) — 移动止盈观察模式（25%回撤通知，不平仓）
  2. check_positions(scanner_state, cfg) — 仓位生命周期管理：
     a. 仓位消失检测 → 标记止盈/止损 → 冷却48h
     b. 超时平仓（24h未爆发）→ 市价平 → 冷却24h
     c. 正常持仓 → 更新峰值浮盈

多账户说明（2026-04-19）：
  - 底层 HTTP 函数接 (acct_name, key_env, secret_env) 三元组
  - check_all 读 cfg["accounts"] 聚合各账户持仓（观察，symbol级）
  - check_positions 按 pos_info["accounts"] 列表逐账户动作
  - 缺 accounts 字段视为脏数据，跳过并告警，不再静默回退
"""
import hashlib
import hmac
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path("/root/.qixing_env"))

logger = logging.getLogger("bull_trailing")

FAPI_BASE = "https://fapi.binance.com"
STATE_FILE = Path("/root/maomao/trader/skills/bull_sniper/data/trailing_state.json")

from notifier import route as _route
from _atomic import atomic_write_json


def _load() -> dict:
    """读追踪状态，保证返回 dict（坏文件/非 dict 都自愈为空容器）。"""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception as e:
        logger.warning(f"[trailing] 状态文件读失败，重置: {e}")
        return {}
    if not isinstance(data, dict):
        logger.warning(f"[trailing] 状态文件格式非 dict，重置")
        return {}
    return data


def _save(state: dict):
    atomic_write_json(STATE_FILE, state)


def _iter_accounts(cfg: dict):
    """yield (acct_name, key_env, secret_env) for each enabled account."""
    accounts = (cfg or {}).get("accounts") or {}
    if not accounts:
        logger.warning("[trailing] cfg 缺 accounts 字段，无账户可遍历")
        return
    for name, c in accounts.items():
        if not c.get("enabled"):
            continue
        key_env = c.get("api_key_env", "")
        secret_env = c.get("secret_env", "")
        if not key_env or not secret_env:
            continue
        yield name, key_env, secret_env


def _get_positions(acct_name: str, key_env: str, secret_env: str) -> dict:
    """拉指定账户的多头持仓 {symbol: {amt, entryPrice, markPrice}}"""
    key = os.getenv(key_env, "")
    secret = os.getenv(secret_env, "")
    if not key or not secret:
        logger.warning(f"[trailing] {acct_name} 缺少 {key_env}/{secret_env}")
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


def _fetch_all_positions_parallel(cfg: dict):
    """并行拉所有启用账户的持仓。返回 (acct_positions, acct_creds)。
    acct_positions[acct] = {sym: pos}（成功）或 None（拉取失败，本轮不做决策）。"""
    acct_creds: dict = {}
    triples = []
    for acct, key_env, secret_env in _iter_accounts(cfg):
        acct_creds[acct] = (key_env, secret_env)
        triples.append((acct, key_env, secret_env))
    if not triples:
        return {}, acct_creds

    def _worker(t):
        acct, k, s = t
        try:
            return acct, _get_positions(acct, k, s)
        except Exception as e:
            logger.warning(f"[trailing] {acct} 拉持仓异常: {e}")
            return acct, None

    acct_positions: dict = {}
    with ThreadPoolExecutor(max_workers=min(len(triples), 4)) as ex:
        for acct, pos in ex.map(_worker, triples):
            acct_positions[acct] = pos
    return acct_positions, acct_creds


def _get_mark_price(symbol: str) -> float:
    try:
        resp = requests.get(
            f"{FAPI_BASE}/fapi/v1/premiumIndex",
            params={"symbol": symbol}, timeout=5,
        )
        return float(resp.json()["markPrice"])
    except Exception:
        return 0


def _close_position(acct_name: str, key_env: str, secret_env: str,
                    symbol: str, qty: float) -> str:
    """市价平多（双向持仓 SELL+LONG），走 UMFutures SDK"""
    key = os.getenv(key_env, "")
    secret = os.getenv(secret_env, "")
    if not key or not secret:
        return f"[{acct_name}]缺少密钥"
    from binance.um_futures import UMFutures
    try:
        c = UMFutures(key=key, secret=secret)
        resp = c.new_order(
            symbol=symbol,
            side="SELL",
            positionSide="LONG",
            type="MARKET",
            quantity=str(qty),
        )
        return f"[{acct_name}]平仓成功 orderId:{resp.get('orderId', '?')}"
    except Exception as e:
        return f"[{acct_name}]平仓失败: {str(e)[:200]}"


def _cancel_algo_by_id(acct_name: str, key_env: str, secret_env: str, algo_id) -> bool:
    """按 algoId 撤单，成功返回 True"""
    if not algo_id:
        return False
    key = os.getenv(key_env, "")
    secret = os.getenv(secret_env, "")
    if not key or not secret:
        return False
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


def _cancel_all_algo(accts_creds: dict, symbol: str, sl_id, tp_id):
    """按 id 精确撤 SL/TP。撤不掉只记日志，绝不走全撤——
    避免误撤用户通过玄玄/其他渠道对同一 symbol 挂的 SL/TP（feedback_never_cancel_sl_tp）。
    """
    if not (sl_id or tp_id):
        logger.warning(f"[撤单] {symbol} 无 sl_algo_id/tp_algo_id，跳过撤单（防误撤用户挂单）")
        return
    for acct, (key_env, secret_env) in accts_creds.items():
        for label, aid in (("SL", sl_id), ("TP", tp_id)):
            if not aid:
                continue
            try:
                ok = _cancel_algo_by_id(acct, key_env, secret_env, aid)
                if not ok:
                    logger.warning(f"[撤单] {acct} {symbol} {label} id={aid} 撤失败，残留留给币安过期处理")
            except Exception as e:
                logger.warning(f"[撤单] {acct} {symbol} {label} id={aid} 异常: {e}")


def check_all(cfg: dict = None) -> list:
    """
    检查所有追踪仓位，满足条件只发通知不平仓（观察模式）。
    实际平仓由币安原生10%回撤负责。由 scanner 主循环每轮调用。
    多账户：聚合各账户持仓，任一账户仍有仓即视为活跃（symbol级观察）。
    """
    cfg = cfg or {}
    state = _load()
    if not state:
        return []

    # 聚合各账户持仓（观察模式只看symbol，不区分账户）
    acct_positions, _ = _fetch_all_positions_parallel(cfg)
    combined = {}  # {symbol: pos}
    for acct, pos in acct_positions.items():
        if not pos:
            continue
        for sym, p in pos.items():
            if sym not in combined:
                combined[sym] = p

    triggered = []
    changed = False

    for symbol, entry in list(state.items()):
        try:
            pos = combined.get(symbol)
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


def _settle_signal(scanner_state: dict, symbol: str, status: str, exit_price: float):
    """将活跃信号移入 signal_history（仓位关闭时调用）
    status: tp→success, sl→failed, expired→expired, success→success
    """
    status_map = {"tp": "success", "sl": "failed"}
    mapped = status_map.get(status, status)
    signals = scanner_state.get("signals", [])
    remaining = []
    for sig in signals:
        if sig["symbol"] == symbol:
            sig["status"] = mapped
            sig["exit_price"] = exit_price
            sig["settled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            sig["is_virtual"] = False  # bull_trailing 真实成交结算
            scanner_state.setdefault("signal_history", []).append(sig)
            if len(scanner_state["signal_history"]) > 50:
                scanner_state["signal_history"] = scanner_state["signal_history"][-50:]
            logger.info(f"[真实结算] {symbol} 信号 → {status} 出场:{exit_price}")
        else:
            remaining.append(sig)
    scanner_state["signals"] = remaining


def check_positions(scanner_state: dict, cfg: dict = None) -> bool:
    """
    仓位生命周期管理，由 scanner 主循环每轮调用。
    直接操作 scanner_state["positions"] 和 scanner_state["cooldowns"]。
    多账户：按 pos_info["accounts"] 逐账户检查仓位状态。
    返回 True 表示 state 有变更需要保存。
    """
    positions = scanner_state.get("positions", {})
    if not positions:
        return False

    cfg = cfg or {}

    # 并行预取每个启用账户的持仓 + 凭据映射
    acct_positions, acct_creds = _fetch_all_positions_parallel(cfg)

    if not acct_creds:
        logger.warning("[仓位] 无启用账户，跳过")
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
            accts = pos_info.get("accounts")
            if not accts:
                logger.warning(f"[仓位] {coin} pos_info 缺 accounts 字段，跳过该仓位")
                continue

            # 只检查该仓位登记过的 + 当前启用的账户
            active_accts = [a for a in accts if a in acct_creds]
            if not active_accts:
                logger.warning(f"[仓位] {coin} 登记账户{accts}均未启用，跳过")
                continue

            # 判别每账户的仓位状态
            live_by_acct = {}   # acct -> pos dict（仍有仓）
            unknown = False
            for acct in active_accts:
                p = acct_positions.get(acct)
                if p is None:
                    unknown = True
                    continue
                bn_pos = p.get(symbol)
                if bn_pos:
                    live_by_acct[acct] = bn_pos

            if unknown:
                # 有账户拉失败，本轮不做决策（避免误判平仓）
                continue

            # ── 4a. 所有账户仓位都消失 → 结算 ──
            if not live_by_acct:
                sl_id = pos_info.get("sl_algo_id")
                tp_id = pos_info.get("tp_algo_id")
                creds_subset = {a: acct_creds[a] for a in active_accts}
                _cancel_all_algo(creds_subset, symbol, sl_id, tp_id)
                logger.info(f"[仓位] {coin} 全账户消失，已撤残留挂单 sl={sl_id} tp={tp_id}")

                mark = _get_mark_price(symbol) or entry_price
                pnl_pct = (mark - entry_price) / entry_price * 100
                margin_pnl = pnl_pct * leverage

                if margin_pnl >= 0:
                    label = "✅ 止盈成交"
                    emoji = "✅"
                    cooldown_hours = tp_cooldown
                    cooldown_type = "tp"
                    event = "tp_closed"
                else:
                    label = "❌ 触发止损已平仓"
                    emoji = "❌"
                    cooldown_hours = sl_cooldown
                    cooldown_type = "sl"
                    event = "sl_closed"

                scanner_state.setdefault("cooldowns", {})[symbol] = {
                    "expire_at": now + cooldown_hours * 3600,
                    "type": cooldown_type,
                    "last_entry_price": entry_price,
                }

                close_msg = (
                    f"{emoji} <b>交易阻击成交报告 · {coin}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"结果: {label}\n"
                    f"账户: {','.join(active_accts)}\n"
                    f"入场: {entry_price:.4f}  估算盈亏: {margin_pnl:+.1f}%\n"
                    f"冷却: {cooldown_hours}小时"
                )
                _route(event, close_msg)
                logger.info(f"[仓位] {coin} {label} 账户{active_accts} 保证金盈亏{margin_pnl:+.1f}% 冷却{cooldown_hours}h")

                _settle_signal(scanner_state, symbol, cooldown_type, mark)
                del positions[symbol]
                changed = True
                continue

            # 当前价和浮盈（用首个仍有仓账户的markPrice）
            first_live_acct = next(iter(live_by_acct))
            first_live_pos = live_by_acct[first_live_acct]
            cur_price = first_live_pos["mark_price"]
            pnl_pct = (cur_price - entry_price) / entry_price * 100
            margin_pnl = pnl_pct * leverage

            # ── 4b. 超时平仓 ──
            hours_held = (now - entry_time) / 3600
            if hours_held >= timeout_hours:
                logger.info(f"[仓位] {coin} 持仓{hours_held:.1f}h超时，执行平仓 账户{list(live_by_acct.keys())}")

                # 先按 id 精确撤 SL/TP，再平仓（绝不全撤，防误伤用户同币挂单）
                sl_id = pos_info.get("sl_algo_id")
                tp_id = pos_info.get("tp_algo_id")
                creds_subset = {a: acct_creds[a] for a in live_by_acct.keys()}
                _cancel_all_algo(creds_subset, symbol, sl_id, tp_id)

                close_results = []
                for acct, bn_pos in live_by_acct.items():
                    key_env, secret_env = acct_creds[acct]
                    res = _close_position(acct, key_env, secret_env, symbol, bn_pos["amt"])
                    close_results.append(res)

                scanner_state.setdefault("cooldowns", {})[symbol] = {
                    "expire_at": now + timeout_cooldown * 3600,
                    "type": "timeout",
                    "last_entry_price": entry_price,
                }

                timeout_msg = (
                    f"⏰ <b>交易阻击成交报告 · {coin}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"结果: ⏰ 因故平仓\n"
                    f"持仓 {hours_held:.1f} 小时未爆发\n"
                    f"入场: {entry_price:.4f}  现价: {cur_price:.4f}\n"
                    f"保证金盈亏: {margin_pnl:+.1f}%\n"
                    f"{chr(10).join(close_results)}\n"
                    f"冷却: {timeout_cooldown}小时"
                )
                _route("forced_close", timeout_msg)
                logger.info(f"[仓位] {coin} 超时平仓 保证金盈亏{margin_pnl:+.1f}% {close_results}")

                _settle_signal(scanner_state, symbol, "expired", cur_price)
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
