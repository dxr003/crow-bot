"""把引擎的"应该加仓"落到真实下单。

live：
  entry_type=market（默认）→ trader.multi.executor.open_market
  entry_type=limit         → trader.multi.executor.open_limit（币安原生 LIMIT GTC）
shadow：只记日志不下单

margin_mode（rule 可选字段，默认 fixed）：
  - fixed               → margin = rule.margin_usd
  - dynamic_last_sell   → margin = ctx.last_sell.sold_usd × factor（买回潮汐卖出量）

limit 参数（entry_type=limit 时）：
  - limit_price: float           # 绝对挂单价（优先）
  - limit_offset_pct: float      # 相对 fire_price 偏移 %，正=更有利方向
    long:  挂在 fire_price × (1 - offset/100)   → 跌一点成交
    short: 挂在 fire_price × (1 + offset/100)   → 涨一点成交
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .context import TickContext
from . import state as engine_state
from . import reject_log

logger = logging.getLogger("add_engine.executor_bridge")

_TIDE_STATE_PATH = Path("/root/maomao/tide/state/state.json")


def _consume_tide_quota(margin_usd: float):
    """live fire 后把占用的保证金从 tide 额度池扣掉。
    total_usd += margin; remaining_usd -= margin（不跌破 0）。
    保持 tide 不变式 remaining = TOTAL_CAPITAL - total_usd。
    失败静默，不阻塞下单路径。shadow 不调。"""
    try:
        if not _TIDE_STATE_PATH.exists():
            return
        s = json.loads(_TIDE_STATE_PATH.read_text(encoding="utf-8"))
        ps = s.setdefault("position_structure", {})
        m = float(margin_usd)
        ps["total_usd"] = round(float(ps.get("total_usd", 0) or 0) + m, 2)
        ps["remaining_usd"] = round(
            max(0.0, float(ps.get("remaining_usd", 0) or 0) - m), 2
        )
        _TIDE_STATE_PATH.write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"_consume_tide_quota failed: {e}")


def resolve_margin(rule: dict, ctx: TickContext | None) -> tuple[float, str]:
    """返回 (margin_usd, reason_if_zero_or_empty_reason)。

    dynamic_last_sell 读 ctx.last_sell.sold_usd × factor，clamp 到 min/max。
    fixed 就拿 rule.margin_usd。任何无法确定的情况返回 (0, reason)。
    """
    mode = (rule.get("margin_mode") or "fixed").lower()

    if mode == "fixed":
        m = float(rule.get("margin_usd", 0) or 0)
        return (m, "") if m > 0 else (0, "margin_usd<=0")

    if mode == "dynamic_last_sell":
        if ctx is None:
            return 0, "dynamic_last_sell 需要 ctx"
        ls = ctx.last_sell or {}
        sold_usd = float(ls.get("sold_usd") or 0)
        if sold_usd <= 0:
            return 0, "last_sell.sold_usd<=0"
        factor = float(rule.get("margin_factor", 1.0))
        m = sold_usd * factor
        mn = float(rule.get("margin_min_usd", 0) or 0)
        mx = float(rule.get("margin_max_usd", 0) or 0)
        if mn and m < mn:
            m = mn
        if mx and m > mx:
            m = mx
        return (m, "") if m > 0 else (0, "dynamic_margin_resolved<=0")

    return 0, f"未知 margin_mode={mode}"


def resolve_limit_price(rule: dict, side: str, fire_price: float) -> tuple[float, str]:
    """返回 (limit_price, reason)。若未使用 limit 或解析失败返回 (0, reason)。

    优先级：limit_price（绝对） > limit_offset_pct（相对 fire_price）
    """
    lp = rule.get("limit_price")
    if lp is not None:
        try:
            lp = float(lp)
            return (lp, "") if lp > 0 else (0, "limit_price<=0")
        except (TypeError, ValueError):
            return 0, f"limit_price 解析失败: {lp}"

    off = rule.get("limit_offset_pct")
    if off is None:
        return 0, "limit 模式缺 limit_price / limit_offset_pct"
    try:
        off = float(off)
    except (TypeError, ValueError):
        return 0, f"limit_offset_pct 解析失败: {off}"

    # 正值 = 更有利方向（long 挂低、short 挂高），等反向成交
    if side == "long":
        price = fire_price * (1.0 - off / 100.0)
    else:
        price = fire_price * (1.0 + off / 100.0)
    return (price, "") if price > 0 else (0, "offset 计算后 price<=0")


def execute_fire(rule: dict, fire_price: float, shadow: bool,
                 ctx: TickContext | None = None) -> dict:
    """返回 {"ok", "shadow", "reason"?, "result"?, "margin_usd"?, "entry_type"?}"""
    rid = rule["id"]
    side = rule["side"]
    account = rule["account"]
    leverage = int(rule.get("leverage", 3))
    symbol = rule.get("symbol", "BTCUSDT")
    entry_type = (rule.get("entry_type") or "market").lower()

    # 1. margin
    margin = 0.0
    if ctx is not None:
        margin = float(ctx.extra.get("margin_effective", 0) or 0)
    if margin <= 0:
        margin, reason = resolve_margin(rule, ctx)
        if margin <= 0:
            reject_log.reject(rid, "bridge", reason or "margin<=0")
            return {"ok": False, "shadow": shadow, "reason": reason or "margin<=0"}

    # 2. 如果是 limit，先算挂单价
    limit_price = 0.0
    if entry_type == "limit":
        limit_price, reason = resolve_limit_price(rule, side, fire_price)
        if limit_price <= 0:
            reject_log.reject(rid, "bridge", reason)
            return {"ok": False, "shadow": shadow, "reason": reason}

    # 3. shadow 不下单
    if shadow:
        if entry_type == "limit":
            logger.info(f"[shadow] {rid} 本该挂限价 {symbol} {side} "
                        f"{margin}U {leverage}x LIMIT @ {limit_price:.4f} "
                        f"(fire_price={fire_price:.4f})")
        else:
            logger.info(f"[shadow] {rid} 本该市价加仓 {symbol} {side} "
                        f"{margin}U {leverage}x @~{fire_price:.4f}")
        reject_log.fire(rid, fire_price, margin, side, account, shadow=True,
                        extra={"entry_type": entry_type, "limit_price": limit_price})
        engine_state.record_fire(rid, fire_price, margin)
        return {"ok": True, "shadow": True, "margin_usd": margin,
                "entry_type": entry_type, "limit_price": limit_price or None}

    # 4. live 路径
    if "/root/maomao" not in sys.path:
        sys.path.insert(0, "/root/maomao")
    from trader.multi import executor
    bn_side = "BUY" if side == "long" else "SELL"
    try:
        if entry_type == "limit":
            res = executor.open_limit(
                "玄玄", account, symbol=symbol, side=bn_side,
                margin=margin, leverage=leverage, price=limit_price,
                margin_type="CROSSED",
            )
        else:
            res = executor.open_market(
                "玄玄", account, symbol=symbol, side=bn_side,
                margin=margin, leverage=leverage, margin_type="CROSSED",
            )
    except Exception as e:
        reject_log.reject(rid, "bridge", f"{entry_type} open 异常: {e}")
        return {"ok": False, "shadow": False, "reason": str(e)}

    if not res or res.get("ok") is False or res.get("error"):
        reject_log.reject(rid, "bridge", f"{entry_type} open 失败: {res}")
        return {"ok": False, "shadow": False, "reason": str(res)}

    reject_log.fire(rid, fire_price, margin, side, account, shadow=False,
                    extra={"order": res, "entry_type": entry_type})
    engine_state.record_fire(rid, fire_price, margin)
    _consume_tide_quota(margin)
    logger.info(f"[live] {rid} {entry_type} 加仓成功 margin={margin}U {res}")
    return {"ok": True, "shadow": False, "result": res, "margin_usd": margin,
            "entry_type": entry_type, "limit_price": limit_price or None}
