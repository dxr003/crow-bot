# Bull Sniper 原子写 + change_leverage 容错修复（2026-04-20）

## 背景
`/crow-review 扫交易策略` 命中的 Bug #2（非原子写，kill -9/断电残留半截 JSON）和 Bug #3（change_leverage 异常时未拦截，仍往下走下单/挂 SL）。

## 修复范围
- 新增 `bull_sniper/_atomic.py`：`atomic_write_json(path, data, indent=...)` 写 `.tmp` + `os.replace`。
- 7 处状态落盘改走 `atomic_write_json`：
  - `scanner.py` `save_state`
  - `buyer.py` `_register_trailing`（`TRAILING_STATE`）
  - `bull_trailing.py` `_save`
  - `trailing_limit.py` `_save`
  - `reject_tracker.py` `_save`
  - `chain_score.py` `_save_holders_snapshot` / `_save_alpha_cache`
- `buyer.py` `_execute_auto` 把 `c.change_leverage(...)` 包 try/except：失败直接返回 `status=skipped`，附 `reason="设置杠杆失败"`，避免用默认/旧杠杆下单。

## 封板变化
解封再改再封的文件：scanner / buyer / bull_trailing / trailing_limit / chain_score（原本 `chattr +i`，改完重新 +i）。
`_atomic.py` 新建后 +i。
`reject_tracker.py` 原本未封板，维持未封。

## 影响
- 原子写只改落盘路径，读/其它逻辑零变动；旧文件继续可读。
- change_leverage 失败由"静默异常外抛被外层 execute 捕获变 error"升级为"显式 skipped + 原因"；观察日志 `[buyer] ... change_leverage 失败`。
- 仓位数/黑名单/信号峰值等内存状态不受影响。

## 验证
- `py_compile` 7 个文件通过。
- `sys.path` 导入全部模块 OK。
- `atomic_write_json` 单元：普通 dict / `indent=None` / 不残留 `.tmp` 均通过。

## 后续
重启 bull-sniper 服务生效，需乌鸦批准（feedback_restart_notify 要求不静默重启）。
