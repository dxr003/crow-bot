# OKX 接入工程量评估
> 2026-04-23 | 来源：agent-trade-kit + onchainos-skills GitHub 仓库原文

---

## 一、信息源说明

| 仓库 | 版本 | 实际路径 |
|------|------|----------|
| `okx/agent-trade-kit` | latest | `/root/okx-research/agent-trade-kit/` |
| `okx/onchainos-skills` | v2.4.0 | `/root/okx-research/onchainos-skills/` |

**⚠️ 勘误**：任务要求读取 `okx-onchain-gateway` 的 simulate tx 能力——该 skill **确实存在**（`skills/okx-onchain-gateway/`），之前评估错误。内容见第三问。

---

## 二、四个核心问题

### 问题 a：okx-dex-token 的 holder cluster analysis 能替代 Nansen 吗？

**字段清单（来自 `cli-reference.md` 原文）**：

`cluster-overview` 返回：
| 字段 | 含义 |
|------|------|
| `clusterConcentration` | `Low / Medium / High` 集中度等级 |
| `top100HoldingsPercent` | 前 100 地址持仓占比 |
| `rugPullPercent` | 跑路概率 % |
| `holderNewAddressPercent` | 前 1000 持有人中，3天内新钱包占比 |
| `holderSameFundSourcePercent` | 前 1000 持有人中，资金来源相同的占比 |
| `holderSameCreationTimePercent` | 前 1000 持有人中，同一时间创建的占比 |

`cluster-top-holders`（top 10/50/100）返回：
- `averagePnlUsd`、`averageBuyPriceUsd`、`averageSellPriceUsd`、`averageHoldingPeriod`
- `clusterTrendType`：`buy / sell / neutral / transfer / transferIn`
- `holdingPercent`：前 N 地址占总供应比例

`token holders --tag smart-money / whale / KOL` 支持按标签过滤 top 100 持仓地址。

**结论：能替代 Nansen 的 E 因子部分，不能完全替代 Nansen。**

能覆盖的部分：
- E3a（新钱包占比）→ `holderNewAddressPercent` ✅
- E3b（bundler 聪合）→ `holderSameFundSourcePercent` + `rugPullPercent` 近似覆盖 ✅
- E3c（持有人数量）→ `token price-info` 有 holderCount ✅
- E3d（持有人趋势）→ `clusterTrendType` ✅
- 聪明钱地址识别 → `--tag smart-money` ✅（免费）

**不能覆盖的部分**：
- 精确地址画像（Nansen 标签库 300+ 维度）
- 历史聪明钱行为轨迹（不只是持仓快照）
- 跨链聪明钱追踪

**ROI 判断**：你目前 E 因子用的是 Moralis，而 OKX 这套完全免费且字段更丰富。如果 v3.4 验证期结束前仍未订阅 Nansen，OKX cluster analysis 是比 Moralis 更适合的临时替代方案。

---

### 问题 b：okx-onchain-gateway 的 simulate tx 能识别蜜罐吗？

**原文**（`okx-onchain-gateway/SKILL.md`）：

```bash
# 模拟交易（dry-run）
onchainos gateway simulate --from 0xYourWallet --to 0xContract --data 0x... --chain xlayer
```

`gateway simulate` 的设计目标是**预估 gas 和模拟执行结果**，不是安全扫描。

蜜罐检测走的是独立的 `okx-security` 模块：

```bash
onchainos security token-scan --tokens "<chainId>:<contractAddress>"
```

返回 `riskLevel`（CRITICAL/HIGH/MEDIUM/LOW）+ 风险标签（Honeypot、Gas-mint scam、Rug pull gang 等）。

**结论**：`gateway simulate` **不能**识别蜜罐，它只跑 EVM 执行模拟。蜜罐识别在 `okx-security` 里，和你之前问的 Binance Token Audit 功能完全等价。但同样的问题：你的系统扫的是币安合约 TRADING 状态的币，蜜罐在这个场景下不存在——这两个工具对你都没有使用价值。

---

### 问题 c：swap 模块 trailing stop 参数 vs 我们的 exchange.py

**agent-trade-kit `swap` 模块**（永续合约，不是 DEX Swap）：

下单时可附带 TP/SL：
```bash
okx swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz 1 \
  --tpTriggerPx 100000 --tpOrdPx 99900 \
  --slTriggerPx 85000 --slOrdPx 84900
```

**没有 trailing stop 参数。** OKX 合约的 trailing stop 需要通过 `ordType=move_order_stop` 单独下单，agent-trade-kit v1 尚未封装这个接口。

**我们的实现对比**：

| 维度 | 我们的 exchange.py + trailing.py | OKX agent-trade-kit |
|------|--------------------------------|---------------------|
| trailing stop | `cron` 每分钟轮询 + 峰值比较 + 暗单平仓 | ❌ 未封装，需自己调原生 API |
| TP/SL 附单 | `place_stop_loss` / `place_take_profit` | ✅ `--tpTriggerPx --slTriggerPx` |
| 保证金模式 | `CROSSED / ISOLATED` | `cross / isolated` |
| 多账户 | `multi/executor` 4 账户并行 | ❌ 单账户（`~/.okx/config.toml`） |
| 精度处理 | `bn_precision.py` 自动对齐 stepSize/tickSize | 不清楚，文档未提 |

**结论**：OKX 的 trailing stop 能力比我们弱（未封装），TP/SL 附单比我们简洁。我们的底盘在多账户和移动止盈上明显更成熟，OKX 接入不会带来执行层升级，只会带来平台切换成本。

---

### 问题 d：期权模块最小开仓金额 + 可用币种

**原文**（`option.md`）：
- 合约格式：`BTC-USD-241227-50000-C / P`，`ETH-USD-250328-3000-C / P`
- **文档中没有提最小开仓金额**，需要实际调 `option_get_instruments` 查询 `minSz` 字段
- 文档示例只有 **BTC 和 ETH**，OKX 实际支持的币种更多但 SDK 未列举
- 买方用 `tdMode=cash`（无需保证金，亏损有限）；卖方用 `cross / isolated`

**工程量估计**：期权接入需要独立开发——当前 multi/executor 完全没有期权接口，需要新模块。优先级低，路线图五期之后。

---

## 三、OKX 接入总体工程量评估

### 认证层（最大阻力）

OKX API 是三件套：`api_key + secret_key + passphrase`，而我们的币安是两件套。

`config.toml.example` 原文：
```toml
[profiles.live]
api_key = "your-live-api-key"
secret_key = "your-live-secret-key"
passphrase = "your-live-passphrase"   # ← 币安没有这个
```

**影响**：`multi/registry.py` 的账户注册逻辑需要改，`exchange.py` 的客户端初始化需要改。工程量约 2-3 天。

### 执行层

| 功能 | 币安当前状态 | OKX 接入代价 |
|------|------------|-------------|
| 开平仓 | ✅ 成熟稳定 | 重写 API 调用层，全新验证 |
| 移动止盈 | ✅ trailing.py v4.1 | OKX 原生 `move_order_stop` 需单独对接 |
| 多账户并行 | ✅ 4 账户 executor | OKX SDK 单账户，需自己做 fan-out |
| 精度处理 | ✅ bn_precision.py | OKX 有不同精度规范，需新写 |

### 数据层（唯一净增益）

onchainos-skills 的链上数据是真正的增益点：
- `okx-dex-token cluster analysis` → E 因子升级
- `okx-dex-signal` → smart money 信号补充（需评估）

**关键结论**：这两个功能是**纯数据调用**，不需要接入 OKX 执行层。可以独立集成到现有系统，不需要切换交易所。

---

## 四、结论与建议

| 接入项 | 建议 | 理由 |
|--------|------|------|
| OKX 永续合约执行层 | ❌ 不接入 | 工程量大，执行层不如现有币安底盘，无净增益 |
| OKX 期权模块 | ⏳ 路线图五期后再看 | 有潜力，但现在优先级不在这 |
| okx-dex-token cluster analysis | ✅ 值得评估接入 | 免费，字段比 Moralis 丰富，可升级 E 因子 |
| okx-security token-scan | ❌ 不需要 | 蜜罐场景在币安合约里不存在（同 Binance Token Audit 结论） |
| okx-onchain-gateway simulate | ❌ 不需要 | 不能识别蜜罐，也没有其他对现有系统的补充价值 |

**最小可行接入路径**（如果要做）：

只做数据层，不碰执行层：
1. 在 E 因子分析流程里调 `onchainos token cluster-overview` 替代 Moralis 的持仓集中度查询
2. 不需要 OKX API Key（onchainos CLI 有共享 Key，或申请免费 Web3 Key）
3. 不影响任何现有交易执行代码

工程量：**0.5 天**（在 `analyzer.py` 加一个新的 HTTP 调用函数）。
