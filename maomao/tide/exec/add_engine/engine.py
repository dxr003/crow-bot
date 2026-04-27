"""加仓引擎主循环：每个 tick 遍历 rules → trigger → guards → executor_bridge。

入口：
  from tide.exec.add_engine import run_once
  run_once()   # 外部 cron / tide runner 调用
"""
from __future__ import annotations
import logging
import sys
import time
from pathlib import Path

from .context import TickContext
from .rules_loader import RulesCache
from . import reject_log
from . import executor_bridge
from .executor_bridge import resolve_margin

logger = logging.getLogger("add_engine.engine")

_CACHE = RulesCache()


def _fetch_mark_price(symbol: str) -> float:
    # 2026-04-27 Step 6-B: 走 api_hub 统一封装层
    if "/root/maomao" not in sys.path:
        sys.path.insert(0, "/root/maomao")
    from trader.api_hub.binance import fapi
    return float(fapi.get_premium_index(symbol)["markPrice"])


def _load_positions(account: str, symbol: str) -> list[dict]:
    """查指定账户的合约持仓（只返回该 symbol）。失败返空。"""
    if "/root/maomao" not in sys.path:
        sys.path.insert(0, "/root/maomao")
    try:
        from trader.multi import executor
        all_pos = executor.get_positions("玄玄", account) or []
        return [p for p in all_pos if p.get("symbol") == symbol]
    except Exception as e:
        logger.warning(f"[engine] 查持仓失败 {account} {symbol}: {e}")
        return []


def _load_tide_state() -> dict:
    p = Path("/root/maomao/tide/state/state.json")
    if not p.exists():
        return {}
    try:
        import json
        return json.loads(p.read_text())
    except Exception:
        return {}


class AddEngine:
    def __init__(self):
        self.cache = _CACHE

    def run(self) -> dict:
        raw, rules = self.cache.get()
        eng_cfg = (raw.get("engine") or {})
        enabled = bool(eng_cfg.get("enabled", False))
        shadow = bool(eng_cfg.get("shadow", True))
        now = int(time.time())

        if not enabled:
            logger.info("[engine] engine.enabled=false，跳过 tick")
            return {"tick_ts": now, "enabled": False, "shadow": shadow,
                    "rules_total": len(rules), "fired": 0}

        if not rules:
            logger.info("[engine] 无启用 rule，tick 结束")
            return {"tick_ts": now, "enabled": True, "shadow": shadow,
                    "rules_total": 0, "fired": 0}

        tide_state = _load_tide_state()
        # 每个 tick 内对同一个 account+symbol 的 positions 做缓存，少打一次 API
        pos_cache: dict[tuple[str, str], list[dict]] = {}
        price_cache: dict[str, float] = {}

        fired = 0
        per_rule = []
        for rule in rules:
            symbol = rule["symbol"]
            account = rule["account"]
            try:
                if symbol not in price_cache:
                    price_cache[symbol] = _fetch_mark_price(symbol)
                cur = price_cache[symbol]
            except Exception as e:
                reject_log.reject(rule["id"], "tick", f"拉标记价失败: {e}")
                per_rule.append({"id": rule["id"], "result": "price_fail", "err": str(e)})
                continue

            key = (account, symbol)
            if key not in pos_cache:
                pos_cache[key] = _load_positions(account, symbol)
            positions = pos_cache[key]

            ctx = TickContext(
                now_ts=now,
                cur_price=cur,
                mark_price=cur,
                symbol=symbol,
                positions=positions,
                last_sell=(tide_state.get("last_sell")
                           if isinstance(tide_state, dict) else None),
                last_add=None,
                engine_state={},
                tide_state=tide_state if isinstance(tide_state, dict) else {},
            )

            # 提前解析 margin（guard 和 bridge 都要用）
            margin_eff, margin_reason = resolve_margin(rule, ctx)
            ctx.extra["margin_effective"] = margin_eff
            ctx.extra["margin_reason"] = margin_reason

            trigger = rule["trigger"]
            try:
                fire = trigger.should_fire(ctx)
            except Exception as e:
                reject_log.trigger_skip(rule["id"], trigger.kind, f"异常: {e}")
                per_rule.append({"id": rule["id"], "result": "trigger_err", "err": str(e)})
                continue
            if not fire:
                per_rule.append({"id": rule["id"], "result": "no_fire"})
                continue

            # 守门员串行
            blocked = False
            for g in rule["guards"]:
                ok, reason = g.check(rule, ctx)
                if not ok:
                    reject_log.reject(rule["id"], f"guard:{g.kind}", reason)
                    per_rule.append({"id": rule["id"], "result": f"blocked:{g.kind}", "reason": reason})
                    blocked = True
                    break
            if blocked:
                continue

            res = executor_bridge.execute_fire(rule, fire_price=cur, shadow=shadow, ctx=ctx)
            if res.get("ok"):
                fired += 1
                per_rule.append({"id": rule["id"], "result": "fired",
                                 "shadow": res.get("shadow", False)})
            else:
                per_rule.append({"id": rule["id"], "result": "exec_fail",
                                 "reason": res.get("reason")})

        logger.info(f"[engine] tick 完 rules={len(rules)} fired={fired} shadow={shadow}")
        return {"tick_ts": now, "shadow": shadow, "rules_total": len(rules),
                "fired": fired, "per_rule": per_rule}


def run_once() -> dict:
    return AddEngine().run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    import json as _json
    r = run_once()
    print(_json.dumps(r, ensure_ascii=False, indent=2))
