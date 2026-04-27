# 日志系统现状审计 · 2026-04-21

> 纯侦查文档，不改代码不新建目录。扫描 `/root/maomao` `/root/damao` `/root/tiantian` `/root/baobao` `/root/scripts` `/root/short_attack` 下所有 `*.log` / `*.jsonl` / 流式写入的 `*.json`。
> 产出目的：为后续"日志作为独立神经系统"改造提供可核对的起点。

---

## 1. 日志文件清单（全覆盖）

| 路径 | 产生进程 | 写入频率 | 当前大小 | 最后写入时间 | 轮转规则 |
|------|---------|---------|---------|-------------|---------|
| `/root/maomao/data/exec_log.jsonl` | maomao.service（trader.multi.executor 全动作装饰器） | 每次 executor 公开方法调用（含 guardian 巡检） | 42 KB / 134 行 | 04-21 13:30 | 代码内建：5MB 切片，保留最多 3 份（.jsonl.1/.2/.3） |
| `/root/maomao/data/guardian.log` | cron `*/10 * * * *` → `python3 -m trader.multi.guardian` | 每 10 分钟追加一块（多行文本报告） | 69 KB / 3294 行 | 04-21 14:00 | **无轮转** |
| `/root/maomao/data/guardian_state.json` | guardian.py（同上 cron） | 每 10 分钟全量覆写 | 1.1 KB | 04-21 14:00 | 覆写式，非增长 |
| `/root/maomao/data/sys_log.json` | damao.service 内定时 5 min 一次 | 每 5 分钟追加一条 JSON 对象到数组 | 94 KB | 04-21 14:00 | **无轮转**（整个数组读-改-写） |
| `/root/maomao/data/bot_log.json` | maomao.service / damao.service（online/offline 事件） | 服务启停时 | 7.9 KB | 04-21 13:40 | **无轮转** |
| `/root/maomao/data/trade_log.json` | maomao 指令收到后 | 每次派发交易指令（当前含 close 等） | 714 B | 04-21 01:15 | **无轮转**（列表增长） |
| `/root/maomao/data/session.json` | damao / maomao session 切换 | 偶发（session 切换时） | 54 B | 04-08 | 覆写 |
| `/root/maomao/data/mode.json` | `/model` 命令切换 | 偶发 | 28 B | 04-17 | 覆写 |
| `/root/maomao/data/trailing_state.json` | trader.trailing 移动止盈状态 | 有活跃监控时 | 2 B（空列表） | 04-13 04:00 | 覆写 |
| `/root/maomao/logs/bot.log` | maomao.service（python logging root） | 每次 TG getUpdates/sendMessage（约 10 s 一条） | 24 MB / 16.3 万行 | 04-21 13:59 | **无轮转**，持续膨胀 |
| `/root/maomao/logs/trailing.log` | cron `*/1 * * * *` → trailing_cron.py（stdout 重定向） | 每分钟 3 行固定模板 | 2.8 MB / 5.5 万行 | 04-21 13:59 | **无轮转** |
| `/root/maomao/logs/rolling.log` | cron `*/2 * * * *` → rolling_cron.py | 每 2 分钟（当前已不活跃，04-13 后无新增） | 19 KB | 04-13 04:00 | **无轮转** |
| `/root/maomao/logs/status.log` | maomao 内部"多账户状态推送" | 每小时整点 | 7.7 KB | 04-18 03:55 | **无轮转**，04-18 后停写 |
| `/root/maomao/trader/skills/bull_sniper/logs/scanner.log` | bull-sniper.service | 每 30 秒扫描+事件 | 6.2 MB / 5.4 万行 | 04-21 13:59 | **无轮转** |
| `/root/maomao/trader/skills/bull_sniper/data/score_history.jsonl` | bull-sniper 评分时 | 观察池币每次评分 | 283 KB / 972 行 | 04-21 13:36 | 手工切片（有 `.bak.<ts>` 副本） |
| `/root/maomao/trader/skills/bull_sniper/data/filter_log.jsonl` | bull-sniper 每轮扫描 | 每轮被过滤掉的币逐条写 | 1.25 MB / 1.14 万行 | 04-21 13:59 | **无轮转** |
| `/root/maomao/trader/skills/bull_sniper/data/scanner_state.json` | bull-sniper 每个 tick 覆写 | 每 10 秒全量覆写 | 9.6 KB | 04-21 13:58 | 覆写式（有 `.bak.<ts>`） |
| `/root/maomao/trader/skills/bull_sniper/data/reject_tracker.json` | bull-sniper 退出池时追加 history / tracking 数组 | 币从池退出时 | 3 KB | 04-21 13:49 | 覆写式（有 `.bak.<ts>`） |
| `/root/maomao/trader/skills/bull_sniper/data/alpha_cache.json` / `holders_snapshot.json` / `trailing_state.json` / `trailing_limit_state.json` | bull-sniper 各子模块缓存 | 低频覆写 | 1-17 KB | 04-19 ~ 04-21 | 覆写 |
| `/root/damao/logs/bot.log` | damao.service（python logging root） | 每次 TG getUpdates（约 10 s 一条） | 46 MB / 30.8 万行 | 04-21 13:59 | **无轮转**，**已超 40MB** |
| `/root/tiantian/logs/bot.log` | tiantian.service | 同上 | 11 MB / 7.5 万行 | 04-21 13:59 | **无轮转** |
| `/root/tiantian/logs/status.log` | tiantian 内部定时推送 | 04-18 后停写 | 3.6 KB | 04-18 03:55 | 无 |
| `/root/baobao/logs/bot.log` | baobao.service | 同上 | 23 MB / 16.3 万行 | 04-21 13:59 | **无轮转** |
| `/root/tiantian/.npm/_logs/*.log` | Claude Code npm CLI 调试 | 每次 claude 命令触发 | 20+ 文件 | 04-20 18:37 | npm 自带滚动 |
| `/root/tiantian/.claude/projects/-root/*.jsonl` | Claude Code 会话存档 | 天天每次 /ask 都写（模型对话） | 3 个 session jsonl | — | Claude Code 自带 |
| `/root/scripts/logs/daily_report.log` | cron（daily_trade_report.py） | 每天 0 点 | 3.4 KB | 04-18 00:01 | **无轮转** |
| `/root/short_attack/logs/short_attack.log` | 旧做空阻击（已停跑） | — | 726 KB | 04-13 20:40 | 无，**死日志** |
| `/root/short_attack/logs/cron.log` | 同上 | — | 726 KB | 04-13 20:40 | 无，**死日志** |
| `/root/short_attack/logs/card.log` | 同上 | — | 4.9 KB | 04-13 20:01 | 无，**死日志** |

> 说明：`journalctl -u {damao,maomao,tiantian,baobao,bull-sniper}` 是 systemd 的同源视图（进程 stdout/stderr），**不是文件日志**但**是目前唯一捕获服务启停/崩溃栈的信道**，值得在改造里一并规划。

---

## 2. 按功能分类

### A. 系统 / 运维

- `/root/maomao/data/guardian.log`（4 账户 API + 5 服务 health + 心跳，每 10 min）
- `/root/maomao/data/guardian_state.json`（最新一次结构化结果）
- `/root/maomao/data/sys_log.json`（大猫侧采集 load / mem / disk / 服务状态，每 5 min）
- `/root/maomao/data/bot_log.json`（玄玄/大猫 online/offline 事件）
- `journalctl -u *.service`（系统级 stdout/stderr，唯一能看到崩溃栈的地方）

### B. 交易执行

- `/root/maomao/data/exec_log.jsonl`（**核心**，全 executor 方法带 `@log_call` 装饰，含 role/account/action/args/result/ms/error）
- `/root/maomao/data/trade_log.json`（**遗留**，只记录 dispatch 层 close 等动作，字段少）
- `/root/maomao/logs/trailing.log`（cron 每分钟检查移动止盈触发）
- `/root/maomao/logs/rolling.log`（滚仓检查，04-13 后静默）
- `/root/maomao/trader/skills/bull_sniper/data/trailing_state.json` / `trailing_limit_state.json`（bull 内独立移动止盈状态，与 trader.trailing 平行，两套）

### C. 信号扫描

- `/root/maomao/trader/skills/bull_sniper/logs/scanner.log`（纯文本，扫描/进池/退出/信号/健康报告）
- `/root/maomao/trader/skills/bull_sniper/data/scanner_state.json`（观察池实时快照，10s 覆写）
- `/root/maomao/trader/skills/bull_sniper/data/score_history.jsonl`（逐次评分+分项 breakdown）
- `/root/maomao/trader/skills/bull_sniper/data/filter_log.jsonl`（每轮被过滤掉的币）
- `/root/maomao/trader/skills/bull_sniper/data/reject_tracker.json`（退出池后继续追踪 6h 的跟踪+历史）
- `/root/short_attack/logs/*`（**死日志**，04-13 起停跑；当前做空靠 pump_daemon cron + 群组推送，无独立文件日志）

### D. 机器人对话

- `/root/maomao/logs/bot.log` / `/root/damao/logs/bot.log` / `/root/tiantian/logs/bot.log` / `/root/baobao/logs/bot.log`
  - 全部是 python-telegram-bot + httpx 产生的 **HTTP 请求元日志**，**不含任何消息内容**（只有 URL+状态码）
  - damao 额外记 `[ERROR] Exception` 栈（jobqueue 回调抛异常等）
- `/root/tiantian/.claude/projects/-root/*.jsonl`（Claude Code 会话存档，实际记录了模型对话 I/O，但不是我们规划的业务日志）
- **业务侧没有"用户说了什么 / 机器人回复了什么"的对话流日志**

### E. 外部调用

- **完全没有文件日志**。币安 REST 调用只能通过 executor 装饰器间接看到"动作+耗时+错误码"，WebSocket 流、Tavily 新闻 API、OpenRouter AI 调用、Coinglass 行情、Google RSS 备用全部**不留痕**
- scanner.log 里偶见 `[分析结果]` 一行（analyzer 调 AI 的结论），但输入 prompt、原始响应、token 用量、成本都不记
- 下架公告（币安 exchangeInfo）、新闻情绪判断（AI+Tavily）的 **raw payload** 未存

---

## 3. 空白诊断（核心）

### A. 系统 / 运维

**A.1 完全没有日志的动作**
- systemd 重启/崩溃事件在文件日志里**完全不可见**（guardian.log 只说"服务 active"，不记 active→failed→active 的时刻与原因）
- 崩溃栈只在 `journalctl` 里（journal 默认保留期有限，且非业务视角）
- cron 任务执行结果（成功/失败/耗时）没有汇总——trailing_cron.py 每分钟 3 行固定文本，无法快速聚合"今天跑了几次、有几次失败"
- 磁盘/内存告警阈值：有数据（sys_log.json）但没有"触发告警"的动作日志

**A.2 有日志但字段不全**
- `guardian.log` 是**纯文本**多行报告拼接，每 10 分钟一块；要回溯"某时刻某账户 API 延迟"得靠 grep + 行号邻近关系，没有机读字段
- `sys_log.json` 记了 maomao/damao/baobao，**没记 tiantian 和 bull-sniper**（和 guardian_state.json 服务列表不一致）
- `bot_log.json` 的 detail 只是一句话（"正常退出" / "Claude Code代理 | opus-4-6"），异常退出没有 exit_code / signal

**A.3 有重复日志写在多个地方**
- 服务 active 状态：`guardian.log`、`guardian_state.json`、`sys_log.json` 各记一份，格式都不同
- online/offline 事件：`bot_log.json` 记，`journalctl` 也记，互不关联

**A.4 格式不统一难以检索**
- 纯文本（guardian.log）+ JSON 数组（sys_log.json）+ JSONL（exec_log.jsonl）三种格式混用
- 时间戳格式：`2026-04-19T16:04:16.575046+08:00`（guardian.log）/ `1776664910` epoch + `04-20 14:01:50` 手工串（sys_log.json）/ `2026-04-21 13:58:50,873` python logging（bot.log）— 三种并存

### B. 交易执行

**B.1 完全没有日志的动作**
- **信号 → 开单的来源链**完全不记：exec_log.jsonl 的 `open_market` 条目里**没有 signal_id / source / strategy 字段**（见 §4 原文），看不出这单是"玄玄人工开""天天人工开""bull_sniper 自动开"还是"pump_daemon 推荐"
- **止损/止盈挂单成功后的触发事件**没记：币安真的 triggered 了 STOP_MARKET 后，只有 position 消失，exec_log 里没有一条"被止损触发"的事件
- **风控拦截**：`risk.py` 还没开发，所以 "风控阻止了什么单" 根本没日志
- **手续费 / funding fee / PnL 结算**：只在 `daily_report.log` 里每天 0 点看到聚合结果，日中**没有每笔成交的 taker/maker、手续费、成交价**；这些得回头去币安 `/fapi/v1/userTrades` 拉，本地完全无存档

**B.2 有日志但字段不全**
- `exec_log.jsonl` 的 `open_market.result` 里有 `orderId / qty / price / leverage / margin / notional / hedge`，但**没有**：`margin_type`（CROSSED/ISOLATED）、`avgFillPrice`（实际成交均价，和下单 price 不同）、`cumQuote`（成交额）、`commission`（手续费）、`trade_id`、**signal_source**
- `trailing.log` 每分钟只写 "无触发 / === 检查完成 ==="，**触发时写得也简陋**：没记峰值、当前价、回撤百分比、该账户当时浮盈、撤单返回、最终平仓 orderId
- `rolling.log` 同样问题，并且从 04-13 起**完全停写**（服务未跑 or 无活跃滚仓）

**B.3 有重复日志写在多个地方**
- 玄玄手工 close 同时进 `trade_log.json`（dispatch 层）和 `exec_log.jsonl`（executor 层），但格式完全不同，很难对账
- bull_sniper 自己维护 `trailing_state.json`，trader.trailing 也维护 `/root/maomao/data/trailing_state.json`——两套移动止盈状态文件并存，互不感知

**B.4 格式不统一难以检索**
- `exec_log.jsonl` 用 epoch + `MM-DD HH:MM:SS`（**没有年份**，跨年会灾难）
- `trailing.log` 用 `YYYY-MM-DD HH:MM:SS,mmm`（python logging）
- `trade_log.json` 用 epoch + `MM-DD HH:MM:SS` —— 又少年份
- 所有时间戳**都是本地时间**（+08:00），没有一个是 UTC/ISO8601 完整格式

### C. 信号扫描

**C.1 完全没有日志的动作**
- **推送给谁、推送内容的原文**：scanner.log 只记 "[信号] GWEIUSDT 1h+11.3% 原因: score=28"，但**实际推到哪个 chat_id、消息 ID、Telegram 返回是否成功**一概不记
- **AI 决策 prompt / response 原文**：analyzer.py 调 Haiku 做新闻情绪判断和最终决策，**没有任何一条落盘**（token 用量、cost、fail 回退到关键词匹配的触发点都看不到）
- **新闻源原文**：Tavily / Google RSS / CoinGecko 抓到的原始标题、发布时间、来源 URL 都未存，只有 AI 处理后的一句话结论
- **下架公告获取**：币安 exchangeInfo 返回的原始字段未记录，只记"contract_TRADIFI_PERPETUAL / status_SETTLING"这种浓缩结论

**C.2 有日志但字段不全**
- `reject_tracker.json` 的 `breakdown` 字段是**字符串化的 dict**（`"{'C.买占比39%<51%闸门否': 0, 'E.Alpha+2...': 9}"`），不是真 JSON，机读难度大
- `score_history.jsonl` 有分项 breakdown 但**没记 price_at_score**（评分时的价格），退出后回看无法还原 "评 28 分时价格多少"
- `scanner.log` 的 `[信号]` 行没记 symbol 的 `entry_price / watchpool_entered_at / watchpool_duration`，得去 `scanner_state.json`（快照，早就变了）交叉查

**C.3 有重复日志写在多个地方**
- 单次评分同时写 `score_history.jsonl`（jsonl）和 `scanner.log`（纯文本），内容不完全一致（log 里简化）
- `reject_tracker.json` 的 history 数组和 scanner.log 的 "[退出池]" 行重叠

**C.4 格式不统一难以检索**
- `filter_log.jsonl` 用 ISO `2026-04-21T13:59:25`（**无时区**），`score_history.jsonl` 用 `2026-04-21 13:28:21`（空格分隔，同样无时区），reject_tracker 用 epoch，scanner.log 用 python logging — 一个子模块里四套时间格式

### D. 机器人对话

**D.1 完全没有日志的动作**
- **用户消息内容**：四个 bot.log 里**没有一条消息文本**，只有 `POST /getUpdates "HTTP/1.1 200 OK"`（全部 92% 是心跳）
- **机器人回复内容**：同上，只看到 `POST /sendMessage`，不知道回了什么
- **dispatch 命中/未命中**：`try_dispatch` 返回后没记 "这条是交易指令走了 executor" 还是 "闲聊走了 Claude"
- **Claude 调用的 prompt/response**：只记 `POST /v1/messages "HTTP/1.1 200 OK"`，token 用量、cost、模型切换都不记

**D.2 有日志但字段不全**
- bot.log 时间戳完整但**内容完全无业务价值**，16 万行 24 MB 几乎全是噪声
- damao/bot.log 里偶见 `[ERROR] Exception` 栈（例如 04-02 的 `object NoneType can't be used in 'await' expression`），但这些异常没有关联到当时在处理什么用户消息

**D.3 有重复日志写在多个地方**
- 每条 TG 消息会触发 `getUpdates`（被 bot.log 记一次）+ `sendMessage`（记一次）+ 有时 `editMessageText`（又一次）——噪声 **3 倍**
- Claude Code 自己在 `/root/tiantian/.claude/projects/` 下存 session jsonl（含完整对话历史），但**和 bot.log 是两个维度**，没有 chat_id ↔ claude_session 的绑定

**D.4 格式不统一难以检索**
- 四个 bot.log 全是 python logging 默认格式，但 baobao 用 `%H:%M:%S`（无毫秒），其他用 `%H:%M:%S,mmm`——解析器得兼容两种
- TG bot token **明文出现在每一行 URL 里**（`/bot8506007563:AAFWFB-EmlS9wD3EUOo_ROH68tkX6q0t9hs/`），**严重敏感泄漏**

### E. 外部调用

**E.1 完全没有日志的动作**
- 币安 REST：除了 executor 装饰器（只看到"方法名+结果"），**没有 HTTP 层日志**（URL、请求体、响应头、rate limit 剩余）
- 币安 WebSocket：完全无记录（websocket 断连、重连、订阅成功都不记）
- Tavily / Google News / CoinGecko 新闻抓取：完全无日志
- OpenRouter / Anthropic Haiku（analyzer.py 里 AI 决策）：完全无记录，**成本失控风险**
- Coinglass：同上

**E.2 有日志但字段不全**
- 仅 `exec_log.jsonl` 的币安错误会带上币安原始错误码（例 `(400, -4120, 'Order type not supported...')`），这是**唯一可用的外部错误痕迹**
- scanner.log 的 `[分析结果]` 只记 AI 结论字符串，没记调用耗时、输入 token、输出 token、成本

**E.3 有重复日志写在多个地方**
- HTTPX 的 `[INFO] HTTP Request` 同时出现在 4 个 bot.log + 部分 scanner 调用，全是噪声级重复

**E.4 格式不统一难以检索**
- 没有统一外部调用 wrapper，格式由各库自行决定（httpx、binance-sdk、requests 三种）

---

## 4. 单条日志字段审查

> 以下抽样为 04-21 真实日志，敏感字段已打码。

### A. 系统 / 运维

**A 样本**（`/root/maomao/data/guardian.log`，纯文本块）：
```
时间: 2026-04-19T16:04:16.575046+08:00

账户 API:
  ✅ 币安1      105ms
  ✅ 币安2      69ms
  ✅ 币安3      74ms
  ✅ 币安4      74ms

服务:
  ✅ maomao          active
  ✅ damao           active
  ✅ tiantian        active
  ✅ baobao          active
  ✅ bull-sniper     active

bull_sniper 心跳: ✅  最近 16:02:43（93秒前）

✅ 全部正常
```

**A 样本**（`/root/maomao/data/sys_log.json`，JSON 数组）：
```json
{
  "ts": 1776751222,
  "dt": "04-21 14:00:22",
  "load1": 0.65,
  "mem_pct": 80.2,
  "mem_used_mb": 2854,
  "services": {"maomao":"active","damao":"active","baobao":"active","redis":"inactive"}
}
```

**A 样本**（`/root/maomao/data/guardian_state.json`，结构化）：
```json
{"last_run":"2026-04-21T14:00:01+08:00","last_report":{"accounts":[{"account":"币安1","ok":true,"ms":359},...]}}
```

- 时间戳：guardian.log ISO+08:00 / sys_log.json epoch + `MM-DD HH:MM:SS`（无年无时区）/ guardian_state.json ISO — **三套**
- trace_id：**完全没有**
- 自解释性：guardian.log 块级易读但机读难；sys_log.json 可机读但缺 tiantian/bull-sniper；guardian_state.json 只有最新一次，无历史
- 敏感字段：无，OK

### B. 交易执行

**B 样本**（`/root/maomao/data/exec_log.jsonl`，04-21 03:24-03:27，SOL 开空+挂止损+平仓完整链）：
```json
{"ts":1776713089.3,"dt":"04-21 03:24:49","action":"open_market","role":"玄玄","account":"币安2","args":{"_role_arg":"玄玄","_acc_arg":"币安2","symbol":"SOLUSDT","side":"SELL","margin":15,"leverage":3,"margin_type":"CROSSED"},"symbol":"SOLUSDT","ms":438,"ok":true,"result":{"ok":true,"orderId":210053983746,"qty":0.52,"price":85.78,"side":"SELL","leverage":3,"margin":15,"notional":44.6056,"hedge":true}}
{"ts":1776713099.6,"dt":"04-21 03:24:59","action":"place_stop_loss","role":"玄玄","account":"币安2","args":{"_role_arg":"玄玄","_acc_arg":"币安2","symbol":"SOLUSDT","stop_price":90,"direction":"SHORT"},"symbol":"SOLUSDT","ms":306,"ok":false,"error":"(400, -4120, 'Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.', ...","result":{}}
{"ts":1776713226.0,"dt":"04-21 03:27:06","action":"close_market","role":"玄玄","account":"币安2","args":{"_role_arg":"玄玄","_acc_arg":"币安2","symbol":"SOLUSDT"},"symbol":"SOLUSDT","ms":611,"ok":true,"result":{"ok":true,"orderId":210054183327,"qty":0.52,"hedge":true,"closed":[{"direction":"空","qty":0.52,"orderId":210054183327}],"errors":null}}
```

**B 样本**（`/root/maomao/logs/trailing.log`）：
```
2026-04-21 13:59:02,108 [INFO] === 移动止盈检查 ===
2026-04-21 13:59:02,109 [INFO] 无触发
2026-04-21 13:59:02,109 [INFO] === 检查完成 ===
```

- 时间戳：exec_log 用 epoch + `MM-DD HH:MM:SS`（**缺年份**）；trailing.log 有年份但无时区
- trace_id / correlation_id：**完全没有**。上面 3 条 SOL 事件只能靠 `symbol + account + 时间邻近` 推断是同一笔生命周期——真实生产没法这么 fragile 地猜
- 自解释性：exec_log 单条尚可（role+action+args+result 齐备），但"这单的信号来源""这单服务于哪个策略"看不到
- 敏感字段：**args 里有 margin/leverage/stop_price 业务数据**（正常），**没有**泄漏 API key 或私钥 ✓

### C. 信号扫描

**C 样本**（`/root/maomao/trader/skills/bull_sniper/data/score_history.jsonl`）：
```json
{"time":"2026-04-21 13:28:21","symbol":"BLESSUSDT","action":"hold","score":13,"breakdown":{"B.涨幅中期+13.7%":8,"C.后2m买1万<20万闸门否":0,"D.OI上涨+1.6%":3,"E.Alpha+2":2},"change_1h":-0.89,"change_5m":-0.22,"change_1m":-0.31,"vol_ratio":1.71,"oi_change_pct":1.63}
```

**C 样本**（`/root/maomao/trader/skills/bull_sniper/logs/scanner.log`）：
```
2026-04-21 07:56:15,124 [INFO] [信号] GWEIUSDT 1h+11.3% 原因: score=28
2026-04-21 07:56:16,997 [INFO] [buyer] GWEIUSDT mode=alert, 推送等待人工确认
```

**C 样本**（`/root/maomao/trader/skills/bull_sniper/data/reject_tracker.json` 中一项）：
```json
{"symbol":"SIRENUSDT","reason":"timeout_24h","score":9,"breakdown":"{'C.买占比39%<51%闸门否': 0, 'E.Alpha+2 | 聪明钱持仓+1...': 9}","pool_entry_price":0.6859,"exit_price":0.7028,"peak_price":0.7098,"last_price":0.7041}
```

- 时间戳：score_history `YYYY-MM-DD HH:MM:SS`（无时区）；scanner.log 有毫秒；reject_tracker 用 epoch；filter_log 用 ISO 无时区 — **同一子模块四套**
- trace_id：无。GWEIUSDT 从进池 → 评分 → 信号推送无法通过 id 串起来，只能靠 symbol
- 自解释性：score_history 的 breakdown 是真 JSON 可用；reject_tracker 的 breakdown 是**字符串化 dict**，机读要 eval
- 敏感字段：无

### D. 机器人对话

**D 样本**（`/root/maomao/logs/bot.log`）：
```
2026-04-21 13:59:31,839 [INFO] HTTP Request: POST https://api.telegram.org/bot<TOKEN_REDACTED>/getUpdates "HTTP/1.1 200 OK"
```

**D 样本**（`/root/damao/logs/bot.log`，包含 4-02 异常）：
```
2026-04-02 14:29:36 [ERROR] Exception:
Traceback (most recent call last):
  File "/root/damao/venv/lib/python3.10/site-packages/telegram/ext/_jobqueue.py", line 1010, in _run
    await self.callback(context)
TypeError: object NoneType can't be used in 'await' expression
```

- 时间戳：有毫秒（除 baobao），本地时区无 +08:00 标记
- trace_id：无
- 自解释性：**零业务信息**。除了"有一次 HTTP 请求"外，回看无法得知用户说了什么、机器人回了什么
- 敏感字段：**TG bot token 明文出现在每一行 URL 里**。原文样本（做审计时脱敏）：`bot8506007563:AAFWFB-EmlS9wD3EUOo_ROH68tkX6q0t9hs` — **严重**

### E. 外部调用

**E 样本**（`/root/maomao/logs/bot.log` 里 Anthropic API 调用痕迹）：
```
2026-04-02 14:30:15 [INFO] HTTP Request: POST https://api.anthropic.com/v1/messages "HTTP/1.1 200 OK"
```

**E 样本**（`exec_log.jsonl` 里币安返回错误，**唯一能看到**的外部 raw error）：
```
"error":"(400, -4120, 'Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.', {'Date': 'Mon, 20 Apr 2026 19:24:59 GMT', ...})"
```

- 时间戳：httpx 默认；exec_log 内嵌 error 字段含币安返回的 Date 头（**正好是唯一的 UTC 时间戳**）
- trace_id：完全没有
- 自解释性：差。只知"Anthropic 被调了一次"，不知为哪个 chat、prompt 是什么、用了多少 token
- 敏感字段：**Anthropic URL 无 token（header 注入），这部分 OK**；但币安错误返回体里有 Date/Content-Length 头信息，本身不敏感

---

## 5. 回溯场景压力测试

### 场景 A：乌鸦问"今早 08:52-09:08 EDU 被拒 15 次，每次具体分项得多少分？"

**需要的日志**：bull_sniper 评分历史（分项）+ 拒绝时刻

**实际可用**：
- `score_history.jsonl` 有 `breakdown` 真 JSON 字段，可以 `grep EDUUSDT` 拉出所有评分
- `reject_tracker.json` 有退出时的 `breakdown`，但是**字符串化**，要 eval
- 问题：**08:52-09:08 这个时间段 score_history 里有没有 EDU 的条目？取决于"EDU 当时在不在观察池"**。如果 EDU 根本没进池（比如 24h 涨幅未到 8%），则 score_history 里一条都没有——"被拒 15 次"说法本身需要先确认在哪一层被拒（Z 前置筛选 / 观察池准入 / 评分不足 / buyer mode=alert 拦截）
- filter_log.jsonl 只记 exchange_info 过滤（SETTLING 等），不记评分拒绝

**结论**：⚠️ **部分能回答**。如果 EDU 进了池，score_history 能给精确分项；但"被拒 15 次"的计数没有专门的字段，得自己按 `action=hold & score<30` 聚合，且前置筛选层的拒绝（24h 涨幅不够、成交额不够、距 ATH 跌不够）**不写任何日志**。

### 场景 B：乌鸦问"2 小时前玄玄开的那单是哪个信号触发的，对应的 E-factor 数据是什么？"

**需要贯穿**：信号推送日志 → 玄玄对话日志 → executor 下单日志（必须有 trace_id）

**实际可用**：
- `exec_log.jsonl` 的 `open_market` 条目**没有 `signal_id` / `source` 字段**。04-21 03:24 的 SOL 空单只能看到 `role=玄玄 account=币安2 margin=15 leverage=3`，**看不出是人工决策还是某信号**
- `scanner.log` 的 `[信号] GWEIUSDT ... score=28` 和 `exec_log` 的 `open_market` **没有任何 id 关联**，只能靠 "symbol + 时间邻近 ± 几分钟" 猜
- 玄玄的对话日志（用户消息原文）——**不存在**（见 §3.D.1）
- E-factor 模块：scanner.py 里 `E.Alpha+2` 这种是评分的加成项（来自 alpha_cache.json），但 **E-factor 作为独立"链上/聪明钱"数据接入点目前还没上线**（见 `project_pending_modules.md`），所以问"E-factor 数据"=问未来模块

**结论**：❌ **完全答不上来**。缺三样：①exec_log 里没 signal_source 字段 ②信号推送与下单之间无 trace_id ③用户消息原文根本没存。

### 场景 C：大猫问"昨天夜里 guardian 告警重启过 maomao，重启前玄玄最后一个动作是什么？"

**需要**：guardian.log 告警时间 + systemctl 重启时间 + maomao 最后一条动作

**实际可用**：
- `guardian.log` 确实每 10 min 写一块"全部正常 / 发现异常"的报告；**但 guardian 本身不会主动 restart 服务**（代码里只是"推 ADMIN 告警"，不调 systemctl restart），所以"guardian 告警重启"这个前提可能根本不成立
- 如果重启是手工做的或 systemd 自动做的（失败重启策略），**文件日志里没有记录**，只能去 `journalctl -u maomao` 查 "Started / Stopped / Main process exited"
- maomao 重启前最后一条动作：`exec_log.jsonl` 有 epoch 时间戳可以对齐（精确到秒），但问题是 systemctl 重启时间也要从 journal 拿，**两侧对齐要人手操作**
- `bot_log.json` 记 online/offline 事件，但 detail 仅"正常退出" or "Claude Code代理"，**异常退出的原因 / exit code 不记**

**结论**：⚠️ **部分能回答**。exec_log 能给"最后动作"、journalctl 能给重启时刻，但①guardian 并不做 restart 决策 ②若 systemd 自动重启则文件日志完全缺席 ③两个时间源要手工对齐。**没有一条统一的"事件流"视图能把这三件事串成因果链**。

---

## 6. 业界参考

选取两个成熟框架，列出它们做了但我们没做的事。

### Freqtrade

1. **单独的 logging 配置段**：`log_config` 在主配置里独立一节，支持多 handler、多 formatter，不写代码就能换 json/文本/syslog。我们现在 `logging.basicConfig` 散在 4 个文件里，格式各自为政。
2. **RotatingFileHandler 开箱即用**：`--logfile` 启用自动轮转。我们 5 个主要日志（bot.log ×4 + scanner.log）全部**零轮转**，damao/bot.log 已经 46 MB。
3. **--trade-ids 作为一等公民**：查询/调试都可按 `trade_id` 过滤。我们 exec_log.jsonl 连基础的 `trade_id` 字段都没有，同币种多次开平就靠肉眼看时间戳。
4. **Telegram bot 独立 log handler**：bot 消息和业务日志隔离。我们 bot.log 里 99% 是 HTTP 心跳噪声，dropped-needle-in-haystack。

### Hummingbot

1. **`conf/hummingbot_logs.yml` 集中治理**：所有日志的 formatter/handler/level 在一个 YAML 里，用户改参数即改行为。我们 5 个配置散在 5 个 .py 里。
2. **每日切片 + 保留 N 天自动删除**：v0.17.0 起内建 daily rotation + 保留 7 天。我们的 short_attack 死日志停写一周还躺在那占 1.4 MB，scanner.log 从 04-09 开始累积不切。
3. **HummingbotLogger 统一基类**：所有策略/连接器继承同一 logger，事件类型枚举（OrderCreated / OrderFilled / OrderCancelled ...）。我们的"事件分类"目前只存在于人类对日志文本的约定中，没有枚举。
4. **Debug Console**：开发模式下可接入 PyCharm/VSCode 断点 + 实时日志流。我们连 `tail -f` 多文件都得 tmux 分屏。
5. **会话级 trace**：每个 strategy tick 有 tick_id，所有在该 tick 内产生的事件共享这个 id——正好是我们**场景 B 答不上来**缺的那个能力。

---

## 7. 小结备忘（供后续改造用）

- 目前唯一**结构化且有全动作覆盖**的日志源是 `/root/maomao/data/exec_log.jsonl`，改造方向建议以此为骨架向外扩字段。
- 最大的两个技术债：①bot.log 的 **TG token 明文泄漏** ②所有大文件**无轮转**。
- 最大的两个业务债：①缺 **trace_id** 串接"信号→对话→执行"三层 ②缺**对话原文**（用户/机器人 I/O）。
- 已死模块：`/root/short_attack/logs/*`（04-13 后停写，2.2 MB 垃圾）。
