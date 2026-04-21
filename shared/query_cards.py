"""查询卡片渲染：独立小卡，不堆一起。

- render_positions_card(role, account) — 持仓（含围绕持仓的止盈止损挂单）
- render_wallet_card(role, account)    — 余额（合约+现货+资金 三项合并）
- render_all_card(role)                — 全账户净值汇总（仅大猫/玄玄）

权限：内部调 trader.multi.executor，按 role 自动拦截；
天天发 /all 会被 render_all_card 抛 PermissionError，bot 层接住回友好文案。
"""
from trader.multi import executor, registry

_STABLES = {"USDT", "USDC", "BUSD", "FDUSD"}
_ALL_CARD_ALLOWED = {"大猫", "玄玄"}  # 天天无"全查"入口


def _fmt_spot(spot: dict, top: int = 5) -> str:
    if not spot:
        return "空"
    items = sorted(spot.items(), key=lambda x: -x[1])
    shown = items[:top]
    rest = len(items) - len(shown)
    parts = [f"{a}:{v:.4f}" for a, v in shown]
    if rest > 0:
        parts.append(f"+{rest}种")
    return ", ".join(parts)


def _equity_usd(b: dict) -> float:
    """合约净值 + spot/funding 稳定币按 1U"""
    total = b["futures"]["total"] + b["futures"]["upnl"]
    for asset, amt in b.get("spot", {}).items():
        if asset in _STABLES:
            total += amt
    for asset, amt in b.get("funding", {}).items():
        if asset in _STABLES:
            total += amt
    return total


# ══════════════════════════════════════════
# 持仓 block 共享渲染（单账户卡 + 全账户卡共用）
# ══════════════════════════════════════════

def _render_position_block(p: dict, related_orders: list[dict]) -> list[str]:
    """一个持仓的 4 行详情块：币种/方向/数量/仓位值 → 入场/现价 → 爆仓 → 浮盈/保证金/杠杆
    + 关联挂单行（止损/止盈按方向分类）。
    """
    amt = float(p["positionAmt"])
    side = "多" if amt > 0 else "空"
    side_icon = "🟩多" if amt > 0 else "🟥空"
    entry = float(p.get("entryPrice", 0) or 0)
    mark = float(p.get("markPrice", 0) or 0)
    liq = float(p.get("liquidationPrice", 0) or 0)
    upnl = float(p.get("unRealizedProfit", 0) or 0)
    margin = float(p.get("isolatedWallet", 0) or 0) or float(p.get("initialMargin", 0) or 0)
    notional = abs(float(p.get("notional", 0) or 0)) or abs(amt) * (mark or entry)
    pnl_pct = (upnl / margin * 100) if margin > 0 else 0
    pnl_icon = "🟢" if upnl >= 0 else "🔴"
    sym = p["symbol"].replace("USDT", "")
    lev = round(notional / margin) if margin > 0 else None

    lines = [f"  <b>{sym}</b> {side_icon}  {abs(amt):.4f}  仓位值≈{notional:.0f}U"]
    if mark > 0:
        lines.append(f"    入场 {entry:.4f}  →  现价 {mark:.4f}")
    else:
        lines.append(f"    入场 {entry:.4f}")
    if liq > 0:
        lines.append(f"    爆仓 {liq:.4f}")
    if margin > 0:
        lev_s = f"  ~{lev}x" if lev else ""
        lines.append(f"    {pnl_icon} 浮盈 {upnl:+.2f}U ({pnl_pct:+.1f}%)  保证金{margin:.2f}U{lev_s}")
    else:
        lines.append(f"    {pnl_icon} 浮盈 {upnl:+.2f}U")

    for o in related_orders:
        tag = _classify_order(o, side)
        price = o.get("stopPrice") or o.get("price") or "?"
        try:
            price_s = f"{float(price):.4f}"
        except Exception:
            price_s = str(price)
        lines.append(f"    {tag} @ {price_s}")
    return lines


# ══════════════════════════════════════════
# 持仓卡（单账户，含围绕持仓的止盈止损挂单）
# ══════════════════════════════════════════

def render_positions_card(role: str, account: str) -> str:
    account = registry._resolve_name(account)
    positions = executor.get_positions(role, account)
    orders = executor.get_open_orders(role, account)

    lines = [f"📊 <b>{account} · 持仓</b>"]
    active = [p for p in positions if float(p["positionAmt"]) != 0]
    if not active:
        lines.append("  （无持仓）")
    else:
        for p in active:
            pos_orders = [o for o in orders if o.get("symbol") == p["symbol"]]
            lines.extend(_render_position_block(p, pos_orders))

    # 未关联持仓的孤立挂单
    used = {p["symbol"] for p in active}
    leftover = [o for o in orders if o.get("symbol") not in used]
    if leftover:
        lines.append(f"\n📌 <b>其他挂单 {len(leftover)}</b>")
        for o in leftover[:20]:
            price = o.get("stopPrice") or o.get("price")
            try:
                price_s = f"{float(price):.4f}"
            except Exception:
                price_s = str(price)
            lines.append(f"  {o['symbol']} {o['side']} {o['type']} @ {price_s} 数量{o['origQty']}")
    return "\n".join(lines)


# ══════════════════════════════════════════
# 余额卡（合约+现货+资金 三项合并）
# ══════════════════════════════════════════

def render_wallet_card(role: str, account: str) -> str:
    account = registry._resolve_name(account)
    b = executor.get_balance(role, account)

    lines = [f"💼 <b>{account} · 余额</b>"]
    f = b["futures"]
    upnl_icon = "🟢" if f["upnl"] >= 0 else "🔴"
    lines.append(f"💰 合约 {f['total']:.2f}U  可用{f['available']:.2f}U  {upnl_icon}浮盈{f['upnl']:+.2f}")

    spot = {k: v for k, v in b.get("spot", {}).items() if v >= 0.01}
    if spot:
        lines.append(f"🪙 现货 {_fmt_spot(spot, top=8)}")
    else:
        lines.append("🪙 现货 无")

    funding = {k: v for k, v in b.get("funding", {}).items() if v >= 0.01}
    if funding:
        lines.append(f"💵 资金 {_fmt_spot(funding, top=5)}")
    else:
        lines.append("💵 资金 无")

    lines.append(f"💎 净值 ≈ {_equity_usd(b):.2f}U")
    return "\n".join(lines)


# ══════════════════════════════════════════
# 全账户净值汇总（简化：只看余额，不查持仓）
# ══════════════════════════════════════════

def render_all_card(role: str) -> str:
    if role not in _ALL_CARD_ALLOWED:
        raise PermissionError(f"角色『{role}』不允许全查入口，请用 /pos{{N}} /bal{{N}} 独立查看")
    all_bal = executor.get_all_balances(role)
    lines = ["💼 <b>全账户净值</b>"]
    grand_total = 0.0
    grand_upnl = 0.0
    for name, b in all_bal.items():
        if "error" in b:
            lines.append(f"  【{name}】 ❌ {b['error']}")
            continue
        equity = _equity_usd(b)
        grand_total += equity
        f = b["futures"]
        grand_upnl += f["upnl"]
        upnl_icon = "🟢" if f["upnl"] >= 0 else "🔴"
        upnl_pct = (f["upnl"] / f["total"] * 100) if f["total"] > 0 else 0
        lines.append(
            f"  【{name}】 净值{equity:.2f}U  "
            f"合约{f['total']:.0f}(可用{f['available']:.0f})  "
            f"{upnl_icon}{f['upnl']:+.1f}U({upnl_pct:+.1f}%)"
        )
    grand_icon = "🟢" if grand_upnl >= 0 else "🔴"
    lines.append(f"\n💎 <b>总净值 ≈ {grand_total:.2f}U</b>  {grand_icon}总浮盈{grand_upnl:+.2f}U")
    return "\n".join(lines)


# ══════════════════════════════════════════
# 全账户持仓汇总（/1 用，按 role 自动过滤权限）
# ══════════════════════════════════════════

def _classify_order(o: dict, pos_side: str) -> str:
    """根据挂单类型+方向给中文标签：止损/止盈/限价/条件"""
    t = (o.get("type") or "").upper()
    side = o.get("side", "")
    reduce_only = o.get("reduceOnly", False) or o.get("closePosition", False)
    if "STOP" in t and "MARKET" in t:
        if reduce_only:
            if pos_side == "多" and side == "SELL":
                return "🛡 止损"
            if pos_side == "空" and side == "BUY":
                return "🛡 止损"
        return "⚠ 条件"
    if "TAKE_PROFIT" in t:
        return "🎯 止盈"
    if t == "TRAILING_STOP_MARKET":
        return "📈 移动止盈"
    if t == "LIMIT":
        return "📎 限价"
    return f"  {t}"


def render_all_positions_card(role: str) -> str:
    """遍历 role 有权限的账户，列出所有活跃持仓 + 挂单（清晰多行版）"""
    from trader.multi import registry as _reg
    from trader.multi import permissions as _perm
    lines = [f"📊 <b>全账户持仓</b>"]
    any_active = False
    for a in _reg.list_accounts(enabled_only=True):
        name = a["name"]
        if not _perm.check(role, "query", name):
            continue
        try:
            positions = executor.get_positions(role, name)
            orders = executor.get_open_orders(role, name)
        except Exception as e:
            lines.append(f"\n<b>【{name}】</b> ❌ {e}")
            continue
        active = [p for p in positions if float(p["positionAmt"]) != 0]
        if not active and not orders:
            continue
        any_active = True
        lines.append(f"\n<b>【{name}】</b>")
        if active:
            for p in active:
                pos_orders = [o for o in orders if o.get("symbol") == p["symbol"]]
                lines.extend(_render_position_block(p, pos_orders))
        else:
            lines.append("  （无持仓）")
        # 剩余挂单（没对应持仓的）
        used = {p["symbol"] for p in active}
        leftover = [o for o in orders if o.get("symbol") not in used]
        if leftover:
            lines.append(f"  📌 其他挂单 {len(leftover)}")
            for o in leftover[:10]:
                price = o.get("stopPrice") or o.get("price")
                lines.append(f"    {o['symbol']} {o['side']} {o['type']} @ {price} 数量{o['origQty']}")
    if not any_active:
        lines.append("\n📭 所有账户均无持仓无挂单")
    return "\n".join(lines)


# ══════════════════════════════════════════
# 全账户余额详情（/2 用，按 role 自动过滤权限）
# ══════════════════════════════════════════

def render_all_wallets_card(role: str) -> str:
    """遍历 role 有权限的账户，每个账户列出 合约/现货/资金 三项"""
    all_bal = executor.get_all_balances(role)
    lines = [f"💰 <b>全账户余额</b>"]
    grand_total = 0.0
    for name, b in all_bal.items():
        lines.append(f"\n<b>【{name}】</b>")
        if "error" in b:
            lines.append(f"  ❌ {b['error']}")
            continue
        f = b["futures"]
        upnl_icon = "🟢" if f["upnl"] >= 0 else "🔴"
        lines.append(f"  💰 合约 {f['total']:.2f}U  可用{f['available']:.2f}U  {upnl_icon}浮盈{f['upnl']:+.2f}")
        spot = {k: v for k, v in b.get("spot", {}).items() if v >= 0.01}
        if spot:
            lines.append(f"  🪙 现货 {_fmt_spot(spot, top=6)}")
        funding = {k: v for k, v in b.get("funding", {}).items() if v >= 0.01}
        if funding:
            lines.append(f"  💵 资金 {_fmt_spot(funding, top=3)}")
        equity = _equity_usd(b)
        grand_total += equity
        lines.append(f"  💎 净值 {equity:.2f}U")
    lines.append(f"\n━━━━━━━━━━━━━━━")
    lines.append(f"<b>合计 ≈ {grand_total:.2f}U</b>")
    return "\n".join(lines)


if __name__ == "__main__":
    print("=== 玄玄 /all ===")
    print(render_all_card("玄玄"))
    print("\n=== 玄玄 /pos3 ===")
    print(render_positions_card("玄玄", "币安3"))
    print("\n=== 玄玄 /bal3 ===")
    print(render_wallet_card("玄玄", "币安3"))
    print("\n=== 玄玄 /pos4 ===")
    print(render_positions_card("玄玄", "币安4"))
    print("\n=== 玄玄 /bal4 ===")
    print(render_wallet_card("玄玄", "币安4"))
    print("\n=== 天天 /all（应拒） ===")
    try:
        print(render_all_card("天天"))
    except Exception as e:
        print(f"EXCEPTION: {e}")
    print("\n=== 玄玄 /1（全账户持仓）===")
    print(render_all_positions_card("玄玄"))
    print("\n=== 玄玄 /2（全账户余额）===")
    print(render_all_wallets_card("玄玄"))
    print("\n=== 天天 /2（应自动过滤币安1）===")
    print(render_all_wallets_card("天天"))
