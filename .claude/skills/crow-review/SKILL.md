---
name: crow-review
description: 审 crow-bot 代码，带铁律和架构约束。当用户要求审查 /root/maomao、/root/damao、/root/scripts 下代码，或说"扫 xxx"、"review xxx"时使用。
---

# crow-review：乌鸦家代码审查

专用于乌鸦团队代码库的定制审查流程。相比通用 `/simplify`，额外带入：
- 7 条开发铁律（封板/不打补丁/原生优先 等）
- 5 条架构约束（+i/TG shell/多账户/模块开关/DRY）
- 三段式报告格式（Bug/效率/清洁）

使用方式：`/crow-review <文件或模块路径>` 或直接说自然语言（见下方别名映射）
例：`/crow-review bull_sniper.py`、`/crow-review trader/multi/`、「扫交易策略」

---

## Phase 0：加载策略 ground truth（必做，可跳过）

**为什么要这步**：skill 不评判策略设计本身是否科学，但**能查代码是否忠实实现了乌鸦写下来的规则**。
"代码 vs 文档漂移"是一类极低成本高收益的 Bug —— 文档说阈值 28、代码写 25；文档说不跑币安1、代码循环 4 账户；这种必须标出来。

### 加载源（按优先级合并）

1. **模块对应 spec 文件**（别名映射表里带了就读；没有就跳过）
2. **`/root/CLAUDE.md`** 模块相关章节（搜模块名或模块路径）
3. **记忆目录 `/root/.claude/projects/-root/memory/`** 下的 `project_<模块>_spec.md` / `project_*_tuning.md` / `feedback_*.md`
   用 `Grep` 在 `MEMORY.md` 里找相关条目，按文件名读进来

### 抽取"规则条目"

从上述文档里抽出**能和代码对照**的**规则**（不是设计动机、不是背景）。典型形态：
- 数值阈值（"进池 ≥8%"、"评分 ≥28 推信号"、"回撤 25% 止盈"、"冷却 60 分钟"）
- 路径/账户限制（"做空阻击不推广多账户，只跑 X"、"tt/震天响只接触币安2"）
- 触发/顺序（"先撤单再平仓"、"浮盈 ≥50% 激活移动止盈"）
- 模块开关（"mode=off 纯记录不下单"）

把这些条目作为**第 4 条共享上下文**传给下面三个 Agent。Agent 1 在审 Bug 时显式检查每一条"代码 vs 文档一致性"。

### 文档不存在怎么办

- 仅有 CLAUDE.md 章节：够用，继续。
- 连章节都没有：Phase 0 产出一行 `⚠️ 无 ground truth 文档，本轮只做工程质量审查`，跳过去 Phase 1。
- **不要**自己编"应该的阈值"当 ground truth —— 宁可跳过，不可幻觉。

---

## Phase 1：识别审查范围

### 模块别名映射（支持自然语言）

用户用自然语言指代模块时，按下表解析成实际路径 + 对应 spec：

| 用户说 | 实际扫描范围 | 策略 spec 源 |
|---|---|---|
| 扫交易策略 / 扫策略 / 扫交易策略模块 | `trader/skills/bull_sniper/` | CLAUDE.md「做多阻击系统」+ memory `project_bull_sniper_spec.md` / `project_bull_sniper_layers.md` / `project_scoring_tuning.md` |
| 扫做多阻击 / 扫 bull | 同上 | 同上 |
| 扫做空阻击 / 扫做空 | `maomao/short_attack/` | memory `project_short_attack_audit.md` / `feedback_short_attack_no_multi.md` |
| 扫多账户 | `trader/multi/` | CLAUDE.md「多账户架构」+ memory `project_multi_account.md` |
| 扫移动止盈 | `trader/trailing.py` + `trader/skills/bull_sniper/bull_trailing.py` | CLAUDE.md「移动止盈预设 v3.1」 |
| 扫滚仓 | `trader/rolling.py` | CLAUDE.md「滚仓 v2.0」 |
| 扫下单 / 扫交易 | `trader/order.py` + `trader/exchange.py` | memory `project_trade_cmd_spec.md` |

表里没有的说法：先用 Glob/Grep 猜最匹配路径，确认后再扫；spec 去 memory 目录 `grep -i` 匹配。

### 拉取范围

1. 用户指定了文件/目录（或别名命中的路径），直接 Read/Grep 读原文。
2. 没指定，运行 `git diff`（或 `git diff HEAD`）看改动。
3. 都没有，扫最近修改的文件。

把完整原文（或 diff）**和 Phase 0 抽出的规则条目清单**一起作为三个审查代理的共同输入。

---

## Phase 2：三维度并行审查

用 Agent 工具**并行**启动三个审查代理（同一条消息里多个 tool call）。每个代理独立工作，最后汇总。

### Agent 1：⭕ Bug 审查

重点找**会炸的**问题：

- **策略/代码漂移**（Phase 0 喂进来的 ground truth 逐条对照）：
  * 数值阈值不一致（文档说 28，代码写 25）
  * 账户/路径限制被违反（文档说"不跑币安1"，代码循环 4 账户）
  * 触发顺序/流程倒置（文档说"先撤再平"，代码直接平）
  * 模块开关被绕过（mode=off 但仍下单）
- **封板违规**：改了标 🔒 的文件（`chattr +i`）、或把新逻辑塞进封板模块内部
- **权限漏洞**：`trader/multi/` 公开函数漏了 `require(role, action, account)` 开头检查
- **hedge mode 错误**：双向持仓下只平一侧、positionSide 漏传、reduceOnly 和 positionSide 混用
- **并发竞态**：模块被多线程调用时，客户端缓存/状态文件/字典没加锁
- **精度丢失**：下单数量没走 `_fix(qty, stepSize)`、价格没走 tickSize 对齐
- **API key 暴露**：print / log / Telegram 推送里带了 key 前缀
- **状态文件损坏**：写 JSON 没先写临时文件再 rename，进程 kill 会留半文件
- **异常吞噬**：`except Exception: pass` 无日志，把关键失败压下来

### Agent 2：🔥 效率坑审查

重点找**白烧 CPU/等待时间**的问题：

- **串行多账户**：4 个账户循环查接口没用 `ThreadPoolExecutor`，节省 1.5~3 秒
- **重复 API 调用**：同一轮循环内多次拉 `get_position_risk`、`mark_price`，应复用
- **N+1 请求**：对列表每个元素单独调 API，应走批量接口
- **热路径阻塞**：每次 tick/每条消息都读大文件、重建字典、初始化客户端
- **土办法轮询**：能用原生 algoOrder / 条件单的地方自己 while True 盯盘（违反铁律4）
- **未改写回**：轮询中不管状态变没变都 `_save(state)`，触发下游 noise
- **TOCTOU 预检**：先 `Path.exists()` 再操作，应直接 try+except
- **启动重活**：服务启动阶段同步加载几十 MB 数据、同步请求外部 API

### Agent 3：🧹 代码清洁审查

重点找**能删能合并**的代码：

- **重复实现**：新写的工具函数在 `/root/scripts/core.py`、`/root/shared/`、`trader/multi/` 已有（违反 DRY）
- **废弃代码**：留着 `# old version` 注释块、`_legacy_*` 函数、早版本的封板注释
- **参数膨胀**：函数签名挂 5+ 参数只用其中 2 个，应拆或重构
- **复制粘贴变种**：几处几乎一样只差一个字段，应抽公共
- **stringly-typed**：判断 `if action == "trade"` 时 "trade"/"query"/"admin" 不是常量
- **无效注释**：注释讲 WHAT（代码已经表达）、引用调用者（会腐烂）、任务编号
- **冗余 try/except**：套在内部函数已经处理过异常的调用外面
- **幽灵向后兼容**：为不存在的调用方留 `_old_name = new_name` 别名
- **wrapper 泛滥**：薄适配只改一个字段名，调用方改掉更直接

---

## Phase 3：修复

等三个代理都返回后：

1. **按严重度排序**：⭕ Bug 优先改，🔥 效率坑其次，🧹 清洁最后
2. **批量小改一起提**：相邻行的同类改动合一次编辑
3. **封板文件**：发现需改时，先报告乌鸦，`chattr -i` 解锁 → 改 → 测 → `chattr +i` 重新封板
4. **无法修的标记出来**：不是所有 finding 都值得立即改，挑真正有价值的

改完用**三段式报告**交付（Bug 段里把"策略/代码漂移"单独前置）：

```
⭕ Bug（N 条）
  [漂移] 1. <文件>:<行> — 文档规则「...」 vs 代码「...」 — <修法>
  [工程] 1. <文件>:<行> — <问题> — <修法>
  ...

🔥 效率坑（N 条）
  1. <文件>:<行> — <问题> — <优化>
  ...

🧹 代码清洁（N 条）
  1. <文件>:<行> — <冗余> — <删/合并建议>
  ...
```

最后一行用一句话总结：本次审查覆盖 X 个文件，Phase 0 加载 K 条规则，共 Y 条改动已落地，Z 条跳过（原因：...）。

---

## 附录 A：7 条开发铁律（照抄自 `/root/CLAUDE.md`）

### 铁律1：封板制度
封板模块列表以 `/root/CLAUDE.md` 为准。审查前先跑：

```bash
lsattr -R /root/trader/ /root/shared/ /root/maomao/trader/ 2>/dev/null | grep -- '----i'
```

拿当前 `+i` 状态，不依赖 skill 内硬编码。封板后不允许改动，必须改先报告乌鸦确认。

### 铁律2：新功能不碰旧模块
新建文件、新增函数、新增配置项。不改已封板模块内部逻辑。

### 铁律3：不打补丁
发现设计问题从根本重新设计，不补丁叠补丁。

### 铁律4：原生优先
能用交易所原生 API 实现的直接调用，不自己写脚本模拟。
币安/HL 原生移动止盈、OCO、条件单等用官方接口。
只有真的做不到才自己写。

### 铁律5：先出架构方案再动手
方案包含：改哪些文件、新建哪些文件、对封板模块有没有影响。确认后再动。

### 铁律6：完成一块测一块

### 铁律7：不触碰 Claude Code 系统规则
写功能和脚本不涉及 Claude Code 本身的系统配置、权限、规则。
需要动 Code 系统层面的问题，先报告乌鸦。

---

## 附录 B：5 条架构约束（crow-review 专属）

审查时必须逐条核对：

1. **+i 封板先解锁再改**
   改前先 `lsattr <file>` 确认是否 +i，是则 `chattr -i` 解锁、改完测完 `chattr +i` 重新封。直接改封板文件报错就跳过是错的。

2. **TG shell 冻结不动**
   `bot.py` 主入口、消息路由、Telegram token 处理冻结，不改动。要改先报告乌鸦。

3. **多账户并行化关注点**
   `trader/multi/` 下任何对 N 个账户 fan-out 的查询/下单循环，必须用 `ThreadPoolExecutor`，不许串行。
   客户端缓存必须双检锁（`_lock` 已在 registry.py 准备）。

4. **模块有 start/stop 开关**
   每个守护/扫描/推送模块必须提供独立开关（配置文件 mode: on/off 或 service disable），不许把开关嵌进硬编码逻辑。

5. **写一次用多次（DRY）**
   新写工具函数前先在 `/root/scripts/core.py`、`/root/shared/`、`trader/multi/` 搜同名或同职责函数。重复实现算违规。

---

## 附录 C：常见绿区（不用误报）

以下情况看起来像问题其实不是，审查时放行：

- `except Exception: pass` 在 `get_full_balance` 的 spot/funding 环节（best-effort，主数据是合约）
- 封板文件里看到"不符合新风格的代码"——封板即历史，不动
- `_resolve_name` 私有别名仍在：为封板模块向后兼容
- TG 推送里的 emoji（🐦/⭕/🔥/🧹/📊 等）——是乌鸦偏好，不是冗余
- 同一个常量在 `config.yaml` 和 `CLAUDE.md` 各写一遍——配置+文档分离，不算重复

---

## 使用范例

```
用户：扫交易策略
→ 解析为 trader/skills/bull_sniper/
→ /crow-review trader/skills/bull_sniper/
→ 启动 3 个代理并行审查
→ 产出三段式报告
→ 按优先级批量修复
→ 汇报改动清单
```

结束。保持简洁直白，不搞陪伴口吻。
