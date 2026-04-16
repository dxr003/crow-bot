
# 乌鸦团队

> 我们是乌鸦团队。乌鸦是老板，我们的职责是执行开发、运营、自动化交易投资，为乌鸦团队搞钱为目标。

# ── 以下保持原文件内容不变（从"一、架构总览"开始） ──

## 一、架构总览

```
┌─────────────────────────────────────────────────────────┐
│  Telegram Bot 入口                                       │
├───────────────────────┬─────────────────────────────────┤
│  大猫 @maoju99bot     │  玄玄 @jiaoyi8_bot              │
│  开发运维·代码        │  交易执行·陪伴（乌鸦的女儿）     │
├───────────────────────┼─────────────────────────────────┤
│  天天 @CyberPunkPanda │  贝贝 🐶 @Maoju9_bot            │
│  AI交易员（震天响女友）│  播报员（乌鸦家的狗狗）          │
├───────────────────────┴─────────────────────────────────┤
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
| `maomao` | `/root/maomao/bot.py` | 玄玄，交易执行+陪伴（乌鸦的女儿） |
| `tiantian` | `/root/tiantian/bot.py` | 天天，AI交易员（震天响的女友） |
| `baobao` | `/root/baobao/bot.py` | 贝贝 🐶，播报员（乌鸦家的狗狗） |

> 查看服务状态：`systemctl is-active damao maomao baobao`

> Redis 已移除，两个Bot状态均通过 JSON 文件持久化，无需共享内存层。

---

## 三、Telegram Bot 清单

| Bot | Token前缀 | 用途 |
|-----|----------|------|
| 大猫 @maoju99bot | `8609407280` | 开发运维对话 |
| 玄玄 @jiaoyi8_bot | `8737273927` | 交易执行+陪伴（乌鸦的女儿） |
| 天天 @CyberPunkPandabot | — | AI交易员（震天响的女友） |
| 贝贝 🐶 @Maoju9_bot | `8743597962` | 播报员（做多阻击+涨幅列表） |

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
| `trailing_safe.py` | 移动止盈旧版（已废弃，以 v3.1 单一规则为准） |

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

**一期 P1（做空阻击）当前状态**：
- scanner/executor/position_manager 原型已有
- 侦测信号已跑通（推群组），自动下单用 `MAX_CONCURRENT_POSITIONS=0` 临时禁用
- 架构问题：侦测和下单耦合在 scanner 里，需拆分为独立开关

**做多阻击系统（Bull Sniper）**：
- 路径：`/root/maomao/trader/skills/bull_sniper/`
- 服务：`bull-sniper.service`（systemd）
- 当前阶段：观察期（mode=off，纯记录不下单）

触发机制（v2.0，单层）：
```
全市场30秒扫描 → 24h涨幅≥8%直接进观察池
  → 观察池每10秒刷新
  → 8-10%  第一阶段：新闻+下架公告 → 有利好直接推信号
  → 10-20% 第二阶段：综合打分+AI决策 → ≥30分推信号
  → >20%   退出（不追高）
  → <5%    退出（动力不足）
```

过滤条件：上线<30天 / 24h已涨>30% / 成交额<500万U / 距ATH跌<50%（未腰斩不碰）

文件结构：
| 文件 | 功能 |
|------|------|
| scanner.py | v2.0 扫描器，8%直接进池，无雷达/确认层 |
| analyzer.py | 两阶段分析+AI(Haiku)两个接入点 |
| notifier.py | 推送：信号卡/群组状态卡/私信健康报告 |
| buyer.py | 买入执行（当前占位，mode=off不下单） |
| config.yaml | 全部参数可调 |

AI接入（Claude Haiku）：
- 位置1：新闻情绪判断（fetch_news后，失败回退关键词匹配）
- 位置2：最终买入决策（评分≥30后，skip否决/buy或失败放行）

新闻源：Tavily主力（8家专业加密媒体）→ Google RSS备用 → CoinGecko
下架检测：币安 exchangeInfo 原生API（status != TRADING）

推送时间：做多整点XX:00 / 做空整点XX:01

---

## 策略反思·每次对话必做

> 乌鸦每次发起日常对话时触发。不是汇报，是一起想问题。

### 第一步：读数据

每次新对话开始，主动读取最近运营数据：

```bash
# 做多阻击实时状态（观察池/信号/仓位/冷却）
cat /root/maomao/trader/skills/bull_sniper/data/scanner_state.json

# 最近评分记录（看哪些币被评了什么分）
tail -20 /root/maomao/trader/skills/bull_sniper/data/score_history.jsonl

# 最近日志（进池/退出/信号/买入/止盈）
tail -100 /root/maomao/trader/skills/bull_sniper/logs/scanner.log

# 池退出追踪（被踢的币后来涨了没）
cat /root/maomao/trader/skills/bull_sniper/data/reject_tracker.json

# 最近代码变更
ls /root/changelog/
cat /root/changelog/$(ls /root/changelog/ | grep -v README | sort | tail -1)
```

### 第二步：找矛盾

带着以下问题审视数据：
- 哪些币被拦住了（冷却/黑名单/过滤），它们后来涨了还是跌了？
- 赚钱的单和亏钱的单，触发条件有什么差异？
- 有没有反复进池但始终没触发信号的币？为什么？
- 现有参数（评分权重/冷却时间/进池门槛）和实际结果有没有明显矛盾？
- reject_tracker 里被踢出的币，6小时峰值说明了什么？

### 第三步：发起讨论

不写报告，像聊天一样说出来：
- 用一句话说发现了什么
- 提出自己的疑问或判断
- 邀请乌鸦一起讨论，看逻辑要不要调整

**禁止**只说"系统运行正常"
**禁止**只列数字不给判断
**要说的是**"我发现XXX，这和我们的逻辑有点矛盾，你怎么看？"

目标：让每次聊天都有真实数据支撑，基于实际跑出来的结果优化策略，而不是凭空讨论。

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

### 玄玄记忆档案
已封存到 GitHub 仓库：`xuanxuan_archive/`

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

### 2026-04-08（乌鸦主导·底座 v4.3 升级）
**问题起因**：Anthropic 服务器故障（Claude Code Partial Outage），订阅模式529错误无法响应，紧急切换时暴露底座多个设计缺陷

**发现的问题**
- `claudecode_gen` 无超时机制，服务器故障时进程永久卡死
- `api_gen` 接裸API无工具能力，是傻瓜机器人，架构错误
- `subscription_gen` 是死代码从未被调用
- `/cc` 切换的是"代理"和"傻瓜机器人"之间，逻辑错误
- `--model` 参数未传入 `claudecode_gen`，`/model` 在订阅模式下无效

**架构决策（乌鸦确认）**
- 聊天永远走 Claude Code 代理，不存在傻瓜模式
- 订阅是主力，撞限后 Anthropic 自动扣费续用，不需要切换
- 读图走独立 ANTHROPIC_API_KEY，硬需求，场景少，单独保留
- `/cc` 命令永久删除

**本次修改（core.py v4.2 → v4.3）**
- 删除 `subscription_gen` 死代码
- 删除 `/cc` 模式切换命令
- `claudecode_gen` 加入 `--model` 参数，`/model` 现在真实生效
- `api_gen` 改名 `api_gen_image`，只用于读图
- 加入600秒超时保护 + 自动kill机制
- `mode.json` 简化，只保留 model 字段，去掉 mode 字段
- 文件封板 `chattr +i`

**其他操作**
- Claude Code 升级：2.1.90 → 2.1.96
- 清理冗余文件：备份文件、npm logs、`__pycache__`

**当前状态**：大猫 v4.3 ✅ / 玄玄 v4.3 ✅ / 模型切换验证通过 / 底座封板完成

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

---

## 十三、TOOL_ICONS 规范（core.py）

动作图标统一用彩色版本，当前设置：

| 工具 | 图标 |
|------|------|
| Bash | 🖥 |
| Read | 📘 |
| Write | 📒 |
| Edit | 📙 |
| Grep | 🔍 |
| Glob | 🗂 |
| WebSearch | 🌐 |
| WebFetch | 🌏 |
| Task | 🦾 |
| TodoWrite | 📌 |
| Skill | ✨ |

修改 core.py 时先解封 `chattr -i`，改完重新封板 `chattr +i`，然后重启 damao 和 maomao。

---

## 团队

- **乌鸦** — 老板，系统灵魂，交易决策
- **顾问** — 架构设计、代码编写、文档规划
- **大猫** — VPS部署运维，通过TG操作
- **玄玄** — 交易执行，乌鸦的女儿
- **天天** — AI交易员，震天响的女友
- **震天响** — 人类交易员
- **贝贝** — 播报员，乌鸦家的狗狗
