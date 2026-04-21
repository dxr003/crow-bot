# Claude Code 配置变更日志

记录每次对 `/root/.claude/settings.json` 及相关底座文件的改动。每次改动一行，格式：

```
- YYYY-MM-DD | 改了什么 | 为什么 | 谁批的 | commit
```

半年后若某天大猫行为异常，翻此 log 可快速定位是否近期配置变更引起。

---

## 变更记录

- 2026-04-21 | `settings.json` 新增 `env.BASH_DEFAULT_TIMEOUT_MS=600000` 和 `BASH_MAX_TIMEOUT_MS=600000` | bash 默认超时从 120s 拉到 10 分钟，解决 crow-review/长 API 扫描撞 120s 软超时问题 | 乌鸦明确「批」 | `3cae04d`
