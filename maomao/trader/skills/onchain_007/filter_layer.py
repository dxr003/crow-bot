"""过滤层：市值 / 流动性 / 量 / 上线时长 四个硬门槛"""
from __future__ import annotations


def pass_filter(pool: dict, cfg: dict) -> tuple[bool, str]:
    """返回 (是否通过, 失败原因)"""
    f = cfg["filters"]
    mc = pool["marketcap_usd"]
    if mc < f["min_marketcap_usd"]:
        return False, f"mcap<{f['min_marketcap_usd']/1e4:.0f}万"
    if mc > f["max_marketcap_usd"]:
        return False, f"mcap>{f['max_marketcap_usd']/1e4:.0f}万"
    if pool["liquidity_usd"] < f["min_liquidity_usd"]:
        return False, f"liq<{f['min_liquidity_usd']/1e4:.0f}万"
    if pool["volume_h24"] < f["min_24h_volume_usd"]:
        return False, f"vol24h<{f['min_24h_volume_usd']/1e4:.0f}万"
    if pool["age_hours"] < f["min_age_hours"]:
        return False, f"age<{f['min_age_hours']}h"
    # 2026-04-26 老大爆发力双门槛（C 方案严选）
    min_ratio = f.get("min_volume_to_mcap", 0)
    if min_ratio > 0 and mc > 0:
        ratio = pool["volume_h24"] / mc
        if ratio < min_ratio:
            return False, f"量/市值{ratio:.1f}x<{min_ratio}x"
    min_chg = f.get("min_24h_change_pct", 0)
    if min_chg > 0 and pool["change_h24"] < min_chg:
        return False, f"24h+{pool['change_h24']:.0f}%<{min_chg}%"
    return True, ""


def calc_stars(pool: dict, cfg: dict) -> int:
    """爆发力 1-3 星（h24 涨幅 + 量综合）"""
    chg = pool["change_h24"]
    vol = pool["volume_h24"]
    t = cfg["star_thresholds"]
    if chg >= t["three_star"]["h24_change_min"] and vol >= t["three_star"]["h24_volume_min"]:
        return 3
    if chg >= t["two_star"]["h24_change_min"] and vol >= t["two_star"]["h24_volume_min"]:
        return 2
    return 1
