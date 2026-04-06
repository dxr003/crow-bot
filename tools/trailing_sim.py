#!/usr/bin/env python3
"""
移动止盈 v3.0 验证脚本
两档策略：1档(≥55%) + 2档(≥110%)，容错±3%

用法：
  python3 trailing_sim.py          # 看参数验证表
  python3 trailing_sim.py sim      # 跑模拟走势
  python3 trailing_sim.py calc 80  # 计算当前峰值80%时的触发线
"""

import sys

# ── 策略参数 ─────────────────────────────────────────────
TIERS = {
    1: {
        "name": "1档（保）",
        "cmd":  "移1 / 保",
        "activate": 55,
        "ranges": [
            (55,  110, 25),   # 55~110%：回撤25%触发
            (110, 200, 20),   # 110~200%：回撤20%触发
            (200, 999, 15),   # 200%+：回撤15%触发
        ],
    },
    2: {
        "name": "2档（激）",
        "cmd":  "移2 / 激",
        "activate": 110,
        "ranges": [
            (110, 200, 20),   # 110~200%：回撤20%触发
            (200, 300, 15),   # 200~300%：回撤15%触发
            (300, 999, 10),   # 300%+：回撤10%触发
        ],
    },
}
TOLERANCE = 3   # ±3% 容错防抖


# ── 核心函数 ─────────────────────────────────────────────
def get_drawdown(tier_num: int, peak: float) -> float:
    """根据峰值浮盈，返回当前应用的回撤触发比例"""
    for lo, hi, dd in TIERS[tier_num]["ranges"]:
        if lo <= peak < hi:
            return dd
    return TIERS[tier_num]["ranges"][-1][2]

def trigger_at(peak: float, dd_pct: float) -> float:
    """峰值浮盈 + 回撤比例 → 触发时的浮盈"""
    return peak * (1 - dd_pct / 100)

def locked(peak: float, tier_num: int) -> float:
    """峰值浮盈时，该档能锁住的最低浮盈"""
    dd = get_drawdown(tier_num, peak)
    return trigger_at(peak, dd)

def effective_dd(tier_num: int, peak: float) -> float:
    """考虑容错后的实际触发回撤（需超过阈值+容错才确认）"""
    return get_drawdown(tier_num, peak) + TOLERANCE


# ── 模式1：参数验证表 ─────────────────────────────────────
def show_table():
    print("=" * 65)
    print("  移动止盈 v3.0 参数验证表（容错±3%）")
    print("=" * 65)

    peaks = [55, 70, 80, 100, 110, 130, 150, 200, 250, 300, 400]

    for t_num, t in TIERS.items():
        print(f"\n【{t['name']}】指令：{t['cmd']}  激活条件：≥{t['activate']}%")
        print(f"  {'峰值浮盈':>8}  {'回撤阈值':>8}  {'触发浮盈':>8}  {'锁住利润':>10}  {'实际触发(含容错)':>16}")
        print("  " + "-" * 58)
        for peak in peaks:
            if peak < t["activate"]:
                continue
            dd   = get_drawdown(t_num, peak)
            trig = trigger_at(peak, dd)
            lock = trig          # 锁住的最低浮盈约等于触发时浮盈
            eff  = effective_dd(t_num, peak)
            print(f"  {peak:>7}%  {dd:>7}%  {trig:>7.1f}%  {lock:>9.1f}%  超过{eff}%时确认触发")

    print()
    print("说明：")
    print("  触发浮盈 = 峰值 × (1 - 回撤阈值%)，即平仓时大约能拿到的利润")
    print("  容错±3%：回撤需超过(阈值+3%)才确认触发，防止噪音误扫")
    print()


# ── 模式2：走势模拟 ──────────────────────────────────────
def simulate():
    print("=" * 60)
    print("  移动止盈 v3.0 走势模拟")
    print("=" * 60)

    # 模拟一段典型走势：先涨后跌再涨再跌
    scenarios = [
        ("场景A：涨到80%后回撤",
         [10, 30, 55, 65, 80, 75, 70, 65, 60, 55, 50, 45]),
        ("场景B：涨到150%后回撤",
         [20, 60, 90, 110, 130, 150, 140, 130, 120, 110, 100, 90]),
        ("场景C：涨到300%后小回撤再触发",
         [50, 110, 180, 250, 300, 290, 280, 270, 260, 250]),
    ]

    for name, path in scenarios:
        print(f"\n{name}")
        print(f"  走势：{' → '.join(str(p)+'%' for p in path)}")

        for t_num, t in TIERS.items():
            peak    = 0.0
            triggered = False
            trigger_profit = None
            trigger_peak   = None

            for p in path:
                if p > peak:
                    peak = p

                if peak < t["activate"]:
                    continue

                dd      = get_drawdown(t_num, peak)
                eff_dd  = dd + TOLERANCE
                actual_drawdown = (peak - p) / peak * 100 if peak > 0 else 0

                if actual_drawdown >= eff_dd:
                    triggered      = True
                    trigger_profit = p
                    trigger_peak   = peak
                    break

            if triggered:
                dd = get_drawdown(t_num, trigger_peak)
                print(f"  {t['name']}：峰值{trigger_peak}%，回撤触发 → 平仓利润约 {trigger_profit:.1f}%  ✅")
            elif peak >= t["activate"]:
                print(f"  {t['name']}：峰值{peak}%，走势结束未触发（仍持仓）")
            else:
                print(f"  {t['name']}：峰值{peak}%，未达激活条件{t['activate']}%")


# ── 模式3：实时计算器 ─────────────────────────────────────
def calc(peak: float):
    print("=" * 50)
    print(f"  当前峰值浮盈：{peak}%")
    print("=" * 50)

    for t_num, t in TIERS.items():
        if peak < t["activate"]:
            print(f"\n{t['name']}：未激活（需≥{t['activate']}%）")
            continue
        dd   = get_drawdown(t_num, peak)
        trig = trigger_at(peak, dd)
        eff  = effective_dd(t_num, peak)
        print(f"\n{t['name']}（已激活）")
        print(f"  当前回撤阈值：{dd}%（含容错实际：{eff}%）")
        print(f"  触发线：浮盈跌破 {trig:.1f}% 即平仓")
        print(f"  现在回撤多少触发：峰值再跌 {peak - trig:.1f}% 绝对值")


# ── 入口 ─────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        show_table()
    elif args[0] == "sim":
        simulate()
    elif args[0] == "calc" and len(args) > 1:
        calc(float(args[1]))
    else:
        print("用法：")
        print("  python3 trailing_sim.py          # 参数验证表")
        print("  python3 trailing_sim.py sim      # 走势模拟")
        print("  python3 trailing_sim.py calc 80  # 计算峰值80%时的触发线")
