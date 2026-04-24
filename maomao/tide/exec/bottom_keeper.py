"""bottom_keeper — 潮汐底仓守护器。

职责：
  1. 启动时 / 按需调用，检查配置账户的底仓是否存在
  2. 没有则按 config.mock_short 规格开底仓（做空 BTCUSDT）
  3. 已有底仓则跳过（不补齐、不调整，避免重复入场）

调用方式：
  python -m tide.exec.bottom_keeper           # CLI 检查一次
  python -m tide.exec.bottom_keeper --force   # 忽略已有仓位，强行开（慎用）

  # 代码调用（推荐 runner 启动时调一次）：
  from tide.exec.bottom_keeper import ensure_bottom
  r = ensure_bottom()   # → dict(ok, action, detail)

开仓规格（读 tide/config.yaml）：
  - role:     mock_short.role       默认 '玄玄'
  - account:  mock_short.account    默认 '币安2'
  - symbol:   mock_short.symbol     默认 'BTCUSDT'
  - 方向:     SHORT（做空，目前潮汐策略只做空底仓）
  - margin:   position.base_usd     默认 100
  - leverage: mock_short.leverage   默认 3
  - 保证金模式: CROSSED（全仓）

Shadow vs Live：
  system.mode == 'shadow' → 只日志+返回 dict，不真下单
  system.mode == 'live'   → 真调 executor.open_market

幂等：检查 positionAmt 非零即视为"已有底仓"，直接跳过。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

if "/root/maomao" not in sys.path:
    sys.path.insert(0, "/root/maomao")

from trader.multi import executor  # noqa: E402

logger = logging.getLogger("tide.bottom_keeper")

_CFG_PATH = Path("/root/maomao/tide/config.yaml")


def _load_cfg() -> dict:
    if not _CFG_PATH.exists():
        raise FileNotFoundError(f"潮汐配置缺失: {_CFG_PATH}")
    return yaml.safe_load(_CFG_PATH.read_text()) or {}


def _pick_short_position(positions: list[dict], symbol: str) -> dict | None:
    for p in positions:
        if p.get("symbol") != symbol:
            continue
        amt = float(p.get("positionAmt", 0) or 0)
        if amt < 0:
            return p
    return None


def ensure_bottom(force: bool = False) -> dict:
    """检查底仓；缺了就开。返回 {ok, action, detail}。"""
    cfg = _load_cfg()
    mode = (cfg.get("system") or {}).get("mode", "shadow")
    ms = cfg.get("mock_short") or {}
    role = ms.get("role", "玄玄")
    account = ms.get("account", "币安2")
    symbol = ms.get("symbol", "BTCUSDT")
    leverage = int(ms.get("leverage", 3))
    margin_usd = float((cfg.get("position") or {}).get("base_usd", 100))

    # 1. 查实盘持仓
    try:
        positions = executor.get_positions(role, account) or []
    except Exception as e:
        logger.error(f"[bottom] 查持仓失败 {account} {symbol}: {e}")
        return {"ok": False, "action": "error", "detail": f"query_fail: {e}"}

    existing = _pick_short_position(positions, symbol)
    if existing and not force:
        amt = abs(float(existing.get("positionAmt", 0)))
        notional = abs(float(existing.get("notional", 0) or 0))
        entry = float(existing.get("entryPrice", 0))
        detail = (f"已有底仓 {symbol} SHORT {amt} @ {entry:.2f} "
                  f"notional={notional:.2f}U initMargin="
                  f"{float(existing.get('initialMargin', 0)):.2f}U")
        logger.info(f"[bottom] [{account}] {detail} → 跳过")
        return {"ok": True, "action": "skip_existing", "detail": detail}

    plan = (f"开底仓 {role}@{account} {symbol} SHORT "
            f"margin={margin_usd}U lev={leverage}x CROSSED")

    # 2. shadow 不下单
    if mode != "live":
        logger.info(f"[bottom] [shadow] 本该 {plan}")
        return {"ok": True, "action": "shadow_would_open",
                "detail": f"shadow mode: {plan}"}

    # 3. live 下单
    try:
        r = executor.open_market(
            role, account,
            symbol=symbol, side="SELL",
            margin=margin_usd, leverage=leverage,
            margin_type="CROSSED",
        )
        logger.info(f"[bottom] [live] ✅ {plan} → {r}")
        return {"ok": True, "action": "opened", "detail": str(r)}
    except Exception as e:
        logger.error(f"[bottom] [live] ❌ 开底仓失败: {e}")
        return {"ok": False, "action": "open_fail", "detail": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description="潮汐底仓守护器")
    ap.add_argument("--force", action="store_true",
                    help="忽略已有仓位强行开（仅调试用）")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    r = ensure_bottom(force=args.force)
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
