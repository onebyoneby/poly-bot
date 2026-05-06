# Polymarket 智能交易系统

> 基于 Python 的 Polymarket 预测市场自动化交易机器人，支持多策略并行、实时跟单、自动钱包管理、微信通知、Web 仪表盘等功能，7×24 小时无人值守运行。

## 核心特性

- **5 大交易策略** — 套利、闪崩抄底、跟单共识、动量追踪、实时跟单
- **实时跟单引擎** — 2 秒轮询顶级交易者钱包，T1/T2 分层跟单机制
- **自动钱包管理** — 定时刷新排行榜，自动添加/移除/降级跟单钱包
- **风控系统** — 止损止盈、日亏损限额、单市场持仓限额、FOK 滑点保护
- **微信通知** — 企业微信 + PushPlus 双通道，实时告警 + 日报/周报
- **Web 仪表盘** — Streamlit 实时监控面板（端口 8502）
- **崩溃恢复** — JSON 状态持久化，重启自动恢复持仓和 P&L
- **DRY_RUN 模式** — 默认模拟交易，验证策略后再切换实盘

## 技术栈

| 组件 | 说明 |
|------|------|
| **语言** | Python 3.10+ |
| **异步框架** | asyncio + aiohttp |
| **交易接口** | py-clob-client（Polymarket CLOB API） |
| **数据源** | Gamma API + CLOB API + Data API |
| **通知** | 企业微信 Webhook + PushPlus |
| **Web UI** | Streamlit（端口 8502） |
| **终端 UI** | Rich（表格、面板、进度条） |

---

## 项目结构

```
polymarket-bot/
├── main.py                    # 单策略入口（扫描/交易/跟单）
├── run_all.py                 # 统一调度器（跟单 + 管理 + 天气）
├── start.sh                   # 一键启动脚本
├── config.py                  # 集中配置（从 .env 读取所有参数）
├── market_client.py           # API 客户端（Gamma/CLOB/Data）
├── trader.py                  # 订单执行（买/卖/撤单）
├── risk_manager.py            # 风控（持仓跟踪、止损、P&L）
├── auto_manager.py            # 自动管理（钱包轮换、日报）
├── weather_bot.py             # 天气预测策略
├── wechat_bot.py              # 微信交互机器人
├── wechat_notifier.py         # 微信通知推送
├── wallet_optimizer.py        # 钱包组合优化
├── requirements.txt           # Python 依赖
├── strategies/                # 策略模块
│   ├── base.py                # 策略基类（ABC）
│   ├── arbitrage.py           # 套利策略
│   ├── flash_crash.py         # 闪崩策略
│   ├── copy_trade.py          # 跟单共识策略
│   ├── momentum.py            # 动量策略
│   ├── realtime_copy.py       # 实时跟单引擎（核心）
│   └── weather.py             # 天气策略
├── webui/
│   └── app.py                 # Streamlit 仪表盘
└── logs/                      # 日志与状态文件
    ├── engine_state.json      # 跟单引擎状态（持仓、P&L）
    ├── risk_state.json        # 风控状态
    └── *.log                  # 各模块日志
```

---

## 第一步：环境搭建

### macOS

```bash
# 1. 安装 Python 3.10+（推荐 Homebrew）
brew install python@3.10

# 2. 克隆项目
git clone https://gitee.com/mason_0xxb/polymarket-bot.git
cd polymarket-bot

# 3. 创建虚拟环境
python3.10 -m venv .venv
source .venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的账户信息（详见下方配置说明）
```

### Linux（Ubuntu/Debian）

```bash
# 1. 安装 Python 3.10+
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3-pip

# 2. 克隆项目
git clone https://gitee.com/mason_0xxb/polymarket-bot.git
cd polymarket-bot

# 3. 创建虚拟环境
python3.10 -m venv .venv
source .venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 配置环境变量
cp .env.example .env
```

### Windows

```powershell
# 1. 下载安装 Python 3.10+
# https://www.python.org/downloads/ （安装时勾选 "Add to PATH"）

# 2. 克隆项目
git clone https://gitee.com/mason_0xxb/polymarket-bot.git
cd polymarket-bot

# 3. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 配置环境变量
copy .env.example .env
# 用记事本编辑 .env 填入账户信息
```

---

## 第二步：Polymarket 账户准备

使用本系统前，需要在 Polymarket 平台完成以下准备：

### 1. 注册 Polymarket 账户

1. 访问 [Polymarket](https://polymarket.com) 并注册账户
2. 完成 KYC 验证（如需要）
3. 向账户充值 USDC（Polygon 链）

### 2. 获取 API 凭证

需要以下信息填入 `.env` 文件：

| 凭证 | 说明 | 获取方式 |
|------|------|----------|
| `POLY_PRIVATE_KEY` | 以太坊 EOA 钱包私钥 | 从你的钱包导出（MetaMask → 账户详情 → 导出私钥） |
| `POLY_API_KEY` | CLOB API Key | Polymarket 开发者后台获取，或通过 py-clob-client 自动创建 |
| `POLY_WALLET_ADDRESS` | 交易钱包地址 | 你的 Polygon 钱包地址（0x 开头） |
| `POLY_FUNDER_ADDRESS` | 资金来源地址 | 通常与钱包地址相同 |

> **安全提示**：私钥是最核心的敏感信息，切勿泄露。`.env` 文件已在 `.gitignore` 中排除。

---

## 第三步：配置参数

编辑 `.env` 文件，所有参数都可通过此文件配置：

### 账户配置（必填）

```bash
# 以太坊 EOA 私钥（不是 API Key）
POLY_PRIVATE_KEY=你的以太坊私钥

# CLOB API Key
POLY_API_KEY=你的API_Key

# 交易钱包地址
POLY_WALLET_ADDRESS=0x你的钱包地址

# 资金来源地址（通常与钱包地址相同）
POLY_FUNDER_ADDRESS=0x你的钱包地址
```

### 运行模式

```bash
# true = 模拟交易（默认），false = 实盘交易
DRY_RUN=true

# 策略扫描间隔（秒）
SCAN_INTERVAL=30
```

### 风控参数

```bash
# 单笔最大金额（USDC）
MAX_POSITION_SIZE=50

# 总持仓上限（USDC）
MAX_TOTAL_EXPOSURE=3000

# 最大同时持仓数
MAX_POSITIONS=10

# 止损比例（10% = 亏损达到入场金额的10%时平仓）
STOP_LOSS_PCT=0.10


### 跟单钱包配置

在 `.env` 中添加要跟踪的交易者钱包地址：

```bash
# T1 钱包（必跟）— 高胜率大佬，其交易直接跟单
COPY_WALLET_用户名=0x钱包地址    # 备注信息

# T2 钱包（共识时跟）— 需要多人同时买入才跟
COPY_WALLET_T2_用户名=0x钱包地址  # 备注信息

# 示例：
# COPY_WALLET_TopTrader1=0xabcdef1234567890abcdef1234567890abcdef12
# COPY_WALLET_T2_Whale99=0x1234567890abcdef1234567890abcdef12345678
```

**T1 vs T2 分层规则：**

| 层级 | 行为 | 适用场景 |
|------|------|----------|
| **T1（必跟）** | 该钱包任何交易直接跟单（金额 ≥ `COPY_T1_MIN_SOLO_USDC` 时） | 高 PNL/Vol 比、长期稳定盈利的顶级交易者 |
| **T2（共识时跟）** | 需要 ≥ `COPY_CONSENSUS_MIN_WALLETS` 个钱包在 30 分钟内买入同一市场才跟 | PNL 高但可能是套利型、需要共识确认的交易者 |

> **钱包来源**：从 Polymarket 排行榜 (`https://polymarket.com/leaderboard`) 筛选周 PNL > $100k 且日 PNL > 0 的活跃交易者。

---

## 第四步：运行

### 方式一：一键启动（推荐，Linux/macOS）

```bash
# 赋予执行权限（首次）
chmod +x start.sh

# 注意：start.sh 中的 PYTHON 路径可能需要修改
# 默认为 /opt/homebrew/bin/python3.10，请改为你的 Python 路径
# 例如 Linux: PYTHON="python3.10" 或 PYTHON="/usr/bin/python3.10"

# DRY_RUN 模式（模拟交易）
./start.sh

# 实盘模式
./start.sh --live

# 查看状态
./start.sh status

# 停止所有服务
./start.sh stop

# 启动微信交互机器人（需扫码，前台运行）
./start.sh wechat
```

`start.sh` 会同时启动：
1. **统一引擎**（`run_all.py`）— 跟单 + 自动管理 + 天气
2. **Web 仪表盘**（Streamlit）— `http://localhost:8502`

### 方式二：统一调度器

```bash
# 运行所有模块
python run_all.py

# 实盘模式
python run_all.py --live

# 跳过某些模块
python run_all.py --no-copy       # 不启动跟单引擎
python run_all.py --no-manager    # 不启动自动管理
python run_all.py --no-weather    # 不启动天气机器人
```

### 方式三：单策略运行

```bash
# 运行所有策略（DRY_RUN）
python main.py

# 只扫描不交易
python main.py --scan

# 查看市场概览（Top 20 活跃市场）
python main.py --overview

# 运行指定策略
python main.py --strategy arbitrage      # 套利
python main.py --strategy flash_crash    # 闪崩
python main.py --strategy copy_trade     # 跟单共识
python main.py --strategy momentum       # 动量


```

### 方式四：独立模块

```bash
# 自动管理（钱包轮换、日报）
python auto_manager.py

# 天气预测策略
python weather_bot.py

# Web 仪表盘
streamlit run webui/app.py --server.port 8502

# 微信交互机器人（需扫码）
python wechat_bot.py
```

---

## 命令行参数速查

| 参数 | 适用入口 | 说明 |
|------|----------|------|
| `--live` | main.py / run_all.py / start.sh | 切换为实盘交易（默认 DRY_RUN） |
| `--scan` | main.py | 只扫描市场机会，不执行交易 |
| `--overview` | main.py | 显示 Top 20 活跃市场概览 |
| `--strategy NAME` | main.py | 只运行指定策略 |
| `--copy` | main.py | 实时跟单模式（2 秒轮询） |
| `--no-copy` | run_all.py | 不启动跟单引擎 |
| `--no-manager` | run_all.py | 不启动自动管理 |
| `--no-weather` | run_all.py | 不启动天气机器人 |
| `stop` | start.sh | 停止所有服务 |
| `status` | start.sh | 查看运行状态 |
| `wechat` | start.sh | 启动微信机器人（前台） |
| `wechat-bg` | start.sh | 后台启动微信（需已登录） |

---

## 交易策略详解

### 1. 套利策略（ArbitrageStrategy）

当 YES + NO 买入价之和 < $1 时，同时买入两方，无论结果如何都赚差价。

- **触发条件**：YES 价格 + NO 价格 < (1 - `MIN_ARBITRAGE_PROFIT`)
- **参考交易者**：kch123（$247M 交易量，$10.2M 利润）、DrPufferfish

### 2. 闪崩策略（FlashCrashStrategy）

当某市场概率突然大幅下跌时抄底买入，等待回升获利。

- **触发条件**：概率跌幅 > `FLASH_CRASH_DROP_THRESHOLD`（默认 15%）
- **止盈**：回升 `FLASH_CRASH_RECOVERY_TARGET`（默认 8%）
- **止损**：继续下跌 `FLASH_CRASH_STOP_LOSS`（默认 5%）
- **参考交易者**：CemeterySun、Len9311238

### 3. 跟单共识策略（CopyTradeStrategy）

跟踪排行榜顶级交易者，当多人同时买入同一市场时跟单。

- **触发条件**：≥ 2 个顶级交易者在同一市场有活跃头寸
- **最低门槛**：跟单目标 PNL > `COPY_TRADE_MIN_PROFIT`
- **参考交易者**：Theo4、Fredi9999、PrincessCaro

### 4. 动量策略（MomentumStrategy）

跟随 30 分钟内的价格趋势。

- **触发条件**：价格变化 > `MOMENTUM_MIN_CHANGE`（默认 5%）
- **回看窗口**：`MOMENTUM_LOOKBACK_MINUTES`（默认 30 分钟）
- **参考交易者**：walletmobile、RepTrump

### 5. 实时跟单引擎（RealtimeCopyEngine）— 核心

2 秒轮询跟单目标钱包的最新交易，实时跟随。

**7 条过滤规则：**

1. **不限方向** — YES/NO 都跟，大佬买什么跟什么
2. **跳过对冲** — 同一人同一事件买 YES 又买 NO → 视为套利，跳过
3. **金额控制** — 单笔 $5-$15（可配置）
4. **共识放大** — 2+ 个钱包买同一市场 → 金额提升至 $20-$30
5. **跟退出** — 目标卖出时自动跟卖
6. **日亏损限额** — 当日亏损达 $50 时停止交易
7. **单市场限额** — 同一市场最大持仓 $30

**止盈机制：**
- 自动止盈：涨 10% 立即卖出
- 追踪止盈：涨 6% 后激活追踪，从峰值回落 3% 时卖出
- 止损：亏损 15% 时平仓

---

## 自动管理系统

`auto_manager.py` 在后台持续运行，自动优化跟单钱包组合：

| 周期 | 动作 |
|------|------|
| **每 5 分钟** | 检查 P&L、钱包表现、触发调整 |
| **每 30 分钟** | 自动刷新钱包（添加新的高手、移除表现差的） |
| **每 1 小时** | 刷新排行榜数据 |
| **每天** | 生成日报、备份 .env 配置 |

**自动规则：**
- 全仓亏损 > 5% 的钱包 → 自动移除
- P&L 恶化 > $10 → 告警
- 30 分钟无交易 → 告警
- T1 钱包持续亏损 → 自动降级为 T2

---

```

### systemd

```bash
sudo tee /etc/systemd/system/polymarket-bot.service << 'EOF'
[Unit]
Description=Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/polymarket-bot
ExecStart=/path/to/polymarket-bot/.venv/bin/python run_all.py --live
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot
sudo systemctl start polymarket-bot
```

### screen

```bash
screen -S polymarket
source .venv/bin/activate
python run_all.py --live
# Ctrl+A D 脱离，screen -r polymarket 重连
```

---

## 日志与监控

| 日志文件 | 说明 |
|----------|------|
| `logs/runner_stdout.log` | 统一引擎输出 |
| `logs/copy_engine.log` | 跟单引擎详细日志 |
| `logs/auto_manager_YYYYMMDD.log` | 自动管理日志（按天） |
| `logs/weather_bot.log` | 天气策略日志 |
| `logs/engine_state.json` | 跟单引擎状态（持仓、P&L） |
| `logs/risk_state.json` | 风控状态（崩溃恢复用） |
| `trading.log` | 主交易日志（5MB 轮转，3 份备份） |

**Web 仪表盘**：`http://服务器IP:8502`（Streamlit）

---

## 故障排查

| 问题 | 解决方案 |
|------|----------|
| `ModuleNotFoundError` | 确认已激活虚拟环境：`source .venv/bin/activate` |
| `py-clob-client` 安装失败 | 升级 pip：`pip install --upgrade pip`，确认 Python ≥ 3.10 |
| API 连接超时 | 检查网络，Polymarket API 需要能访问外网。可配置代理 |
| DRY_RUN 模式无实际交易 | 正常行为。设置 `DRY_RUN=false` 并使用 `--live` 参数开启实盘 |
| 私钥错误 | 确认 `POLY_PRIVATE_KEY` 是 EOA 私钥（非 API Key），以 `0x` 开头或纯 hex |
| 跟单无反应 | 检查 `.env` 中 `COPY_WALLET_*` 是否配置了有效的钱包地址 |
| 微信机器人连接失败 | `wechat_bot.py` 需要前台运行并扫码登录 |
| 重启后持仓丢失 | 检查 `logs/engine_state.json` 和 `logs/risk_state.json` 是否存在 |
| `start.sh` 找不到 Python | 修改 `start.sh` 第 9 行的 `PYTHON` 路径为你的 Python 3.10 路径 |

---

## 许可证

仅供学习研究使用。加密货币交易具有高风险，使用本软件产生的任何收益或损失由使用者自行承担。
