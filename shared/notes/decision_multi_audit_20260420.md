# 多账户架构审计 + 修复（2026-04-20）

扫描范围：`/root/maomao/trader/multi/` 全部（executor / guardian / permissions / registry / strategy_router / _self_test / __init__）
工具：升级版 `/crow-review` 带 Phase 0 ground truth
Ground truth：`CLAUDE.md`「多账户架构」 + memory `project_multi_account.md`

## 本轮已修

### ⭕ Bug（5 条）
1. `executor.py` `_is_hedge` 异常吞噬 → `logger.warning` 记录，不静默
2. `executor.py` `clear_hedge_cache` 无权限检查 → 加 `require(role, "admin", account or "币安1")`，签名从 `(account)` 改为 `(role, account=None)`
3. `guardian.py` `save_state` 普通 `write_text` → 换 `atomic_write_json`（新建 `multi/_atomic.py`），kill -9 不会留半文件
4. `guardian.py` `send_admin` 异常吞噬 → HTTP 非 200 / 异常 / token 缺失都记 `logger.warning/error`
5. `registry.py` / `permissions.py` 读 yaml/env 未指定 `encoding="utf-8"` → 全部显式指定，跨平台安全

### 🔥 效率（2 条）
1. `guardian.py` `check_accounts` 4 账户串行 → `ThreadPoolExecutor` 并行，4 账户从 1.5~2s 降到 ~0.5s
2. `executor.py` `get_full_balance` 合约+现货+资金 3 路 REST 串行 → `ThreadPoolExecutor` 并行，从 ~1.2s 降到 ~0.4s

### 🧹 清洁
无（保持改动最小）

## 跳过未改（两项）

### Agent #5 误判（close_market 的 reduceOnly）
报告说"hedge mode 下 `reduceOnly` 和 `positionSide` 可共存"。
**实际币安规则**：hedge 模式 `positionSide=LONG/SHORT` 已隐含 reduce 语义，多一个 `reduceOnly=true` 会被拒（-4013 / -4061 类错误）。
现有代码是对的：`if hedge: positionSide` / `else: reduceOnly=True` 互斥，保持。

### Agent #6 实机已验证（place_stop_loss / place_take_profit）
报告说"hedge 模式下 `closePosition=true` + `positionSide` 同时传会被拒"。
**实机证据**（scanner.log 2026-04-19）：
```
07:14:28 [币安2] BIOUSDT 止损 @ 0.02928 algoId:1000001393664331
07:19:03 [币安2] 币安人生USDT 止损 @ 0.43172 algoId:1000001393692789
```
bull_sniper buyer.py 实际 payload 带 `positionSide:"LONG"` + `closePosition:"true"`，币安多次返回 200+algoId 成功，未被拒。
**结论**：Agent #6 误判，当前 `multi/executor.py` 的 `place_stop_loss/place_take_profit` 写法正确，无需改。

## 状态

- 文件均未封板，直接在 `/root/maomao/trader/multi/` 修改
- py_compile + import 通过
- **未重启服务**：guardian 目前无 cron（CLAUDE.md 十三节），executor 是库不是进程；下次 bot 重启自然加载新代码
- 已同步 `/root/bot-backup/maomao/trader/multi/`，git commit + push

## 架构观察（遗留，未本轮修）

- `registry.get_credentials` 未对 API key 做脱敏日志（目前未 log 即可，但以后加 log 要注意）
- `strategy_router.py` 暂未审（本轮未覆盖，薄胶水文件，下轮再扫）
- `_self_test.py` 结构 OK，保留
