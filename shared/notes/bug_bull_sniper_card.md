# 做多阻击信号卡片 Bug 清单（已关闭）

发现时间：2026-04-18 02:54
修复完成：2026-04-20 15:40（大猫 /crow-review 复审）
状态：✅ 全部修复

## Bug 1：结算全标"✅ 成功" → ✅ 已修

根因：早期 signal_history.status 被写死 success。
修复：`bull_trailing.py:_settle_signal` 按实际平仓原因分类（tp→success / sl→failed / expired→expired）。

## Bug 2：exit_price=0，-100% 假象 → ✅ 已修

根因：平仓后没写回真实成交均价。
修复：`_settle_signal` 入参为 mark price，结算时写入 exit_price 字段。

## Bug 3：列表硬截断 → ✅ 已修

根因：`notifier.py:437` 硬编码 `list(reversed(group))[:5]`。
修复：改读 `config.yaml` 的 `max_history_per_group`，默认 10。commit `430e4ff`。

## Bug 4：signal_history 语义混乱 → ✅ 自然解决

根因（历史）：同一张表既当信号评分日志又当交易结算。
解决方案：
- 真实结算走 `bull_trailing.py:_settle_signal` → is_virtual=False
- 虚拟结算走 `scanner.py:1189-1193` → is_virtual=True
- `notifier.py:send_status_card` 按 is_virtual 分 `real_hist` / `virt_hist` 两组独立渲染

无需再动 scanner.py 封板文件。

## 同期落地（430e4ff）

notifier.py 工程/效率 7 条：
- 3 处异常静默吞掉 → logger.warning
- chat_id 硬编码默认 → env 缺失时启动告警
- send_status_card 持仓 N+1 + 两路 API 串行 → 并行化
- route() 3 路 TG 推送串行 → 线程池并行
- 重复 _load_notify_cfg() 合并
