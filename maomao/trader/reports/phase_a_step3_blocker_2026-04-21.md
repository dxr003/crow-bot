# Phase A · 第 3 步阻塞点报告

**时间**：2026-04-21 15:50+08
**状态**：阻塞 · 等乌鸦拍 A/B/C

---

## 1. 触发

执行第 3 步"order_placed 全链路试水" 先做 `@log_call` 装饰器挂点摸底时，发现 conventions §8.1 列的 5 个挂点函数 **4 个在真实代码里不存在**。

## 2. 真实代码形态

### 2.1 multi/ 目录下的文件（11 个 py，无封板）
```
_atomic.py / __init__.py / _self_test.py
dispatch.py     19KB  try_dispatch + 6 个 _do_* 动作分发
exec_log.py      8KB  ledger exec 域写入器
executor.py     37KB  open_market/open_limit/open_liq/close_market/cancel_order 等
guardian.py     10KB  夜间守护
permissions.py   5KB  check(role, action, account) 权限闸门
registry.py      9KB  account 注册
strategy_router.py  7KB  策略路由（STRATEGIES 表）
```

**无 `order.py`** · **无 `risk.py`** · **封板均无 +i**（lsattr 三文件皆 `--------------e-------`）

### 2.2 executor.py 函数清单（关键下单函数）
```
L348 cancel_order          ← conventions §8.1 对得上的唯一函数
L385 open_market           ← 市价开仓，主力
L457 close_market          ← 市价平仓
L561 _place_close_trigger  ← 平仓触发单
L606 place_stop_loss       ← 止损
L616 place_take_profit     ← 止盈
L623 cancel_all
L679 open_limit            ← 限价开仓
L745 add_to_position       ← 加仓
L772 open_liq              ← 强平价反推开仓
L917 transfer              ← 现货-合约划转
```

### 2.3 dispatch.py 函数清单
```
L174 try_dispatch          ← TG 指令总入口
L273 _do_open              ← 开仓分发（调 executor.open_*）
L326 _do_close             ← 平仓分发
L339 _do_tp
L354 _do_sl
L368 _do_cancel
L379 _do_add
```

### 2.4 真实下单链路

```
bot.py
 └─ dispatch.try_dispatch (dispatch.py:174)
     ├─ permissions.check (permissions.py:66)     ← 权限闸门
     └─ dispatch._do_open (dispatch.py:273)        ← 开仓分发
         └─ executor.open_market (executor.py:385) ← 真实 REST 下单
             └─ [Binance API]
```

**没有独立 `risk.check` 环节**。风控逻辑分散：
- 权限闸门 → `permissions.check`（谁能对哪个账户做什么）
- 精度/杠杆/保证金模式 → `executor._fix` / `_set_leverage` / `_set_margin_mode`
- 系统级风控（单笔 500U/日亏 200U/回撤 15%）→ `scripts/risk_manager.py`（旧世界，未接入新 multi/ 架构）

## 3. Conventions §8.1 vs 真实对照表

| conventions §8.1 批的 | 真实代码 | 差距 |
|---|---|---|
| `trader/multi/order.py::place_order` | ❌ 不存在 | `multi/` 下无 `order.py`；实际开仓散在 `executor.open_market/open_limit/open_liq/add_to_position` 4 个函数 |
| `trader/multi/order.py::cancel_order` | `executor.py:348` | 文件名错，函数名对 |
| `trader/multi/risk.py::check` | ❌ 不存在 | `multi/` 下无 `risk.py`；语义最接近的是 `permissions.check:66`（权限闸门） |
| `trader/multi/executor.py::dispatch_open` | ❌ 不存在 | `dispatch.py:273 _do_open` 才是开仓分发总入口 |
| `trader/multi/executor.py::dispatch_close` | ❌ 不存在 | `dispatch.py:326 _do_close` |

**根因**：我 15:23 起草 conventions v1.0 §8.1 时，按乌鸦指示的"理想函数名"写了 5 个挂点，没核对代码实际分布。乌鸦批文档时也无法人工穿 37KB executor.py。

## 4. 三个选项

### 4.1 A · 对齐现实改 conventions §8.1

**5 挂点改为**：
1. `trader/multi/dispatch.py::try_dispatch`（TG 指令总入口）
2. `trader/multi/dispatch.py::_do_open`（开仓分发）
3. `trader/multi/dispatch.py::_do_close`（平仓分发）
4. `trader/multi/executor.py::open_market`（市价开仓主力函数）
5. `trader/multi/executor.py::close_market`（市价平仓）

**建议额外加**：`trader/multi/permissions.py::check`（第 6 挂点，权限闸门，对 trace_id 追溯价值高）

**改动**
- `conventions.md §8.1` 整段重写
- 版本升 v1.2
- CHANGELOG 加一行"v1.2：§8.1 挂点对齐真实代码"

### 4.2 B · 按 conventions 重构代码

新建 `trader/multi/order.py` + `trader/multi/risk.py`，把相关逻辑从 executor 抽出，让代码对齐 conventions 原定模型。

**改动**
- 新建 2 个文件
- 搬移 executor 的下单逻辑 / dispatch 的 `_do_*` 分发
- 全链路联调测试
- 风险：触碰铁律 2（新功能不碰旧模块），连锁修改半个 multi/

### 4.3 C · A + conventions 留锚点

选 A 的 5 挂点落地，但 conventions §8.1 额外保留一节"未来目标"：

> 未来 Phase X 若建立 `multi/order.py` + `multi/risk.py`，挂点迁移到 `place_order` / `risk.check`，当前文档以现实挂点为准。

**改动**
- 同 A，但 conventions §8.1 额外补 2 段说明
- 防止未来 AI 读 §8.1 时只看见现实挂点、忘了乌鸦的原设计意图

## 5. 推荐 · A

**A 副作用**：conventions 改 ~15 行；真实跑得通；与乌鸦原定"order.py/risk.py"概念模型短暂脱节（但代码本来就是现状）
**B 副作用**：第 3 步耗时 ×5；触铁律 2；新旧代码共存期风险
**C 副作用**：同 A + conventions §8.1 混"现实挂点 + 未来目标"，后人读时要分清楚，增加 1 层认知负荷

## 6. 不做

等乌鸦拍板。未拍板前不动任何业务代码、不动 conventions.md、不装任何装饰器。
