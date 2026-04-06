# 乌鸦团队 · 工作日志

> 供顾问查阅，按时间倒序。记录架构变更、功能上线、封板状态。

---

## 2026-04-06 · 交易底座 v1.0 封板 + 日志体系上线

### 交易底座封板（`chattr +i`，不得直接修改）
| 文件 | 说明 |
|------|------|
| `/root/maomao/trader/parser.py` | 自然语言→JSON，支持强平价/暗单/百分比平/逐仓 |
| `/root/maomao/trader/router.py` | 交易关键词路由，直接动作 vs 预览动作 |
| `/root/maomao/trader/preview.py` | 预览卡+文字确认流（回复"确认"/60s超时） |
| `/root/maomao/trader/order.py` | 开/平/加止损止盈/强平价/暗单/百分比平仓 |
| `/root/maomao/trader/exchange.py` | Binance API封装，含algoOrder/多账户余额/划转 |
| `/root/shared/core.py` | 共享Bot底盘v4.2，双管道（硬解析+AI） |

### 日志体系（未封板，可扩展）
| 文件 | 数据文件 | 说明 |
|------|---------|------|
| `trader/trade_log.py` | `data/trade_log.json` | 交易执行记录，100条/7天，`/7`查询 |
| `trader/bot_log.py` | `data/bot_log.json` | Bot上线/下线/异常，100条/7天，`/8`查询 |
| — | `data/sys_log.json` | 系统快照（CPU/内存/磁盘/服务），每5分钟，`/9`查询 |

### 玄玄快捷TG指令（`/1`–`/9`）
| 指令 | 功能 |
|------|------|
| `/1` | 查询所有持仓（方向/杠杆/入场价/强平价/浮盈） |
| `/2` | 查询三账户余额（合约/现货/资金，<1U过滤） |
| `/3 <金额>` | 现货→合约划转 |
| `/4 <金额>` | 合约→现货划转 |
| `/5 <金额>` | 现货→资金划转 |
| `/6 <金额>` | 资金→现货划转 |
| `/7 [n]` | 交易执行日志，默认20条，最多50 |
| `/8 [n]` | Bot运行事件日志 |
| `/9 [n]` | 系统资源快照 |

### 交易指令支持范围
- 开单：`做多/做空 SOL 5x 100u`，全仓/逐仓
- 强平价开单：`做多 SOL 强平价 60`（自动反推杠杆+数量）
- 止损止盈：`加止损 79 止盈 85`
- 平仓：`平 SOL` / `平 SOL 50%`
- 暗单（随机拆单）：任意开/平指令加"暗单"
- 撤单：`撤单 SOL` / `取消SOL挂单`
- 移动止盈：保守/中等/激进/翻倍四档

### 清理
- 根目录历史修复脚本 ×10 已删除
- `/root/bot/` 空壳目录已删除
- `trader/` 下 `.bak` 文件 ×8 已清理
- Redis 相关描述从文档移除（未使用，无价值）

---

## 2026-04-06（早期）· 三Bot架构重建

- damao/maomao/baobao 三服务独立部署，共用 `shared/core.py` 底盘
- 玄玄交易底座从零搭建（parser→router→preview→order→exchange）
- 双管道设计：硬解析处理原子交易指令，AI处理复杂/上下文对话
- 预览+文字确认流替换 InlineKeyboard（更可靠）
- algoOrder止损止盈对接（币安专用接口，与普通挂单分离）
- live_data.md 规则：玄玄查询仓位/余额必须调API，禁止猜测
- liq_concept.md 规则：强平价=爆仓价概念说明

---

## 2026-03-29 · 玄玄重建 & 架构迁移

- 停用旧 trade-bot.service（Gemini正则版），解决409冲突
- 玄玄身份重建（SOUL/IDENTITY/DREAMS/RELATION档案）
- trader_bot 模型：haiku → sonnet（订阅包月）
- OpenClaw 停用，释放内存~370MB / 磁盘~1GB
- 玄玄档案封存至 GitHub `xuanxuan_archive/`
- P1 pump_daemon 两个Bug修复（SETTLING合约/整点卡片静默问题）

---

## 当前封板清单

| 模块 | 封板方式 | 备注 |
|------|---------|------|
| `trader/` 五个核心文件 | `chattr +i` | 改动需乌鸦解锁授权 |
| `shared/core.py` | `chattr +i` | 同上 |
| `trade_log.py` / `bot_log.py` | 未封 | 日志模块保留扩展空间 |
| `baobao/bot.py` | 未封 | 待后续开发对接 pump_daemon |

## 当前服务状态

| 服务 | 端口/协议 | 状态 |
|------|---------|------|
| damao | Telegram polling | ✅ 运行 |
| maomao | Telegram polling | ✅ 运行 |
| baobao | Telegram polling | ✅ 运行（骨架，待对接） |
| Redis | — | 已移除 |
