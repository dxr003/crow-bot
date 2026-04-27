# 查询快捷键审计（2026-04-19 16:40）

> 乌鸦发现 /2 只显示 币安1/2，漏了 币安3/4。
> 根因：新旧接口并存——/1 /2 走老接口 trader.multi_account（两账户），
> /all /pos /bal 走新接口 trader.multi（四账户）。
> 本文档列清楚每个快捷键的后端接口和状态，等乌鸦审。

---

## 当前接口地图

| 接口 | 文件 | 支持账户 | 状态 |
|------|------|----------|------|
| 🟢 新 | `/root/maomao/trader/multi/` (registry/executor/permissions) | 币安1/2/3/4 | 2026-04-19 建好，权限按 role 过滤 |
| 🟡 旧（本地） | `/root/maomao/trader/exchange.py` | 只币安1 | 封板，玄玄本地开单仍在用 |
| 🟡 旧（本地） | `/root/tiantian/trader/exchange.py` | 只币安2 | 封板，天天本地开单仍在用 |
| 🔴 过渡期遗留 | `/root/maomao/trader/multi_account.py` | 只币安1+2 | 两账户版 **已过时**，/1 /2 还在用 |
| 🔴 过渡期遗留 | `/root/shared/multi_account.py` | 只币安1+2 | 平仓记录查询，/1 卡片里用 |

---

## 玄玄（maomao）快捷键清单

| 命令 | 功能 | 后端 | 账户覆盖 | 状态 |
|------|------|------|----------|------|
| `/all` | 全账户净值汇总 | trader.multi (executor.get_all_balances) | ✅ 1/2/3/4 | 2026-04-19 新加 |
| `/pos1`–`/pos4` | 各账户持仓+挂单 | trader.multi (executor.get_positions + get_open_orders) | ✅ 1/2/3/4 | 2026-04-19 新加 |
| `/bal1`–`/bal4` | 各账户余额（合约+现货+资金） | trader.multi (executor.get_balance) | ✅ 1/2/3/4 | 2026-04-19 新加 |
| `/1` | 持仓+近期平仓 | 🔴 trader.multi_account + shared.multi_account | ❌ **只1/2** | **要重构** |
| `/2` | 各账户余额 | 🔴 trader.multi_account.get_all_balances | ❌ **只1/2** | **要重构**（乌鸦刚发现的问题）|
| `/3`–`/6` | 现货↔合约↔资金 划转 | 本地 trader.exchange.transfer_funds | ⚠️ 只币安1（玄玄本地） | 单账户够用，多账户划转未做 |
| `/7` | 交易日志 | trader.trade_log | 通用 | OK |
| `/8` | Bot 运行事件 | trader.bot_log | 通用 | OK |
| `/9` | 系统快照 | 本机 | 通用 | OK |

## 天天（tiantian）快捷键清单

| 命令 | 功能 | 后端 | 账户覆盖 | 状态 |
|------|------|------|----------|------|
| `/pos2`–`/pos4` | 币安2/3/4 持仓+挂单 | trader.multi | ✅ 2/3/4（权限挡币安1）| 2026-04-19 新加 |
| `/bal2`–`/bal4` | 币安2/3/4 余额 | trader.multi | ✅ 2/3/4 | 2026-04-19 新加 |
| `/1` | 持仓+近期平仓 | 🟡 本地 trader.exchange + shared.multi_account | ⚠️ 只币安2（震天响） | 原设计单账户OK，但和 /pos* 重复 |
| `/2` | 币安2 余额 | 🟡 本地 trader.exchange.get_all_balances | ⚠️ 只币安2 | 原设计单账户OK，但和 /bal2 重复 |
| `/3`–`/6` | 划转 | 本地 trader.exchange | ⚠️ 只币安2 | OK |

---

## 重构方案（等乌鸦拍板）

### A. 把 /1 /2 迁到新接口 `trader.multi`
- `/1` 改成走 `executor.get_all_positions(role)` → 四账户持仓汇总
- `/2` 改成走 `executor.get_all_balances(role)` → 四账户余额汇总
- 天天 role 会自动过滤掉币安1

**优点**：统一后端，/1 /2 变成"多账户汇总"的语义
**缺点**：/1 卡片里的"近期平仓记录"那块逻辑也要在 multi 里做；工作量中等

### B. 废掉 /1 /2，让用户直接用 /all /pos /bal
- 菜单里删掉 /1 /2
- 保留 /3-/9 划转和日志

**优点**：命令少，语义清晰；新接口单一
**缺点**：失去"近期平仓记录"功能，要单独做 /hist 或类似

### C. /1 /2 保留原样但改名
- /1 → /recent（最近平仓）
- /2 → 删掉（被 /all 取代）

---

## 其他遗留 / 待确认

1. **划转 /3-/6 只能动 币安1（玄玄）/ 币安2（天天）**
   - 多账户划转（给 币安3/4 挪钱）目前不能做
   - 要不要加 `/transfer3 <方向> <金额>` 这类？还是暂时手动搞？

2. **guardian cron 已装**（每10分钟巡检四账户+服务+bull_sniper）
   - 日志：`/root/maomao/data/guardian.log`

3. **bull_sniper 的 币安3/4 entries 加到 config.yaml**
   - enabled: false（还没授权它动 李红兵/组六 账户）
   - systemd EnvironmentFile 已链到 `/root/safe/*.env`，4 把钥匙都在进程环境里

4. **权限表 permissions.yaml**
   - 大猫/玄玄：全账户查询+交易
   - 天天：除币安1 外全部

---

## 我倾向做 A（迁老接口到新接口）

理由：
- 用户习惯 /1 /2，不想改键位
- 新接口已经跑通四账户，/pos /bal 自测 OK
- /1 的"近期平仓"可以让 multi_account.py 保留那一段（shared 那份只是查平仓历史，没有下单能力，可以不动）

等乌鸦答复。
