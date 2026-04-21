# Phase A · 第 2 步完工报告

**时间**：2026-04-21 15:31+08
**范围**：存量 cost_usd 清理 + core.py 签名改造 + external.py 三态重构
**状态**：等乌鸦审

---

## 1. 触达文件（3 个）

| 文件 | 改动 | 行数 |
|---|---|---|
| `/root/ledger/core.py` | schema 升级：+actor / +target / +result / +error_msg / +related_files / -cost_usd；DOMAINS 扩 risk/trace；容量表按 §9.1 | ~178 行 diff |
| `/root/ledger/external.py` | 完全重写：3 态函数 api_call_started / _completed / _failed；删 cost_usd/credit_cost；强制从 ContextVar 继承 trace_id | ~202 行 diff |
| `/root/ledger/__init__.py` | 导出从 `log_external_call` 换为 3 个新函数 | ~54 行 diff |

完整 diff 见 `phase_a_step2_diff_2026-04-21.diff`

## 2. 旧文件归档（不清理）

- `/root/logs/external/api_calls.jsonl` → `api_calls.jsonl.1`（1 条 2026-04-21T14:48 的旧 schema 数据，含 cost_usd 和 module 字段，按 conventions §10.1 保留）

## 3. 写入测试结果

### 3.1 测试脚本

构造 3 个事件写入：
1. `order_placed` · actor/target/result/pending 齐
2. `order_failed` · 含 error_msg + related_files（failed 必填）
3. external 三态（api_call_started → completed → failed）全走一遍

### 3.2 样本路径

| 文件 | 条数 | 事件 |
|---|---|---|
| `/root/logs/exec/orders.jsonl` | 末 2 条 | order_placed（trace_id=9a5a9bcb result=pending）/ order_failed（同 tid result=failed） |
| `/root/logs/external/api_calls.jsonl` | 5 条 | 两次 trace（1bbe3102/9a5a9bcb）各含 started/completed，最后一条 failed+timeout |

### 3.3 字段核查清单

新 schema 必选字段（8 项）全部落盘：ts / trace_id / level / actor / event_type / target / result / payload ✓
可选字段按需出现：error_msg（failed）/ related_files（failed）✓
禁用字段已清除：cost_usd / module / provider / model / token_usage ✓（旧 Phase A 字段全没写入新 jsonl）

### 3.4 ContextVar 继承验证

`log_api_call_*()` 系列未显式传 trace_id，但落盘 trace_id = `9a5a9bcb`（上一个 `set_trace_id()` 设置），说明 external 从 ContextVar 自动继承生效。

## 4. 发现的约束冲突（待乌鸦拍板）

**⚠️ `result` 枚举范围**

- conventions.md §4.1 定 result enum：`success / failed / partial / pending / n-a`（5 态）
- 乌鸦第 2 步指令：external 强化 4 态 `success / failed / timeout / rate_limited`
- `timeout` / `rate_limited` 在 conventions 里不存在

**我的处理**（偏向跑通、等你明批）
- `core.py` 的 `VALID_RESULTS` 临时加入 `timeout / rate_limited`，代码跑通
- `external.py` 的 `log_api_call_failed` 校验只允许 `failed / timeout / rate_limited`
- **未改 conventions.md**（按"改 conventions 先停手问"原则）

**你选一条**：
- A. conventions §4.1 补 `timeout / rate_limited` 进通用 enum → 我改文档
- B. external.py 的 result 回归 5 态，把 timeout/rate_limited 放进 payload.failure_type → 我改代码
- C. 其他

### 4.1 我倾向 A，理由

`timeout` / `rate_limited` 本质是 `failed` 的**跨域通用细分**：
- exec 下单也会 timeout（币安 REST 10s 无响应）
- risk 检查调外部也会 rate_limited
- dialog 调 bot API 也会 timeout

一次扩 conventions 到位，未来各域复用，不用反复谈。

### 4.2 选 A 副作用

- `conventions.md §4.1` 的 result enum 表要补两态（加 1 行）
- 附录 cheatsheet 保持不变（已用 `result=="failed"` 通配，新增两态等价于扩大 failed 大类）
- 查询工具 `query.py`（第 2 步后落地）要识别 7 态，逻辑零复杂度增加
- **语义不模糊**：timeout/rate_limited 都是明确的 failed 子类，不会被当成 success/partial

### 4.3 选 B 副作用

- `grep result="failed"` 混所有失败原因（网络超时 / 限流 / 业务逻辑错），**可观测性下降**
- external 查询要复合条件：`result=="failed" && payload.failure_type=="timeout"`，两层访问
- 未来 exec / risk 遇到相同需求，还要在 payload 里再引入 `failure_type` 字段一次——**本质是变相 schema 扩展，但散在各 payload 里而非 conventions**
- 维护分散，不如一次扩 enum 干净

## 5. 未动的清单

- scanner.py / dispatch.py / guardian.py / exec_log.py 的 trace_id 机制 — 按约束保留
- `@log_call` 装饰器 — 留第 3 步
- 27 事件接入业务代码 — 留第 3 步
- 老 orders.jsonl / bull_sniper.jsonl / commands.jsonl / guardian.jsonl 等 Phase A 产出 — 原地不动（含 cost_usd:null 字段位），按 §10 读时兼容

## 6. 回退方案

单个文件回退：
```bash
cp /root/bot-backup/ledger/core.py /root/ledger/core.py
cp /root/bot-backup/ledger/external.py /root/ledger/external.py
cp /root/bot-backup/ledger/__init__.py /root/ledger/__init__.py
mv /root/logs/external/api_calls.jsonl.1 /root/logs/external/api_calls.jsonl  # 如需恢复
```

## 7. 下一步

**不动**，等"第 2 步 PASS"再推第 3 步（业务代码接入 + `@log_call` 金路径 5 个挂点 + related_files 栈追踪）。
