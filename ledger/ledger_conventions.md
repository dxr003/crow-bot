# Ledger Conventions · 事件账本约定

**版本**：v1.2（2026-04-21）
**定位**：乌鸦团队所有模块共用的 L0 事件账本约定文档
**修改原则**：类 7b 规则——改前出 diff、乌鸦明批、再落地

**CHANGELOG**
- v1.2 (2026-04-21)：§8.1 挂点对齐真实代码（v1.0 列的 `order.py`/`risk.py` 不存在），改为 dispatch._do_open/_do_close + executor.open_market/close_market + permissions.check，共 6 点
- v1.1 (2026-04-21)：result enum 扩至 7 态，新增 `timeout` / `rate_limited`，来源 Phase B 第 2 步实测冲突
- v1.0 (2026-04-21)：起草首版

---

## 1. 账本边界声明（最高约束）

**只记动作，不记成本。**

**✅ 记**
- 做了什么动作（下单/撤单/风控拦截/信号触发/告警/服务重启...）
- 谁做的（actor）、对谁做的（target）、结果如何（result）
- 关键链路经过了哪些代码文件（related_files）
- 完整因果链（trace_id + parent_trace_id）

**❌ 不记**
- `cost_usd` / `token_usage` / `credit_cost` — 订阅/附加付费模式下统计裸 API 价无意义
- `provider` / `model` 作为审计字段（策略决策必要信息除外）
- `latency_ms`（除非专项性能排查）

**三大设计目标（乌鸦 2026-04-21 定）**
1. **动作追溯**：下过的单走过什么路径、触发了哪些规则，一键回放
2. **修改影响分析**：防"改 A 忘 B"——改完 order.py 今天还能跑通吗
3. **信号回溯**：2h 前那单源自哪条信号、当时评分几何

---

## 2. 时区：+08 全栈统一

所有时间戳字段使用 **ISO 8601 北京时间（UTC+8）**。

**格式**：`2026-04-21T15:18:53+08:00`

**Python 标准写法**
```python
from datetime import datetime, timezone, timedelta
TZ_BJ = timezone(timedelta(hours=8))
datetime.now(TZ_BJ).isoformat(timespec="seconds")
```

**禁止**：UTC Z / naive datetime / 本地字符串 / `time.time()` 浮点秒 作为持久化字段。

**理由**：VPS 固定部署，乌鸦在中国，journalctl/tail/jq 肉眼排查直观；无跨时区迁移预期。

---

## 3. trace_id 规则

### 3.1 格式
- 8 位十六进制小写字符串：`bc157eed`
- 生成：`secrets.token_hex(4)`
- 导出：`ledger.new_trace_id()`

### 3.2 进程内传递（ContextVar）
```python
from ledger import new_trace_id, set_trace_id, current_trace_id

tid = new_trace_id()
set_trace_id(tid)                    # 进程 ContextVar 写入
# 之后同进程任意深度的 ledger.event() 会自动继承
```

### 3.3 跨进程传递（显式 payload）
```python
# 模块 A（进程 1）：
ledger.event("signal_pushed", {..., "trace_id_out": tid})

# 模块 B（进程 2）收到后显式：
set_trace_id(incoming_payload["trace_id_out"])
```

### 3.4 父子链路（parent_trace_id）
- **场景 1**：signal → 多个 order。signal 的 tid 作为后续 order 的 `parent_trace_id`
- **场景 2**：user_message → 多个 bot 动作。user_message 的 tid 作为下游 `parent_trace_id`

### 3.5 生命周期起点
| 起点 | 动作 |
|---|---|
| 用户 TG 消息进入 dispatch | `set_trace_id(new_trace_id())` |
| scanner 进池（pool_entry） | `pool_tid = new_trace_id()` 存进 watchpool |
| scanner 信号生成（signal_pushed） | `signal_tid = pool.get("trace_id") or new_trace_id()` |
| guardian 每次巡检 run() | `new_trace_id()` |
| 外部 API 调用（analyzer → Tavily/Haiku） | 继承当前 ContextVar，不新建 |

---

## 4. JSONL 字段 Schema

### 4.1 必选字段
```json
{
  "ts": "2026-04-21T15:18:53+08:00",
  "trace_id": "bc157eed",
  "level": "INFO",
  "actor": "dispatch",
  "event_type": "dispatch_hit",
  "target": "BTCUSDT",
  "result": "success",
  "payload": { }
}
```

| 字段 | 类型 | 约束 |
|---|---|---|
| `ts` | string | +08 ISO8601 |
| `trace_id` | string | 8 位 hex |
| `level` | enum | DEBUG / INFO / WARNING / ERROR |
| `actor` | string | 产生方模块名（如 `executor`/`scanner`/`dispatch`/`guardian`） |
| `event_type` | enum | 27 种之一（见 §6） |
| `target` | string | 事件对象（币种/订单号/signal_id/账户名），无对象时 `""` |
| `result` | enum | 7 态：`success` / `failed` / `timeout` / `rate_limited` / `partial` / `pending` / `n-a` |
| `payload` | object | 事件具体字段，允许嵌套 |

#### 4.1.1 result 语义层级

- **成功态**：`success` / `partial`
- **失败大类**：`failed` / `timeout` / `rate_limited`
  - `timeout` 和 `rate_limited` 是 `failed` 的**语义子类**：网络超时、被上游限流
  - 查询"所有失败"：`result IN ('failed', 'timeout', 'rate_limited')`
  - 查询"超时专项"：`result = 'timeout'`（精确命中，不会被通用 failed 污染）
- **中间态**：`pending`（动作发起未完成，如 order_placed 还没成交）
- **不适用**：`n-a`（事件无明确结果语义，如 signal_scanned 扫描本身）

**使用原则**
- `timeout` / `rate_limited` 的扩充是"允许使用"不是"强制使用"
- 各域遇到真实场景再用，**不预先在业务代码里写 timeout 分支**
- 如果一个失败既能算 timeout 也能算 failed，优先精准（timeout）

### 4.2 可选字段
| 字段 | 触发条件 |
|---|---|
| `parent_trace_id` | 链接父任务（跨域/跨进程因果） |
| `error_msg` | `result == "failed"` 时**必填** |
| `related_files` | 金路径或 failed 自动采集（见 §7） |

### 4.3 禁用字段
- ❌ `cost_usd` / `provider` / `model` / `token_usage` / `credit_cost`
- 历史 jsonl（2026-04-21 之前）含 `cost_usd: null` 字段位，读取代码**忽略**，不报错

---

## 5. 七个域职责划分

| 域 | 物理路径 | 事件数 | 主要写入方 | 职责 |
|---|---|---|---|---|
| `exec` | `/root/logs/exec/orders.jsonl` | 7 | executor / order | 开仓平仓撤单成交仓位调整 |
| `signal` | `/root/logs/signal/bull_sniper.jsonl` | 5 | scanner | 扫描/评分/否决/推送/规则命中 |
| `risk` | `/root/logs/risk/risk_checks.jsonl` | 2 | risk_manager | 风控通过/拦截 |
| `system` | `/root/logs/system/guardian.jsonl` | 5 | guardian / 运维脚本 | 心跳/告警/服务重启/配置变更 |
| `dialog` | `/root/logs/dialog/commands.jsonl` | 4 | dispatch / core | 用户消息/bot 回复/动作/工具调用 |
| `external` | `/root/logs/external/api_calls.jsonl` | 3 | analyzer / 新闻抓取 / AI 调用点 | 外部 API 三态（不记成本） |
| `trace` | `/root/logs/trace/module_called.jsonl` | 1 | `@log_call` 装饰器 | 金路径 5 函数入口 |

---

## 6. 27 个 event_type 样本定义

### 6.1 exec（7）

**order_placed** · 下单请求发起（REST 发出，未确认）
```json
{"ts":"2026-04-21T15:18:53+08:00","trace_id":"bc157eed","level":"INFO","actor":"executor","event_type":"order_placed","target":"BTCUSDT","result":"pending","payload":{"account":"币安1","side":"BUY","qty":0.001,"order_type":"MARKET","positionSide":"LONG"}}
```

**order_filled** · 成交回报（全部成交）
```json
{"ts":"...","trace_id":"bc157eed","level":"INFO","actor":"executor","event_type":"order_filled","target":"12345678","result":"success","payload":{"symbol":"BTCUSDT","qty":0.001,"avg_price":67890.5,"commission":0.0001}}
```

**order_cancelled** · 撤单成功
```json
{"ts":"...","trace_id":"bc157eed","level":"INFO","actor":"executor","event_type":"order_cancelled","target":"12345678","result":"success","payload":{"reason":"tp_replaced"}}
```

**order_failed** · 下单失败（带 error_msg）
```json
{"ts":"...","trace_id":"bc157eed","level":"ERROR","actor":"executor","event_type":"order_failed","target":"BTCUSDT","result":"failed","payload":{"qty":0.001},"error_msg":"数量计算为 0","related_files":["trader/multi/executor.py","trader/multi/order.py"]}
```

**position_opened** · 仓位建立（可能多笔订单合成一个仓位）
```json
{"ts":"...","trace_id":"bc157eed","level":"INFO","actor":"executor","event_type":"position_opened","target":"BTCUSDT","result":"success","payload":{"account":"币安1","side":"LONG","avg_entry":67890.5,"qty":0.001,"leverage":10}}
```

**position_closed** · 仓位关闭（触发 SL/TP 或手动平）
```json
{"ts":"...","trace_id":"abc99999","parent_trace_id":"bc157eed","level":"INFO","actor":"executor","event_type":"position_closed","target":"BTCUSDT","result":"success","payload":{"exit_price":68500,"pnl_usdt":12.3,"reason":"take_profit"}}
```

**position_adjusted** · 加减仓/调杠杆/移动止盈换价
```json
{"ts":"...","trace_id":"...","level":"INFO","actor":"executor","event_type":"position_adjusted","target":"BTCUSDT","result":"success","payload":{"action":"trailing_move","old_stop":67000,"new_stop":68100}}
```

### 6.2 signal（5）

**signal_scanned** · 单次全市场扫描命中（非进池即退出）
```json
{"ts":"...","trace_id":"sc123456","level":"INFO","actor":"scanner","event_type":"signal_scanned","target":"","result":"n-a","payload":{"scanned_count":480,"matched_count":12,"duration_ms":830}}
```

**signal_scored** · 单币评分完成
```json
{"ts":"...","trace_id":"sc123457","level":"INFO","actor":"scanner","event_type":"signal_scored","target":"PEPEUSDT","result":"n-a","payload":{"score":32,"factors":{"vol_ratio":2.1,"buy_pct":0.65,"ath_drop":0.72}}}
```

**signal_rejected** · 进池 / 评分 / AI 任意环节否决
```json
{"ts":"...","trace_id":"sc123458","level":"INFO","actor":"scanner","event_type":"signal_rejected","target":"XYZUSDT","result":"n-a","payload":{"stage":"pre_filter","reason":"listed_days<30"}}
```

**signal_pushed** · 推 TG 信号卡
```json
{"ts":"...","trace_id":"sc123459","level":"INFO","actor":"scanner","event_type":"signal_pushed","target":"PEPEUSDT","result":"success","payload":{"score":32,"channel":"bull_sniper_signal"}}
```

**rule_triggered** · 单条规则命中（用 rule_id 定位）
```json
{"ts":"...","trace_id":"sc123459","level":"INFO","actor":"scanner","event_type":"rule_triggered","target":"PEPEUSDT","result":"n-a","payload":{"rule_id":"vol_ratio_gte_1_5","rule_value":2.1,"threshold":1.5}}
```

### 6.3 risk（2）

**risk_check_passed**
```json
{"ts":"...","trace_id":"bc157eed","level":"INFO","actor":"risk","event_type":"risk_check_passed","target":"BTCUSDT","result":"success","payload":{"checks":["single_order_lte_500","daily_loss_lte_200","drawdown_lte_15pct"],"order_usdt":50}}
```

**risk_check_blocked**
```json
{"ts":"...","trace_id":"bc157eed","level":"WARNING","actor":"risk","event_type":"risk_check_blocked","target":"BTCUSDT","result":"failed","payload":{"rule":"single_order_lte_500","attempted_usdt":800,"limit":500},"error_msg":"单笔超限","related_files":["trader/multi/risk.py","trader/multi/executor.py"]}
```

### 6.4 system（5）

**heartbeat** · 每次 guardian 巡检
```json
{"ts":"...","trace_id":"g111","level":"INFO","actor":"guardian","event_type":"heartbeat","target":"","result":"success","payload":{"accounts_ok":4,"accounts_total":4,"services_ok":5,"services_total":5,"bull_sniper_age_sec":12,"anomaly_count":0}}
```

**alert_sent**
```json
{"ts":"...","trace_id":"g111","level":"WARNING","actor":"guardian","event_type":"alert_sent","target":"admin","result":"success","payload":{"anomalies":["svc:maomao inactive"],"key":"svc:maomao"}}
```

**alert_send_failed**
```json
{"ts":"...","trace_id":"g111","level":"ERROR","actor":"guardian","event_type":"alert_send_failed","target":"admin","result":"failed","payload":{"anomalies":["svc:maomao inactive"]},"error_msg":"telegram 502"}
```

**service_restarted** · systemd 重启（由运维脚本/deploy hook 写）
```json
{"ts":"...","trace_id":"g112","level":"INFO","actor":"ops","event_type":"service_restarted","target":"maomao","result":"success","payload":{"reason":"dispatch.py 改动","by":"大猫","commit":"89f1e7a"}}
```

**config_changed** · settings/config 变更
```json
{"ts":"...","trace_id":"g113","level":"INFO","actor":"ops","event_type":"config_changed","target":"scanner.config.yaml","result":"success","payload":{"field":"pool_vol_threshold","old":8,"new":10,"by":"乌鸦"}}
```

### 6.5 dialog（4）

**user_message** · 乌鸦 TG 发话
```json
{"ts":"...","trace_id":"bc157eed","level":"INFO","actor":"dispatch","event_type":"user_message","target":"maomao","result":"n-a","payload":{"user":"乌鸦","role":"玄玄","text":"做多 BTC 0.001u"}}
```

**bot_reply** · bot 回复文本
```json
{"ts":"...","trace_id":"bc157eed","parent_trace_id":"bc157eed","level":"INFO","actor":"maomao","event_type":"bot_reply","target":"乌鸦","result":"success","payload":{"text":"下单失败：数量计算为 0","length":11}}
```

**bot_action_taken** · bot 执行了某动作（非聊天）
```json
{"ts":"...","trace_id":"bc157eed","level":"INFO","actor":"dispatch","event_type":"bot_action_taken","target":"open_long","result":"failed","payload":{"account":"币安1","symbol":"BTCUSDT"}}
```

**bot_tool_called** · bot 调用工具（curl/grep/Read 等）
```json
{"ts":"...","trace_id":"bc157eed","level":"INFO","actor":"maomao","event_type":"bot_tool_called","target":"Bash","result":"success","payload":{"tool":"Bash","brief":"systemctl restart maomao"}}
```

### 6.6 external（3）

**api_call_started**
```json
{"ts":"...","trace_id":"sc123459","level":"INFO","actor":"analyzer","event_type":"api_call_started","target":"tavily","result":"pending","payload":{"endpoint":"/search","query_hash":"a7f3..."}}
```

**api_call_completed**
```json
{"ts":"...","trace_id":"sc123459","level":"INFO","actor":"analyzer","event_type":"api_call_completed","target":"tavily","result":"success","payload":{"endpoint":"/search","status":200,"result_count":5}}
```

**api_call_failed**
```json
{"ts":"...","trace_id":"sc123459","level":"ERROR","actor":"analyzer","event_type":"api_call_failed","target":"tavily","result":"failed","payload":{"endpoint":"/search","status":502},"error_msg":"Bad Gateway","related_files":["trader/skills/bull_sniper/analyzer.py"]}
```

### 6.7 trace（1）

**module_called** · 金路径函数入口
```json
{"ts":"...","trace_id":"bc157eed","level":"DEBUG","actor":"executor","event_type":"module_called","target":"open_market","result":"n-a","payload":{"function":"trader.multi.executor.open_market","args_summary":{"role":"玄玄","account":"币安1","symbol":"BTCUSDT"},"caller":"trader.multi.dispatch._do_open"}}
```

---

## 7. related_files 触发规则

**方案 C 混合 + failed 自动**（乌鸦 2026-04-21 批）。

### 7.1 自动采集
1. **金路径 5 函数调用** → 自动 `traceback.extract_stack()` 取最近 3 层文件路径
2. **任何事件 `result == "failed"`** → 自动采集调用栈 3 层（含触发事件的文件）

### 7.2 手动传入
```python
ledger.event("xxx", payload, related_files=["trader/multi/executor.py"])
```
非金路径 & 非 failed 的事件，调用方按需显式传。

### 7.3 采集规则
- 只保留 `/root/` 下的业务文件路径
- 自动过滤：site-packages / logging/ asyncio 等标准库
- 最多 5 项，超过取最近 5 条

### 7.4 开关
环境变量 `LEDGER_RELATED_FILES=0` 全局关闭（降噪 / 调试用）

---

## 8. @log_call 装饰器

### 8.1 初始挂载点（6 个，v1.2 对齐真实代码）

> v1.0 原定 5 点里 `order.py::place_order` / `order.py::cancel_order` / `risk.py::check` / `executor.py::dispatch_open|close` 在真实代码中不存在——`multi/` 下无 `order.py`/`risk.py`，开仓分发在 `dispatch.py`、下单动作散在 `executor.py`。v1.2 对齐现实并补权限闸门一点。

1. `trader/multi/dispatch.py::try_dispatch` — TG 指令总入口，trace_id 发源地
2. `trader/multi/dispatch.py::_do_open` — 开仓分发
3. `trader/multi/dispatch.py::_do_close` — 平仓分发
4. `trader/multi/executor.py::open_market` — 市价开仓主力（REST 落地）
5. `trader/multi/executor.py::close_market` — 市价平仓
6. `trader/multi/permissions.py::check` — 权限闸门（role × action × account）

**延伸挂点（不在初始批内，新增必须乌鸦明批）**：`executor.open_limit` / `open_liq` / `add_to_position` / `cancel_order` / `cancel_all` / `place_stop_loss` / `place_take_profit`。

**未来目标**：若建立 `multi/order.py` + `multi/risk.py`，挂点迁移到 `place_order` / `risk.check`。当前以上表 6 个点为准。

### 8.2 装饰器行为
- 函数入口落一条 `trace/module_called.jsonl`
- `target` = 函数短名（如 `dispatch_open`）
- `payload.function` = 完整点分路径
- `payload.args_summary` = 前 3 个位置参数 + 最多 3 个关键字参数（调用 `ledger.scrub` 脱敏）
- `payload.caller` = 上一层栈帧的模块路径
- **不记返回值**（可能含敏感信息）
- 落盘失败不阻塞函数执行

### 8.3 扩展原则
- 不得擅自给更多函数挂装饰器
- 新增每一个点都要乌鸦明批 + 估算调用频次
- 高频函数（>10 次/秒）必须先提采样策略

---

## 9. JSONL 切分策略

### 9.1 RotatingFileHandler
| 域 | maxBytes | backupCount |
|---|---|---|
| exec | 15 MB | 10 |
| signal | 10 MB | 5 |
| risk | 10 MB | 5 |
| system | 10 MB | 5 |
| dialog | 10 MB | 5 |
| external | 10 MB | 5 |
| trace | 20 MB | 10（高频） |

### 9.2 归档命名
`orders.jsonl` → `orders.jsonl.1` → `orders.jsonl.2` → ...

### 9.3 永不自动删除
轮转到 `backupCount` 份后最老一份被覆盖——**这是唯一的丢失点**。若需要长期保留：
- 由运维手动打包归档到冷存储
- 未来可加 gzip 压缩归档（当前不做）

**理由**：乌鸦要"改 A 忘 B"回归对比可能跨月。默认 backupCount 保留上限足够覆盖 2~4 周常见需求。

---

## 10. Phase A 历史兼容

### 10.1 旧 jsonl（2026-04-21 之前的数据）
- 可能含 `cost_usd: null` 字段位
- **读取代码忽略此字段**，不报错
- **不回溯清理**（保持归档原样）

### 10.2 `module` → `actor` 字段重命名
- Phase A core.py 旧版用 `module`（如 `exec.orders`）
- 新版输出 `actor`（如 `executor`）
- **查询工具要兼容两种字段**：读时 `e.get("actor") or e.get("module")`

### 10.3 external 域字段迁移
- Phase A 的 `log_external_call()` 带 `cost_usd=`、两态 `call`/`call_failed`
- 新版去掉成本，三态 `api_call_started`/`api_call_completed`/`api_call_failed`
- 老日志不迁移

### 10.4 timestamp 字段名
- Phase A 用 `ts`（沿用）
- 新文档字段表里 `timestamp` 指的就是 `ts`，二者等价
- **落盘统一用 `ts`**（短）

---

## 附录 · 快速查询 cheatsheet

```bash
# 按 trace_id 找完整因果链
jq 'select(.trace_id=="bc157eed")' /root/logs/**/*.jsonl

# 最近 1h failed
jq 'select(.result=="failed" and .ts > "2026-04-21T14:00:00+08:00")' /root/logs/**/*.jsonl

# 某规则命中次数
jq 'select(.event_type=="rule_triggered" and .payload.rule_id=="vol_ratio_gte_1_5")' /root/logs/signal/*.jsonl | wc -l

# 某币的所有 order 事件
jq 'select(.target=="BTCUSDT" and (.event_type | startswith("order_")))' /root/logs/exec/*.jsonl
```

更复杂的查询交给 `ledger/query.py`（第 2 步落地）。

---

## 变更记录

- 2026-04-21 v1.2 · §8.1 挂点对齐真实代码：dispatch._do_open/_do_close + executor.open_market/close_market + permissions.check，共 6 点
- 2026-04-21 v1.1 · result enum 扩至 7 态（+timeout/rate_limited）
- 2026-04-21 v1.0 · 乌鸦定方向为"动作账本不记成本"，起草首版
