# decisions.log 格式说明

## 当前统一格式（runner.py v2.0+）

```
YYYY-MM-DD HH:MM | $价格 | 区段名 | 动作类型 | 原因/参数
YYYY-MM-DD HH:MM | DECISION | 优先级层 | 执行动作 | 参数
```

每次区段变化产生两行：第一行是 make_decision 输出，第二行是优先级链执行结果。

## 三种核心动作示例

```
# WAIT（等待，无操作）
2026-04-24 19:15 | $78,162 | near_shore | REDUCE_50 | 减仓 $50（50%，总仓 $100）
2026-04-24 19:15 | DECISION | WAIT | NO_ACTION

# OPEN/ADD（加仓，JC1）
2026-04-25 10:00 | $76,000 | small_box_mid | ADD_30 | 价格回调至小箱中轴
2026-04-25 10:00 | DECISION | JC1 | ADD_30 | usd=30.0

# REDUCE（减仓，JS1 + 可能触发 TRAIL_STOP）
2026-04-25 14:00 | $79,500 | near_shore | REDUCE_50 | 趋势减弱
2026-04-25 14:00 | DECISION | JS1 | REDUCE_50 | usd=35.0
2026-04-25 14:00 | DECISION | TRAIL_STOP | trail_stop|peak=79800|pullback=0.8%|sell=9.0U
```

## 优先级层说明

| 层 | 含义 |
|----|------|
| FK5 | 破箱强平，最高优先级 |
| BUYBACK | 买回平空 |
| JS1 | 减仓（含 TRAIL_STOP 子检查） |
| JC1 | 加仓 |
| WAIT | 等待，无操作 |

## 注意

decisions.log 早期（2026-04-22 前）有 JSON 格式记录，属历史数据，忽略即可。
从 2026-04-24 起统一为 pipe 格式。
