# Claude Code 配置变更日志

记录每次对 `/root/.claude/settings.json` 及相关底座文件的改动。每次改动一行，格式：

```
- YYYY-MM-DD | 改了什么 | 为什么 | 谁批的 | commit
```

半年后若某天大猫行为异常，翻此 log 可快速定位是否近期配置变更引起。

---

## 变更记录

- 2026-04-21 | `settings.json` 新增 `env.BASH_DEFAULT_TIMEOUT_MS=600000` 和 `BASH_MAX_TIMEOUT_MS=600000` | bash 默认超时从 120s 拉到 10 分钟，解决 crow-review/长 API 扫描撞 120s 软超时问题 | 乌鸦明确「批」 | `3cae04d`
- 2026-04-21 | `bot-backup/.git/hooks/pre-commit` 禁用（重命名 `.disabled`）| TG 刷屏，乌鸦要求「commit 日志不输出到贝贝，本地 git log 保留即可」 | 乌鸦明确「一起批准」 | 无 commit（.git 不进仓库）
- 2026-04-21 | `scanner.py:1329` 健康报告频率 1h → 4h（触发时刻 00/04/08/12/16/20 :02）| 贝贝每小时自检刷屏 | 乌鸦明确「一起批准」 | `3f6abdc`
- 2026-04-21 | 建 `/root/logs/` 跨域日志骨架（lib/logkit.py + lib/redact.py + 5 个子目录）| 日志系统重建 Phase A 基础设施，JSONL + ISO8601+08 + trace_id + 密钥脱敏 | 乌鸦「跨域共用 北京时间+800 abc顺序 开工」 | `43fd41f`
- 2026-04-21 | `multi/exec_log.py` 切到 logkit，写入 `/root/logs/exec/orders.jsonl` | Phase A 业务接入第 1 步，统一 schema、trace_id 可追溯；旧 `/root/maomao/data/exec_log.jsonl` 停写保留 | 乌鸦「继续干. 我批」 | `108ae6a`
