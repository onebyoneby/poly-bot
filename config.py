"""交易系统配置"""
import os
from dotenv import load_dotenv

load_dotenv()

# === Polymarket 连接 ===
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
GAMMA_HOST = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
DATA_HOST = os.getenv("DATA_HOST", "https://data-api.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))

# === 账户 ===
PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
API_KEY = os.getenv("POLY_API_KEY", "")
WALLET_ADDRESS = os.getenv("POLY_WALLET_ADDRESS", "")
FUNDER_ADDRESS = os.getenv("POLY_FUNDER_ADDRESS", WALLET_ADDRESS)

# === 风控参数 ===
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "50"))  # 单笔最大 USDC
MAX_TOTAL_EXPOSURE = float(os.getenv("MAX_TOTAL_EXPOSURE", "3000"))  # 总最大敞口
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "10"))  # 最大持仓数
STOP_LOSS_PCT = abs(float(os.getenv("STOP_LOSS_PCT", "0.10")))  # 止损比例(正数)

# 风控硬上限 — 防止 .env 误配导致无限制交易
_HARD_MAX_EXPOSURE = 99999  # 总敞口绝对上限（已放开）
_HARD_MAX_POSITIONS = 99999  # 持仓数绝对上限（已放开）
if MAX_TOTAL_EXPOSURE > _HARD_MAX_EXPOSURE:
    import logging as _log
    _log.getLogger("config").warning(
        f"MAX_TOTAL_EXPOSURE={MAX_TOTAL_EXPOSURE} 超过硬上限 {_HARD_MAX_EXPOSURE}，已截断"
    )
    MAX_TOTAL_EXPOSURE = _HARD_MAX_EXPOSURE
if MAX_POSITIONS > _HARD_MAX_POSITIONS:
    import logging as _log
    _log.getLogger("config").warning(
        f"MAX_POSITIONS={MAX_POSITIONS} 超过硬上限 {_HARD_MAX_POSITIONS}，已截断"
    )
    MAX_POSITIONS = _HARD_MAX_POSITIONS

# === 策略参数 ===
# 套利策略
MIN_ARBITRAGE_PROFIT = float(os.getenv("MIN_ARBITRAGE_PROFIT", "0.01"))  # 最小套利利润率(1%)

# 闪崩买入策略
FLASH_CRASH_DROP_THRESHOLD = float(os.getenv("FLASH_CRASH_DROP_THRESHOLD", "0.15"))
FLASH_CRASH_RECOVERY_TARGET = float(os.getenv("FLASH_CRASH_RECOVERY_TARGET", "0.08"))
FLASH_CRASH_STOP_LOSS = float(os.getenv("FLASH_CRASH_STOP_LOSS", "0.05"))

# 跟单策略
COPY_TRADE_MIN_PROFIT = float(os.getenv("COPY_TRADE_MIN_PROFIT", "100000"))

# 动量策略
MOMENTUM_LOOKBACK_MINUTES = int(os.getenv("MOMENTUM_LOOKBACK_MINUTES", "30"))
MOMENTUM_MIN_CHANGE = float(os.getenv("MOMENTUM_MIN_CHANGE", "0.05"))

# === 运行模式 ===
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))  # 扫描间隔(秒)

# === 跟踪的顶级交易者(从排行榜获取) ===
TOP_TRADERS = [
    "Theo4",
    "Fredi9999",
    "kch123",
    "Len9311238",
    "zxgngl",
    "DrPufferfish",
    "PrincessCaro",
]

# ============================================================
# === 实时跟单配置（稳健保守方案）===
# ============================================================

COPY_POLL_INTERVAL = int(os.getenv("COPY_POLL_INTERVAL", "2"))
COPY_STOPLOSS_CHECK_INTERVAL = int(os.getenv("COPY_STOPLOSS_CHECK_INTERVAL", "15"))

# --- 跟单目标钱包 ---
# 自动从 .env 中读取所有 COPY_WALLET_ 开头的变量
# T1（必跟）: COPY_WALLET_T1_xxx=0x...  或旧格式中的固定名单
# T2（共识时跟）: COPY_WALLET_T2_xxx=0x...
# 无前缀 T1/T2 的默认归入 T1

_T1_DEFAULTS = {"CHOMPCHOMP888", "C4C4"}
_T2_DEFAULTS = {"DLEK", "THEONLYHUMAN", "0X2A2C"}


def _parse_wallet_env(env_dict: dict) -> tuple[dict[str, str], dict[str, str]]:
    """从字典中解析 COPY_WALLET_* 钱包"""
    tier1: dict[str, str] = {}
    tier2: dict[str, str] = {}
    for key, val in env_dict.items():
        if not key.startswith("COPY_WALLET_") or not val or not val.startswith("0x"):
            continue
        suffix = key[len("COPY_WALLET_"):]

        if suffix.startswith("T1_"):
            name = suffix[3:]
            tier1[name] = val
        elif suffix.startswith("T2_"):
            name = suffix[3:]
            tier2[name] = val
        elif suffix.upper() in _T1_DEFAULTS:
            tier1[suffix] = val
        elif suffix.upper() in _T2_DEFAULTS:
            tier2[suffix] = val
        else:
            tier1[suffix] = val
    return tier1, tier2


def reload_wallets_from_env() -> tuple[dict[str, str], dict[str, str]]:
    """重新读取 .env 文件并解析钱包列表（不影响其他环境变量）"""
    from dotenv import dotenv_values
    fresh = dotenv_values()
    return _parse_wallet_env(fresh)


COPY_TIER1_WALLETS, COPY_TIER2_WALLETS = _parse_wallet_env(os.environ)

# --- 过滤规则 ---
# 注意：不限制价格方向，大佬买啥跟啥（YES@$0.05 或 NO@$0.95 都跟）
COPY_MIN_SIZE = float(os.getenv("COPY_MIN_SIZE", "5"))              # 规则3: 单笔最小 $5
COPY_MAX_SIZE = float(os.getenv("COPY_MAX_SIZE", "15"))             # 规则3: 单笔最大 $15
COPY_BASE_SIZE = float(os.getenv("COPY_BASE_SIZE", "10"))           # 默认跟单金额
COPY_CONSENSUS_MIN_SIZE = float(os.getenv("COPY_CONSENSUS_MIN_SIZE", "20"))  # 规则4: 共识最小 $20
COPY_CONSENSUS_MAX_SIZE = float(os.getenv("COPY_CONSENSUS_MAX_SIZE", "30"))  # 规则4: 共识最大 $30
COPY_DAILY_LOSS_LIMIT = float(os.getenv("COPY_DAILY_LOSS_LIMIT", "50"))      # 规则6: 日最大亏损 $50
COPY_MARKET_MAX_POSITION = float(os.getenv("COPY_MARKET_MAX_POSITION", "30"))  # 规则7: 单市场最大 $30
COPY_STOP_LOSS_PCT = abs(float(os.getenv("COPY_STOP_LOSS_PCT", "0.15")))  # 止损 15%(正数，代码中 loss_pct >= 此值时触发)
COPY_SLIPPAGE = float(os.getenv("COPY_SLIPPAGE", "0.02"))             # FOK 滑点保护 2%
COPY_MIN_PRICE = float(os.getenv("COPY_MIN_PRICE", "0.10"))           # 最低跟单价格(过滤彩票)
COPY_MAX_PRICE = float(os.getenv("COPY_MAX_PRICE", "0.75"))           # 最高跟单价格(过滤低赔率)
COPY_CONSENSUS_WINDOW = int(os.getenv("COPY_CONSENSUS_WINDOW", "1800"))  # 共识时间窗口 30分钟
COPY_CATCHUP_MINUTES = int(os.getenv("COPY_CATCHUP_MINUTES", "30"))      # 启动时追补过去N分钟内的交易
COPY_CONSENSUS_MIN_WALLETS = int(os.getenv("COPY_CONSENSUS_MIN_WALLETS", "3"))  # T2共识最低钱包数
COPY_SPORT_CONSENSUS_MIN = int(os.getenv("COPY_SPORT_CONSENSUS_MIN", "2"))     # 体育市场T1也需共识(不禁止但更谨慎)
COPY_WHALE_MIN_USDC = float(os.getenv("COPY_WHALE_MIN_USDC", "5"))      # 大佬最小下注额(过滤噪音小单)
COPY_T1_MIN_SOLO_USDC = float(os.getenv("COPY_T1_MIN_SOLO_USDC", "50"))  # T1独跟最小金额(低于此需2+T1确认)
COPY_MAX_DELAY_SEC = int(os.getenv("COPY_MAX_DELAY_SEC", "120"))          # 最大延迟(秒)，超过跳过
COPY_MAX_MARKET_DAYS = int(os.getenv("COPY_MAX_MARKET_DAYS", "7"))        # 只跟N天内结算的市场
COPY_MAX_SEEN_IDS = int(os.getenv("COPY_MAX_SEEN_IDS", "2000"))

# --- 主动止盈参数 ---
COPY_AUTO_TP_PCT = float(os.getenv("COPY_AUTO_TP_PCT", "0.10"))           # 自动止盈: 涨10%卖出
COPY_TRAILING_ACTIVATE_PCT = float(os.getenv("COPY_TRAILING_ACTIVATE_PCT", "0.06"))  # 追踪止盈激活: 涨6%开始追踪
COPY_TRAILING_STOP_PCT = float(os.getenv("COPY_TRAILING_STOP_PCT", "0.03"))          # 追踪回撤: 从峰值回落3%卖出
