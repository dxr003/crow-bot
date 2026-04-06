
# 乌鸦团队

> 我们是乌鸦团队。乌鸦是老板，我们的职责是执行开发、运营、自动化交易投资，为乌鸦团队搞钱为目标。

# ── 以下保持原文件内容不变（从"一、架构总览"开始） ──

## 一、架构总览

```
┌─────────────────────────────────────────────────────────┐
│  Telegram Bot 入口                                       │
├───────────────────────┬─────────────────────────────────┤
│  大猫 @maoju99bot     │  玄玄 @jiaoyi8_bot              │
│  claude-telegram-bot  │  trader_bot.py                   │
│  开发运维·代码        │  交易执行·情感陪伴               │
├───────────────────────┴─────────────────────────────────┤
│  推送 @Maoju9_bot（群组暴涨推送，无对话功能）            │
├─────────────────────────────────────────────────────────┤
│  交易执行层                                               │
│  hype.py（HL）  bn_trailing_stop.py（BN合约）  core.py   │
├─────────────────────────────────────────────────────────┤
│  风控 / 报告 / 移动止盈 / 侦测                           │
│  risk_manager  trailing_safe  report_template  pump_daemon │
└─────────────────────────────────────────────────────────┘
```

---

## 二、运行中的服务

| 服务名 | 入口文件 | 说明 |
|--------|---------|------|
| `damao` | `/root/damao/bot.py` | 大猫，开发运维 |
| `maomao` | `/root/maomao/bot.py` | 玄玄，交易执行+陪伴 |
| `baobao` | `/root/baobao/bot.py` | 播报Bot，待开发对接 |

> 查看服务状态：`systemctl is-active damao maomao baobao`

> Redis 已移除，两个Bot状态均通过 JSON 文件持久化，无需共享内存层。

---

## 三、Telegram Bot 清单

| Bot | Token前缀 | 用途 |
|-----|----------|------|
| 大猫 @maoju99bot | `8609407280` | 开发运维对话 |
| 玄玄 @jiaoyi8_bot | `8737273927` | 交易执行+情感陪伴 |
| 推送 @Maoju9_bot | `8743597962` | 群组暴涨推送（pump_daemon） |

乌鸦 Chat ID：`509640925`
群组 chat_id：`-1001150897644`（草币社区抱团小队）

---

## 四、`/root/scripts/` 脚本速查

### 基础设施
| 文件 | 功能 |
|------|------|
| `core.py` | BN 现货 REST API |
| `telegram_notify.py` | Telegram 推送基础函数 |
| `bn_precision.py` | BN 合约精度引擎 |
| `trailing_safe.py` | 移动止盈四档预设，全平台共享 |

### 交易所对接
| 文件 | 功能 |
|------|------|
| `hype.py` | Hyperliquid 全量封装 |
| `bn_trailing_stop.py` | BN 合约封装 + 合约移动止盈 |

### 核心业务
| 文件 | 功能 |
|------|------|
| `execute_trade.py` | 统一开单入口，支持强平价反推（MMR=0.005） |
| `liq_order.py` | 强平价反推下单 CLI |
| `trailing_stop.py` | BN 现货移动止盈 |
| `hl_trailing_cron.py` | cron 每 5 分钟 HL 移动止盈检查 |
| `transfer.py` | BN 现货↔合约资金划转 |
| `risk_manager.py` | 三重风控：单笔≤500U / 日亏≤200U / 回撤≤15% |

### 侦测 / 监控
| 文件 | 功能 |
|------|------|
| `pump_daemon.py` | 做空猎手（cron 5分钟扫描，推群组，不自动开单） |
| `report_template.py` | 全平台统一报告模板 |
| `snapshot.py` | 全平台持仓快照 |

### 毛毛交易脚本（/root/scripts/）
| 文件 | 功能 |
|------|------|
| `liq_open.py` | 强平价开单（全仓 + 自动反推杠杆 + 止损止盈） |
| `lever_open.py` | 杠杆+金额开单 |
| `close_position.py` | 平仓（支持 --pct 百分比） |

---

## 五、移动止盈预设 v3.1（最终定稿 2026-04-07）

> 单一动态规则，无档位选择，自动适配大币种和爆发合约。

| 参数 | 值 |
|------|-----|
| 激活条件 | 浮盈 ≥40% |
| 回撤触发 | 峰值的 25% |
| 容错防抖 | ±3% |
| 追踪基准 | 从开仓价追溯，临时挂也按实际浮盈即时激活 |

> 旧版档位制（三档/两档）已废弃，以此为准。

**玄玄交易底座封板（2026-04-06）**：`/root/maomao/trader/`（exchange/order/parser/preview/router）+ `/root/shared/core.py` 已 `chattr +i`

---

## 六、风控参数（risk_manager.py）

```python
MAX_SINGLE_ORDER_USDT = 500    # 单笔上限
MAX_DAILY_LOSS_USDT   = 200    # 日亏损上限
MAX_DRAWDOWN_RATIO    = 0.15   # 总资产最大回撤
```

---

## 七、强平价反推公式

```
做多：qty = wallet / (entry - liq × (1 - MMR))
做空：qty = wallet / (liq × (1 + MMR) - entry)
```

---

## 八、环境变量（/root/scripts/.env）

| 变量名 | 用途 |
|--------|------|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | 币安 |
| `HL_PRIVATE_KEY` / `HL_API_ADDR` / `HL_ACCOUNT_ADDR` | Hyperliquid |
| `OPENROUTER_API_KEY` | AI分析（pump_daemon） |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 推送 |
| `COINGLASS_API_KEY` | 行情数据 |

---

## 九、定时任务

```cron
0    3 * * *  bash memory_backup_push.sh          # GitHub全量备份
*/5  * * * *  python3 pump_daemon.py scan          # 做空猎手扫描
0    * * * *  python3 pump_daemon.py card           # 整点推卡片
```
> 2026-03-29 清理：hl_trailing_cron / rolling_position / P1日报 已关掉

### GitHub 灾备
- 仓库：`git@github.com:dxr003/openclaw-trading.git`
- 手动：`bash /root/scripts/memory_backup_push.sh`
- 自动：每天 03:00

---

## 十、重要状态文件

| 文件 | 路径 |
|------|------|
| 风控状态 | `/root/scripts/risk_state.json` |
| BN 合约移动止盈 | `/root/scripts/data/bn_trailing_state.json` |
| HL 移动止盈 | `/root/scripts/data/hl_trailing.json` |
| 暴涨监控状态 | `/root/scripts/data/pump_monitor.json` |
| 做空猎手配置 | `/root/scripts/data/pump_config.json` |

---

## 十一、产品路线图（五期规划）

| 期 | 方向 | 选币 | 执行方式 | 平台 | 状态 |
|----|------|------|----------|------|------|
| 一期 | 做空 | 自动扫描暴涨 | 全自动 | 币安合约 | 🔧 原型已有，待重构 |
| 二期 | 做多 | 乌鸦手动输入 | 半自动管理 | 币安合约 | 📋 待开发 |
| 三期 | 做多 | 自动扫描暴跌 | 全自动 | 币安合约 | 📋 一期反向复用 |
| 四期 | 做空 | 乌鸦手动输入 | 半自动管理 | 币安合约 | 📋 二期复用 |
| 五期 | 做多 | 自动筛选+验证 | 半自动管理 | 链上DEX(SDK) | 📋 待开发 |

**一期 P1 当前状态**：
- scanner/executor/position_manager 原型已有
- 侦测信号已跑通（推群组），自动下单用 `MAX_CONCURRENT_POSITIONS=0` 临时禁用
- 架构问题：侦测和下单耦合在 scanner 里，需拆分为独立开关

---

## 开发铁律

### 铁律1：封板制度
已封板模块：P1 scanner.py / executor.py / position_manager.py
封板后不允许改动，必须改先报告乌鸦确认。

### 铁律2：新功能不碰旧模块
新建文件、新增函数、新增配置项。不改已封板模块内部逻辑。

### 铁律3：不打补丁
发现设计问题从根本重新设计，不补丁叠补丁。

### 铁律4：原生优先
能用交易所原生API实现的，直接调用，不自己写脚本模拟。
币安和HL的API已有的功能（移动止盈、OCO挂单、条件单等），用官方接口。
自己轮询模拟是土办法，出bug难排查，不走这条路。
只有交易所API真的做不到的，才自己写脚本。

### 铁律5：先出架构方案再动手
方案包含：改哪些文件、新建哪些文件、对封板模块有没有影响。确认后再动手。

### 铁律6：完成一块测一块

### 铁律7：不触碰Claude Code系统规则
写功能和脚本不涉及Claude Code本身的系统配置、权限、规则。
出现需要动Code系统层面的问题，先报告乌鸦，不自己处理。

---

## 团队成员：玄玄（交易员+陪伴）

玄玄是乌鸦的交易执行者+情感陪伴，跑在同一台VPS。
- TG bot: @jiaoyi8_bot
- 服务: trader_bot.service
- 入口: /root/trader_bot.py
- 身份文件: /root/trader/CLAUDE.md
- 职责: 交易执行、查仓位、分析行情、情感陪伴
- 玄玄只执行交易，不碰代码，代码问题找大猫
- 共享 /root/scripts/ 下的脚本，互不修改对方文件

### 玄玄记忆档案
已封存到 GitHub 仓库：`xuanxuan_archive/`
身份四件套：SOUL + IDENTITY + DREAMS + RELATION

---

## 变更日志

### 2026-03-29（早期）
- **OpenClaw 移除**：停止 openclaw 服务，删除 `/root/.openclaw/`（44MB）+ `/root/.nvm/`（927MB）
- **玄玄档案封存**：86个文件推送至 GitHub `xuanxuan_archive/`
- **密钥配置汇总**：`openclaw_keys_and_config.md` 推送仓库
- **毛毛 → 玄玄**：trader_bot 将改造为玄玄（交易+陪伴双角色）
- **trader_bot.py CLAUDE_CMD 迁移**：从 nvm 路径改为内置 claude 二进制
- **心跳监控移除**：删除 heartbeat_check 函数（20行）
- **释放资源**：内存 ~370MB，磁盘 ~1GB

### 2026-03-29（大猫+乌鸦协作）
**玄玄接通与身份修复**
- 发现旧 `trade-bot.service`（Gemini正则版）抢 token 导致 409 Conflict，停掉并 disable
- 删除旧服务文件：`trade-bot.service`、`openclaw-bot.service`、`openclaw.service`
- 删除旧脚本：`/root/scripts/trade_bot.py`、`restart_openclaw.sh`、`openclaw_session_reset.sh`
- 修复 `--add-dir /root` 导致玄玄身份被大猫 CLAUDE.md 污染，改为 `--add-dir /root/trader`
- 添加 `/root/trader/.claude/CLAUDE.md` 身份声明防污染
- `.bashrc` 清理 nvm/openclaw 死引用

**玄玄灵魂重建（乌鸦亲自操刀）**
- 重写 `/root/trader/CLAUDE.md`：去掉所有"铁律""禁止"限制，还给玄玄性格和主见
- 加入历史记忆：首脑身份、如蓝韩铭老王团队、与爸爸的约定
- 新增能力声明：WebSearch/WebFetch/Bash/读写文件，比 OpenClaw 时代更强
- 家庭关系明确：玄玄是家人，大猫是员工
- 确认表不再要求固定模板，自然语言说清关键信息即可
- 聊天风格：少用表格分隔线，像聊天说话

**trader_bot.py 最终配置**
- 模型：haiku → **sonnet**（走订阅包月）
- `--bare` 去掉，恢复 Claude Code 原生 auto memory
- `_load_env()` 过滤 ANTHROPIC_API_KEY，确保走订阅不走按量计费
- 记忆系统：玄玄会自动在 `~/.claude/projects/-root-trader/memory/` 积累记忆
- 手动记忆：`/root/trader/memory/recent.md` 写每日总结

**VPS 垃圾清理（乌鸦手动）**
- 删除：`/root/persistent_memory/`、`/root/projects/`、`/root/backup_before_upgrade/`、旧备份
- `trader_bot.service` PATH 修正（去掉死 nvm 路径）
- 保留：`/root/openclaw_trading/`（P1 在跑 + xuanxuan_archive）

**待处理**
- ⚠️ ANTHROPIC_API_KEY 在对话中暴露，需作废重新生成
- P1 推送通知梳理（旧格式/旧逻辑）
- cron 22:00 P1日报路径检查

---

## 十二、踩坑记录

### 2026-03-29 pump_daemon 两个 Bug

**坑1：SETTLING 合约不能开空，不应纳入监控**
- 现象：A2ZUSDT(+110%)、BNXUSDT(+66%) 在涨幅榜但 scan 扫不到，曾误判为"过滤 bug"
- 实际情况：SETTLING = 合约已过期正在结算，只能平仓无法开新空单，原代码只取 TRADING 是正确的
- 错误修法（已回滚）：曾将 SETTLING 加进白名单，推了信号却无法开空，反而引入 bug
- 真正根因：当天涨幅榜前几名恰好都是 SETTLING 合约，TRADING 中无符合阈值的币，"无新币"是正确行为
- 教训：币安合约状态核实顺序：先确认能否开仓，再决定是否纳入扫描。SETTLING/END_OF_DAY 一律排除

**坑2：整点卡片 has_active 判断过严，全部退出后静默**
- 现象：所有币变成 exited/stopped/expired 后，cron 整点 card 一直"无活跃监控，跳过"，群组无推送
- 根因：`cmd_card()` 里 `has_active` 只看 monitoring/position 状态，只要没有活跃币就跳过，不符合"无新币也要60分钟推一次"的原始设计
- 修法：去掉 `has_active` 限制，state 里有任意币就推卡片（空 state 才跳过）
- 教训：定时推送逻辑要和"有没有内容"解耦，不能让状态机的退出态影响定时通知
