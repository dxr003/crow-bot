# Phase A 完工报告 · L0 Event Ledger 正名 + 五域接入

**commit**: `89f1e7a`
**耗时**: 约 10 分钟（AI 速度）
**执行授权**: 乌鸦「继续开始. 我批」

---

## 1. 定性落地

按你上午确认的三条定性，全部按字面执行：

| 定性条 | 执行结果 |
|---|---|
| 1. 更名 `ledger` 不折中 | `/root/logs/lib/` → `/root/ledger/`；类 `EventLogger` → `Ledger`；接口加 `parent_trace_id` + `cost_usd` 两字段 |
| 2. L0 强制共用，无 config 开关 | 所有业务模块 `from ledger import get_ledger` 直连，无 on/off 分支 |
| 3. 阶段 1 + 阶段 3 先做 | trace_id 贯穿 ✅ · cost_usd 字段就位 ✅ · 查询引擎（SQLite）推迟到事件量起来再上 |

物理路径保留 `/root/logs/` 不变（journalctl/tail/jq 习惯），Python 模块移到 `/root/ledger/`。

---

## 2. 五域账本接入状态

| 域 | 文件 | 事件类型 | 接入方 | 状态 |
|---|---|---|---|---|
| exec | `orders.jsonl` (15MB×10) | open/close/cancel/sl/tp/get_* | executor | ✅ |
| signal | `bull_sniper.jsonl` (10MB×5) | pool_entry / pool_exit / pool_rejected / signal / buy_result / buy_exception / buy_skipped | scanner | ✅ |
| dialog | `commands.jsonl` (10MB×5) | dispatch_hit / dispatch_miss / dispatch_denied / dispatch_error | dispatch | ✅ |
| system | `guardian.jsonl` (10MB×5) | heartbeat / alert_sent / alert_send_failed | guardian | ✅ |
| external | `api_calls.jsonl` (10MB×5) | call / call_failed (provider / endpoint / model / cost_usd) | `log_external_call()` 就位 | 🟡 待 Phase B 接 analyzer |

---

## 3. 里程碑：trace_id 贯穿验证

**场景 B（"2h 前这单哪个信号触发的"）实测通过：**

```bash
# 输入：做多 BTC 0.001u（测试用小额失败单，便于验证 tid 传播）

# dialog/commands.jsonl
{"ts":"2026-04-21T14:46:26+08:00","trace_id":"bc157eed",
 "event":"dispatch_hit",
 "payload":{"role":"玄玄","account":"币安1","action":"open_long","symbol":"BTCUSDT"}}

# exec/orders.jsonl
{"ts":"2026-04-21T14:46:26+08:00","trace_id":"bc157eed",
 "event":"open_market",
 "payload":{"role":"玄玄","account":"币安1","symbol":"BTCUSDT","ms":294,
           "ok":false,"error":"数量计算为 0"}}
```

**同 trace_id `bc157eed` 同时出现在 dialog + exec 两边 → 回溯链路打通。**

实现机制：
- `dispatch.try_dispatch()` 顶部 `set_trace_id(new_trace_id())`（ContextVar 进程内透传）
- `executor.@log_call` 从 `current_trace_id()` 继承，自动落盘到 `payload.trace_id`

scanner 侧同理：进池时 `pool_tid = new_trace_id()` 存进 `watchpool[symbol]["trace_id"]`，信号/买入阶段读回使用，且 `set_trace_id(signal_tid)` 让 buyer → executor 继续贯穿。

---

## 4. 服务状态

五服务全 active：maomao / tiantian / bull-sniper / damao / baobao
scanner.py 封板回位 `----i----`，已重启加载新代码。
maomao/tiantian 已重启加载新 dispatch.py。

---

## 5. Phase B 待办（需要解封板或价格表）

1. **analyzer.py 外部调用接入**：Haiku + Tavily 调用点加 `log_external_call()`；analyzer.py 当前封板，需你批解锁
2. **core.py ask_claude 路径 dialog 覆盖**：非交易 TG 消息（闲聊/问行情）当前不进 dialog 账本；core.py 是底座封板，要你批
3. **老日志清理**：`/root/maomao/logs/bot.log`（46MB）+ `scanner.log` 老部分 → 改为滚动+归档
4. **成本价格表**：`ledger/external.py` 只负责落账，价格计算交调用方；需要一份 `prices.py`（Anthropic tokens/Tavily credit/币安 API 等）

---

## 6. 文档同步

- `/root/maomao/trader/docs/claude_code_config_log.md` 补 6 条变更记录
- `/root/maomao/CLAUDE.md` 已在上一次 commit 同步 exec_log 新路径说明

---

## 7. 本轮规则遵守

- ✅ 铁律 1：scanner.py 封板改前 `chattr -i`，AST 检查通过，改完 `chattr +i` 回封
- ✅ 铁律 2：新建 `/root/ledger/` 模块，没改老 logs/lib 内部
- ✅ 铁律 4：没自写 HTTP 轮询模拟，用 Python 标准 RotatingFileHandler
- ✅ 铁律 5：上条消息先给了 3 题回答 + 改造方案，等到「继续开始. 我批」才动手
- ✅ 铁律 6：每改一块跑一次 smoke test（exec / scanner import / dispatch dry / guardian run）
- ✅ 铁律 7a：settings.json / CLAUDE.md / MCP / .env 全没碰
- ✅ 铁律 8：本次所有修改都在已批授权范围内
