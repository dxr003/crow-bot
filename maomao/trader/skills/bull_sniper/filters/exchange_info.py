"""
exchange_info.py — 候选池前置过滤：下架 / SETTLING / 交割合约剔除

设计原则（2026-04-21 乌鸦钦定）：
- 唯一真相来源：Binance fapi /fapi/v1/exchangeInfo
- 不维护本地黑名单，下架跟随交易所
- fail-closed：连续 FAIL_CLOSE_THRESHOLD 次刷新失败 → is_tradeable 返回 (False, "info_unavailable_fail_closed")
- 被剔项由调用方写 filter_log.jsonl

规则：
    status == "TRADING"        → pass
    status != "TRADING"        → reject "status_<actual>"  (SETTLING/PRE_SETTLE/BREAK/CLOSE/…)
    不在 symbols 列表          → reject "delisted"
    contractType != "PERPETUAL"→ reject "contract_<actual>" (交割合约)
    exchangeInfo 连续 3 次失败 → reject "info_unavailable_fail_closed"

缓存：TTL 5 分钟
"""
from __future__ import annotations

import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)

# 2026-04-27 Step 6-B: 走 api_hub 统一封装层
if "/root/maomao" not in sys.path:
    sys.path.insert(0, "/root/maomao")
from trader.api_hub.binance import fapi as _fapi

_TTL_SEC = 300           # 5 分钟
_FAIL_CLOSE_THRESHOLD = 3

_lock = threading.Lock()
_cache: dict[str, dict] = {}    # symbol → {"status": "TRADING", "contractType": "PERPETUAL"}
_cache_ts: float = 0
_consecutive_fail: int = 0


def _refresh() -> bool:
    """拉 exchangeInfo 刷新缓存。成功返回 True，失败 False（且失败计数 +1）。"""
    global _cache, _cache_ts, _consecutive_fail
    try:
        data = _fapi.get_exchange_info()
        new_cache = {}
        for s in data.get("symbols", []):
            sym = s.get("symbol")
            if not sym:
                continue
            new_cache[sym] = {
                "status": s.get("status"),
                "contractType": s.get("contractType"),
            }
        with _lock:
            _cache = new_cache
            _cache_ts = time.time()
            _consecutive_fail = 0
        logger.info(f"[exchange_info_filter] 刷新完毕，{len(new_cache)}个合约在册")
        return True
    except Exception as e:
        with _lock:
            _consecutive_fail += 1
        logger.warning(f"[exchange_info_filter] 刷新失败 (连续第{_consecutive_fail}次): {e}")
        return False


def _ensure_fresh() -> None:
    """命中缓存或刷新。调用前必须持锁判断。"""
    now = time.time()
    if _cache and now - _cache_ts < _TTL_SEC:
        return
    _refresh()


def is_tradeable(symbol: str) -> tuple[bool, str]:
    """返回 (通过?, 原因)。

    原因格式：
        通过 → "ok"
        拒   → "status_SETTLING" / "status_BREAK" / "delisted" / "contract_DELIVERING" / "info_unavailable_fail_closed"
    """
    _ensure_fresh()

    with _lock:
        # fail-closed：连续失败超阈值，直接拒
        if _consecutive_fail >= _FAIL_CLOSE_THRESHOLD:
            return False, "info_unavailable_fail_closed"

        # 缓存仍为空（冷启动第一次就失败）也视为 fail-closed
        if not _cache:
            return False, "info_unavailable_fail_closed"

        info = _cache.get(symbol)

    if info is None:
        return False, "delisted"

    status = info.get("status")
    if status != "TRADING":
        return False, f"status_{status or 'UNKNOWN'}"

    contract = info.get("contractType")
    # 永续合约 contractType = "PERPETUAL"；交割合约是 CURRENT_QUARTER/NEXT_QUARTER/PERPETUAL_DELIVERING 等
    if contract and contract != "PERPETUAL":
        return False, f"contract_{contract}"

    return True, "ok"


def get_stats() -> dict:
    """调试/dry-run/健康检查用"""
    with _lock:
        return {
            "cached_symbols": len(_cache),
            "cache_age_sec": round(time.time() - _cache_ts) if _cache_ts else None,
            "consecutive_fail": _consecutive_fail,
            "fail_closed": _consecutive_fail >= _FAIL_CLOSE_THRESHOLD,
            "ttl_sec": _TTL_SEC,
        }


def reset_state() -> None:
    """仅测试/dry-run 用，清空缓存"""
    global _cache, _cache_ts, _consecutive_fail
    with _lock:
        _cache = {}
        _cache_ts = 0
        _consecutive_fail = 0
