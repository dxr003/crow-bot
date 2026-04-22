"""human_tracker.py — 人类操作行为账本

定位（2026-04-22 乌鸦批）：补全 4 个账户的"人类在 App 手动操作"记录。
AI 操作已由 executor @log_call 进入 /root/logs/exec/orders.jsonl；
此模块抓 allOrders / income 增量，对照 exec_log 区分 AI vs 人类，
把人类动作落到独立账本 /root/logs/human/operations.jsonl。

只记不推，留铁证。

## 运行方式
- 单次：`python3 -m trader.multi.human_tracker scan`
- Bootstrap（仅初始化 cursor，不写账本）：`python3 -m trader.multi.human_tracker bootstrap`
- 状态：`python3 -m trader.multi.human_tracker status`

cron 每分钟跑一次 `scan`。

## 事件类型（新）
- account_income  · 资金流水（FUNDING_FEE/COMMISSION/REALIZED_PNL/TRANSFER 等）
- human_order_new · 人类新下单（status=NEW 首次见到）
- human_order_filled · 人类订单成交
- human_order_cancelled · 人类订单撤销/过期
- ai_order_echo   · AI 单的订单回声（low-priority，便于对照）

## 数据流
```
4 账户 × 每分钟
  ├─ get_income_history(startTime=cursor+1)  → account_income
  ├─ collect_symbols(pos + income)
  └─ for sym: get_all_orders(symbol, orderId=cursor+1)
        ├─ orderId ∈ ai_order_ids → ai_order_echo
        └─ orderId ∉ ai_order_ids → human_order_*
```

## cursor 文件
`/root/maomao/data/human_tracker_cursor.json`
```
{
  "币安1": {"last_income_time": 1776800000000, "last_order_id": {"SOLUSDT": 210216429891}},
  ...
}
```
"""
from __future__ import annotations

import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if "/root" not in sys.path:
    sys.path.insert(0, "/root")

from ledger import get_ledger
from trader.multi import registry
from trader.multi._atomic import atomic_write_json

logger = logging.getLogger(__name__)

# ── 路径 ──────────────────────────────────
CURSOR_FILE = Path("/root/maomao/data/human_tracker_cursor.json")
EXEC_LOG = Path("/root/logs/exec/orders.jsonl")

# ── 账本（复用 exec 域，独立文件 human_operations.jsonl） ─
# 人类操作账本按乌鸦指示长期保留证据，按需人工清理，实质关闭轮转。
_human_logger = get_ledger(
    "exec",
    "human_operations",
    max_bytes=1 * 1024 ** 4,
    backup_count=99,
)

# ── 配置 ──────────────────────────────────
def _list_accounts() -> list[str]:
    """从 registry 动态拉启用账户列表（accounts.yaml 加/禁账户自动同步）。"""
    return [a["name"] for a in registry.list_accounts(enabled_only=True)]

INCOME_LIMIT = 1000
ORDERS_LIMIT = 500
# 对照 AI 订单 ID 时往回看的 exec_log 历史跨度（秒）
AI_LOOKBACK_SECONDS = 14 * 86400  # 14 天
TZ_BJ = timezone(timedelta(hours=8))

# 币安系统/AI 自动生成 clientOrderId 前缀：algo 触发后的 MARKET 单多见 autoclose-xxx；
# API 默认前缀 x-；ADL 强平走 adl_ / autodeleveraging-。命中即归为系统触发单，
# 不算 human（避免 algo 止损触发后被 human_tracker 误报为人工平仓）。
SYSTEM_CID_PREFIXES = ("autoclose-", "autodeleveraging-", "adl_", "auto-", "x-")


def _is_system_cid(client_order_id: str | None) -> bool:
    if not client_order_id:
        return False
    return any(client_order_id.startswith(p) for p in SYSTEM_CID_PREFIXES)


# ══════════════════════════════════════════
# cursor 读写
# ══════════════════════════════════════════

def _empty_cursor() -> dict:
    return {acc: {"last_income_time": 0, "last_order_id": {}} for acc in _list_accounts()}


def load_cursor() -> dict:
    if not CURSOR_FILE.exists():
        return _empty_cursor()
    try:
        data = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        # 补齐缺失账户（accounts.yaml 新增时兼容）
        for acc in _list_accounts():
            if acc not in data:
                data[acc] = {"last_income_time": 0, "last_order_id": {}}
            else:
                data[acc].setdefault("last_income_time", 0)
                data[acc].setdefault("last_order_id", {})
        return data
    except Exception as ex:
        logger.warning(f"[human_tracker] cursor 读取失败，重建: {ex}")
        return _empty_cursor()


def save_cursor(cursor: dict) -> None:
    atomic_write_json(CURSOR_FILE, cursor)


# ══════════════════════════════════════════
# AI orderId 集合（从 exec_log 提取，mtime 缓存）
# ══════════════════════════════════════════

# 每个 jsonl 文件独立缓存：{path: (mtime_ns, ids_set)}。cron 每分钟跑一次，
# 轮转备份文件几乎不变，只有当前 orders.jsonl 的 mtime 变化；变了才重扫。
_AI_IDS_CACHE: dict[str, tuple[int, set[str]]] = {}


def _scan_ids_in_file(fp: Path, cutoff_iso: str) -> set[str]:
    ids: set[str] = set()
    try:
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = e.get("ts") or ""
                if ts and ts < cutoff_iso:
                    continue
                p = e.get("payload") or {}
                r = p.get("result") or {}
                if isinstance(r, dict):
                    oid = r.get("orderId")
                    if oid:
                        ids.add(str(oid))
                    for c in r.get("closed") or []:
                        if isinstance(c, dict) and c.get("orderId"):
                            ids.add(str(c["orderId"]))
    except Exception as ex:
        logger.warning(f"[human_tracker] 读 {fp} 失败: {ex}")
    return ids


def load_ai_order_ids(lookback_seconds: int = AI_LOOKBACK_SECONDS) -> set[str]:
    """从 /root/logs/exec/orders.jsonl + 轮转备份中扫出所有 AI 下过单的 orderId。

    按文件 mtime 缓存：未变文件复用 _AI_IDS_CACHE，变动文件才重扫。
    覆盖当前 + 最多 10 个轮转备份（14 天窗口）。
    """
    cutoff_iso = (datetime.now(TZ_BJ) - timedelta(seconds=lookback_seconds)).isoformat(timespec="seconds")

    files: list[Path] = []
    if EXEC_LOG.exists():
        files.append(EXEC_LOG)
    for i in range(1, 11):
        bak = EXEC_LOG.with_suffix(f".jsonl.{i}")
        if bak.exists():
            files.append(bak)

    all_ids: set[str] = set()
    for fp in files:
        key = str(fp)
        try:
            mtime_ns = fp.stat().st_mtime_ns
        except OSError:
            continue
        cached = _AI_IDS_CACHE.get(key)
        if cached and cached[0] == mtime_ns:
            all_ids |= cached[1]
            continue
        ids = _scan_ids_in_file(fp, cutoff_iso)
        _AI_IDS_CACHE[key] = (mtime_ns, ids)
        all_ids |= ids
    return all_ids


# ══════════════════════════════════════════
# 扫描核心
# ══════════════════════════════════════════

def _iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ_BJ).isoformat(timespec="seconds")


def _collect_symbols(client, incomes: list[dict]) -> set[str]:
    """整合"需要扫订单"的 symbol 集合：当前持仓 + income 里涉及的 symbol"""
    symbols: set[str] = set()
    for inc in incomes:
        s = inc.get("symbol")
        if s:
            symbols.add(s)
    try:
        positions = client.get_position_risk() or []
        for p in positions:
            try:
                if float(p.get("positionAmt", 0)) != 0:
                    symbols.add(p["symbol"])
            except Exception:
                pass
    except Exception as ex:
        logger.warning(f"[human_tracker] 拉持仓失败: {ex}")
    return symbols


def _scan_incomes(account: str, client, cursor_acc: dict, dry_run: bool = False) -> list[dict]:
    """拉 income 增量，落 account_income 事件。返回本次拉到的 income list（供下一步复用）"""
    last_ts = int(cursor_acc.get("last_income_time") or 0)
    start_ms = last_ts + 1 if last_ts > 0 else int((time.time() - 3600) * 1000)  # 首跑只拉 1h 顶
    try:
        incomes = client.get_income_history(startTime=start_ms, limit=INCOME_LIMIT) or []
    except Exception as ex:
        logger.error(f"[human_tracker][{account}] income 拉取失败: {ex}")
        return []

    if not incomes:
        return []

    # 按时间升序
    incomes = sorted(incomes, key=lambda x: int(x.get("time") or 0))

    if not dry_run:
        for inc in incomes:
            payload = {
                "account": account,
                "income_type": inc.get("incomeType"),
                "symbol": inc.get("symbol") or None,
                "amount": float(inc.get("income") or 0),
                "asset": inc.get("asset"),
                "tran_id": inc.get("tranId"),
                "trade_id": inc.get("tradeId") or None,
                "at": _iso_from_ms(int(inc.get("time"))),
            }
            _human_logger.event(
                "account_income",
                payload,
                actor="human_tracker",
                target=inc.get("symbol") or account,
                result="n-a",
            )

    cursor_acc["last_income_time"] = int(incomes[-1].get("time") or 0)
    return incomes


def _scan_orders_for_symbol(
    account: str, client, symbol: str, cursor_acc: dict,
    ai_order_ids: set[str], dry_run: bool = False,
) -> int:
    """拉单 symbol 的 allOrders 增量，落人类/AI 订单事件。返回新增条数。"""
    last_id_map = cursor_acc.setdefault("last_order_id", {})
    last_id = int(last_id_map.get(symbol) or 0)

    try:
        orders = client.get_all_orders(
            symbol=symbol,
            orderId=(last_id + 1) if last_id > 0 else None,
            limit=ORDERS_LIMIT,
        ) or []
    except Exception as ex:
        logger.error(f"[human_tracker][{account}] get_all_orders {symbol} 失败: {ex}")
        return 0

    if not orders:
        return 0

    orders = sorted(orders, key=lambda o: int(o.get("orderId") or 0))

    if not dry_run:
        for o in orders:
            oid = str(o.get("orderId"))
            cid = o.get("clientOrderId")
            is_ai = oid in ai_order_ids or _is_system_cid(cid)
            status = o.get("status") or "UNKNOWN"
            # 根据订单 status 决定事件类型（FILLED/CANCELED/EXPIRED/NEW）
            if is_ai:
                event_type = "ai_order_echo"
            else:
                if status == "FILLED":
                    event_type = "human_order_filled"
                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    event_type = "human_order_cancelled"
                else:
                    event_type = "human_order_new"

            payload = {
                "account": account,
                "symbol": symbol,
                "order_id": oid,
                "client_order_id": o.get("clientOrderId"),
                "side": o.get("side"),
                "position_side": o.get("positionSide"),
                "order_type": o.get("type"),
                "status": status,
                "orig_qty": float(o.get("origQty") or 0),
                "executed_qty": float(o.get("executedQty") or 0),
                "avg_price": float(o.get("avgPrice") or 0) or None,
                "stop_price": float(o.get("stopPrice") or 0) or None,
                "reduce_only": o.get("reduceOnly"),
                "close_position": o.get("closePosition"),
                "at": _iso_from_ms(int(o.get("updateTime") or o.get("time") or 0)),
                "source": "ai" if is_ai else "human",
            }
            result = "success" if status == "FILLED" else ("failed" if status in ("REJECTED", "EXPIRED") else "n-a")
            _human_logger.event(
                event_type,
                payload,
                actor="human_tracker",
                target=symbol,
                result=result,
            )

    last_id_map[symbol] = int(orders[-1].get("orderId") or last_id)
    return len(orders)


def _scan_account(
    account: str, cursor: dict, ai_order_ids: set[str],
    dry_run: bool, bootstrap: bool,
) -> dict:
    """单账户扫描。返回统计信息 dict，同时就地更新 cursor[account]。"""
    acc_stat = {"ok": True, "incomes": 0, "orders": 0, "symbols": 0, "error": None}
    try:
        client = registry.get_futures_client(account)
    except Exception as ex:
        acc_stat["ok"] = False
        acc_stat["error"] = str(ex)
        return acc_stat

    cursor_acc = cursor.setdefault(account, {"last_income_time": 0, "last_order_id": {}})

    if bootstrap:
        now_ms = int(time.time() * 1000)
        cursor_acc["last_income_time"] = now_ms
        try:
            positions = client.get_position_risk() or []
            active_syms = {p["symbol"] for p in positions if float(p.get("positionAmt", 0)) != 0}
            recent_incomes = client.get_income_history(
                startTime=now_ms - 24 * 3600 * 1000, limit=INCOME_LIMIT
            ) or []
            for inc in recent_incomes:
                if inc.get("symbol"):
                    active_syms.add(inc["symbol"])
            for sym in active_syms:
                orders = client.get_all_orders(symbol=sym, limit=10) or []
                if orders:
                    max_id = max(int(o.get("orderId") or 0) for o in orders)
                    cursor_acc["last_order_id"][sym] = max_id
            acc_stat["symbols"] = len(active_syms)
        except Exception as ex:
            acc_stat["ok"] = False
            acc_stat["error"] = f"bootstrap 失败: {ex}"
        return acc_stat

    # 正常扫：income 增量 → 订单增量（symbol 维度并行）
    try:
        incomes = _scan_incomes(account, client, cursor_acc, dry_run=dry_run)
        acc_stat["incomes"] = len(incomes)

        symbols = _collect_symbols(client, incomes)
        acc_stat["symbols"] = len(symbols)

        if not symbols:
            acc_stat["orders"] = 0
        else:
            def _do(sym: str) -> int:
                return _scan_orders_for_symbol(
                    account, client, sym, cursor_acc, ai_order_ids, dry_run=dry_run
                )
            with ThreadPoolExecutor(max_workers=min(len(symbols), 5)) as ex:
                acc_stat["orders"] = sum(ex.map(_do, symbols))
    except Exception as ex:
        acc_stat["ok"] = False
        acc_stat["error"] = str(ex)
        logger.exception(f"[human_tracker][{account}] 扫描异常")

    return acc_stat


def scan_once(dry_run: bool = False, bootstrap: bool = False) -> dict:
    """扫一次所有账户（4 账户并行）。
    - dry_run: 不写账本，只拉不落
    - bootstrap: 首次初始化 cursor（把游标推到"当前时刻"，不写历史到账本）
    """
    cursor = load_cursor()
    ai_order_ids = load_ai_order_ids()
    accounts = _list_accounts()
    stats = {"ai_id_cache": len(ai_order_ids), "accounts": {}}

    if not accounts:
        return stats

    def _task(acc: str) -> tuple[str, dict]:
        return acc, _scan_account(acc, cursor, ai_order_ids, dry_run, bootstrap)

    with ThreadPoolExecutor(max_workers=min(len(accounts), 4)) as ex:
        for acc, acc_stat in ex.map(_task, accounts):
            stats["accounts"][acc] = acc_stat

    if not dry_run:
        save_cursor(cursor)

    return stats


# ══════════════════════════════════════════
# CLI
# ══════════════════════════════════════════

def _fmt_stats(stats: dict) -> str:
    lines = [f"AI orderId 对照集: {stats['ai_id_cache']} 条"]
    for acc, s in stats["accounts"].items():
        if not s["ok"]:
            lines.append(f"  {acc}: ❌ {s['error']}")
        else:
            lines.append(
                f"  {acc}: incomes={s['incomes']}  symbols={s['symbols']}  orders={s['orders']}"
            )
    return "\n".join(lines)


def _cmd_status() -> None:
    cursor = load_cursor()
    print(f"Cursor 文件: {CURSOR_FILE}")
    for acc in _list_accounts():
        c = cursor.get(acc, {})
        ts = c.get("last_income_time", 0)
        ts_str = _iso_from_ms(ts) if ts > 0 else "(未初始化)"
        oids = c.get("last_order_id", {})
        print(f"  {acc}: income 位点 {ts_str}  已追 {len(oids)} 个 symbol")
        for sym, oid in list(oids.items())[:5]:
            print(f"      {sym} → orderId {oid}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        stats = scan_once(dry_run=False)
        print(_fmt_stats(stats))
    elif cmd == "bootstrap":
        stats = scan_once(bootstrap=True)
        print("Bootstrap 完成（游标推到现在，未写账本）:")
        print(_fmt_stats(stats))
    elif cmd == "dry":
        stats = scan_once(dry_run=True)
        print("Dry-run（不写 cursor / 账本）:")
        print(_fmt_stats(stats))
    elif cmd == "status":
        _cmd_status()
    else:
        print(f"未知命令: {cmd}，可选: scan / bootstrap / dry / status")
        sys.exit(1)
