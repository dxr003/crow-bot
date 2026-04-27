# Bug: bull_trailing 无差别撤销 SL/TP

**发现时间**：2026-04-20 04:05（`/crow-review 扫交易策略` 首次试刀）
**修复时间**：2026-04-20 04:15
**严重度**：⭕ Bug #1（实盘可触发误伤）
**commit**：5200925

## 场景

bull_trailing 的 `_cancel_algo_orders(symbol)` 会拉币安 `/sapi/v1/algo/futures/openOrders` 返回的**所有** algo 单，过滤该 symbol 后**无差别撤销**。

触发点：
1. `_cancel_all_algo` 按 sl_id/tp_id 精确撤失败时，走 fallback 全撤
2. `check_positions` 超时平仓前（原第 486 行）直接无条件全撤

风险：如果你通过玄玄/其他渠道对同一币也挂了 SL/TP（比如手动持仓监控），会被 bull_trailing 连锅端——违反 `feedback_never_cancel_sl_tp` 的明确规矩。

## 修复

1. **删除** `_cancel_algo_orders` 函数（消除诱惑，没人再能误用）
2. **重写** `_cancel_all_algo`：
   - 只按 `sl_algo_id / tp_algo_id` 精确撤
   - 撤不掉只记日志
   - 绝不走全撤
3. 超时平仓路径（`check_positions` 4b）也走精确撤

## 权衡

**残单处理**：如果 bull_sniper 的 sl_algo_id/tp_algo_id 丢失（state 文件损坏或 bug），撤不掉的挂单留给币安自处理。宁可留残单，也不误撤用户的 SL/TP。

**生效**：需重启 `bull-sniper` 服务加载新代码。重启由乌鸦授权。

## 后续

同一次 review 发现的状态文件非原子写问题（Bug #2，5 文件全中）待后续处理。
