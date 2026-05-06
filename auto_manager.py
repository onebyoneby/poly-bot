"""
自动管理员 — 24小时无人值守运行

功能:
1. 每5分钟: 读 engine_state.json → 分析PnL → 自动调整钱包
2. 每1小时: 刷新排行榜 → 淘汰差钱包 → 补充新钱包
3. 每天结束: 生成日报 (logs/daily_report.md)
4. 天气播报: 写入 logs/weather_report.md

管理规则:
  a) 移除钱包: 全仓亏损>5%自动注释掉
  b) PnL改善>$5时记录
  c) PnL恶化>$10时触发分析+钱包刷新
  d) 追踪已实现PnL趋势
  e) 30分钟无交易触发钱包刷新

运行: nohup python auto_manager.py &
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp
import config
from market_client import MarketClient

# ============================================================
# 日志 — 按日期分文件: logs/auto_manager_2026-03-24.log
# ============================================================
os.makedirs("logs", exist_ok=True)
logger = logging.getLogger("auto_manager")
logger.setLevel(logging.INFO)

class DailyFileHandler(logging.Handler):
    """每天自动切换日志文件"""
    def __init__(self, log_dir="logs", prefix="auto_manager"):
        super().__init__()
        self.log_dir = log_dir
        self.prefix = prefix
        self._current_date = ""
        self._fh = None

    def _ensure_handler(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._fh:
                self._fh.close()
            path = os.path.join(self.log_dir, f"{self.prefix}_{today}.log")
            self._fh = open(path, "a", encoding="utf-8")
            self._current_date = today

    def emit(self, record):
        self._ensure_handler()
        msg = self.format(record)
        self._fh.write(msg + "\n")
        self._fh.flush()

daily_fh = DailyFileHandler()
daily_fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(daily_fh)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(ch)

PROXY = os.environ.get("https_proxy") or os.environ.get("http_proxy") or "http://192.168.0.155:7897"
ENV_PATH = Path(__file__).parent / ".env"
COPY_LOG = Path(__file__).parent / "logs" / "copy_engine.log"
ENGINE_STATE = Path(__file__).parent / "logs" / "engine_state.json"
MANAGER_STATE = Path(__file__).parent / "logs" / "manager_state.json"
WEATHER_LOG = Path(__file__).parent / "logs" / "weather_bot.log"
DAILY_REPORT = Path(__file__).parent / "logs" / "daily_report.md"
WEATHER_REPORT = Path(__file__).parent / "logs" / "weather_report.md"

# 管理规则阈值
WALLET_LOSS_THRESHOLD = -5.0      # 钱包全仓亏损>5%时移除
PNL_IMPROVE_THRESHOLD = 5.0       # PnL改善>$5时记录
PNL_WORSEN_THRESHOLD = -10.0      # PnL恶化>$10时告警
NO_TRADE_TIMEOUT = 1800           # 30分钟无交易告警
CHECK_INTERVAL = 300              # 每5分钟检查一次
TUNE_INTERVAL = 1800              # 每30分钟自动调优一次

# ============================================================
# 状态跟踪
# ============================================================
class ManagerState:
    def __init__(self):
        self.last_pnl = None           # 上次总PnL
        self.last_realized = None       # 上次已实现PnL
        self.last_floating = None       # 上次浮动PnL
        self.last_trade_time = None     # 上次新交易时间
        self.last_trade_count = 0       # 上次日交易数
        self.last_wallet_refresh = 0    # 上次刷新钱包时间戳
        self.last_daily_report = ""     # 上次日报日期
        self.last_weather_report = 0    # 上次天气播报时间
        self.daily_start_pnl = None     # 当天起始PnL
        self.daily_trades = 0
        self.daily_cost = 0.0
        self.today = ""
        self.removed_wallets = []       # 已移除的钱包记录
        self.last_seen_trade_id = 0     # 最后处理的成交ID
        self.last_seen_settle_id = 0    # 最后处理的结算ID
        self.last_tune_time = 0         # 上次自动调优时间
        self.tune_history = []          # 调优历史
        self._load_persistent()

    def _load_persistent(self):
        """加载持久化状态"""
        if MANAGER_STATE.exists():
            try:
                with open(MANAGER_STATE) as f:
                    data = json.load(f)
                self.removed_wallets = data.get("removed_wallets", [])
                self.last_realized = data.get("last_realized")
                self.last_floating = data.get("last_floating")
                self.last_pnl = data.get("last_pnl")
                self.last_trade_count = data.get("last_trade_count", 0)
                self.last_seen_trade_id = data.get("last_seen_trade_id", 0)
                self.last_seen_settle_id = data.get("last_seen_settle_id", 0)
            except Exception:
                pass

    def save_persistent(self):
        """保存持久化状态"""
        data = {
            "removed_wallets": self.removed_wallets,
            "last_realized": self.last_realized,
            "last_floating": self.last_floating,
            "last_pnl": self.last_pnl,
            "last_trade_count": self.last_trade_count,
            "last_seen_trade_id": self.last_seen_trade_id,
            "last_seen_settle_id": self.last_seen_settle_id,
            "updated_at": datetime.now().isoformat(),
        }
        with open(MANAGER_STATE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

state = ManagerState()


# ============================================================
# 工具函数
# ============================================================

def read_engine_state() -> dict | None:
    """直接读取 engine_state.json（比日志解析更准确）"""
    if not ENGINE_STATE.exists():
        return None
    try:
        with open(ENGINE_STATE) as f:
            return json.load(f)
    except Exception:
        return None


def analyze_engine_positions(engine_state: dict) -> dict:
    """从 engine_state.json 分析持仓，返回完整指标"""
    positions = engine_state.get("positions", {})
    active = {k: v for k, v in positions.items() if not v.get("is_sold", False)}

    total_exposure = sum(p["size_usd"] for p in active.values())
    floating_pnl = sum(
        (p.get("current_price", p["entry_price"]) - p["entry_price"]) * p.get("shares", 0)
        for p in active.values()
    )
    realized = engine_state.get("daily_pnl", 0.0)

    return {
        "time": engine_state.get("saved_at", ""),
        "positions": len(active),
        "cost": total_exposure,
        "value": total_exposure + floating_pnl,
        "floating": floating_pnl,
        "realized": realized,
        "trades": engine_state.get("daily_trades", 0),
        "total_pnl": floating_pnl + realized,
        "is_stopped": engine_state.get("is_stopped", False),
        "active_positions": active,
    }


def analyze_wallet_performance(active_positions: dict) -> dict:
    """分析每个钱包来源的持仓表现"""
    wallet_perf = {}
    for p in active_positions.values():
        src = p.get("source", "unknown")
        entry = p["entry_price"]
        current = p.get("current_price", entry)
        if entry <= 0:
            continue
        pnl_pct = (current - entry) / entry * 100
        pnl_usd = (current - entry) * p.get("shares", 0)

        names = re.findall(r"[A-Za-z0-9_]+", src)
        for name in names:
            if name in ("共识", "T1", "T2", "跟单"):
                continue
            if name not in wallet_perf:
                wallet_perf[name] = {
                    "total": 0, "positions": [], "pnl_sum": 0.0,
                    "wins": 0, "losses": 0,
                }
            wallet_perf[name]["total"] += 1
            wallet_perf[name]["positions"].append(pnl_pct)
            wallet_perf[name]["pnl_sum"] += pnl_usd
            if pnl_usd >= 0:
                wallet_perf[name]["wins"] += 1
            else:
                wallet_perf[name]["losses"] += 1

    return wallet_perf


def rule_a_remove_bad_wallets(wallet_perf: dict) -> list:
    """规则a: 移除全仓亏损>WALLET_LOSS_THRESHOLD的钱包"""
    bad_wallets = []
    already_removed = {w["name"] for w in state.removed_wallets}

    for name, perf in wallet_perf.items():
        if name in already_removed:
            continue
        if perf["total"] >= 2 and all(p < WALLET_LOSS_THRESHOLD for p in perf["positions"]):
            avg = sum(perf["positions"]) / len(perf["positions"])
            bad_wallets.append((name, perf["total"], avg, perf["pnl_sum"]))

    for name, count, avg_pct, pnl_usd in bad_wallets:
        reason = f"全{count}仓亏损 avg={avg_pct:.1f}% pnl=${pnl_usd:.1f}"
        success = comment_out_wallet_by_name(name, reason)
        if success:
            state.removed_wallets.append({
                "name": name,
                "reason": reason,
                "removed_at": datetime.now().isoformat(),
            })
            logger.info(f"🔴 规则a自动移除钱包 {name}: {reason}")

    return bad_wallets


def comment_out_wallet_by_name(wallet_name: str, reason: str) -> bool:
    """根据钱包名在.env中注释掉对应行"""
    if not ENV_PATH.exists():
        return False

    with open(ENV_PATH, "r") as f:
        lines = f.readlines()

    modified = False
    new_lines = []
    name_lower = wallet_name.lower()
    for line in lines:
        stripped = line.strip()
        # 匹配包含该钱包名的 COPY_WALLET_ 行（未注释的）
        if (stripped.startswith("COPY_WALLET_") and
            not stripped.startswith("#") and
            name_lower in stripped.lower().split("=")[0].lower()):
            ts = datetime.now().strftime("%m-%d %H:%M")
            new_lines.append(f"# {stripped}  # 自动移除[{ts}] {reason}\n")
            modified = True
            logger.info(f"  .env已注释: {stripped.split('=')[0]}")
        else:
            new_lines.append(line)

    if modified:
        with open(ENV_PATH, "w") as f:
            f.writelines(new_lines)

    return modified


def read_latest_metrics() -> dict:
    """从日志尾部读取最新指标（只读最后50KB，避免读整个大文件）"""
    if not COPY_LOG.exists():
        return {}

    try:
        file_size = COPY_LOG.stat().st_size
        with open(COPY_LOG, "r", encoding="utf-8", errors="ignore") as f:
            if file_size > 50000:
                f.seek(file_size - 50000)
                f.readline()  # 跳过不完整行
            lines = f.readlines()
    except:
        return {}

    metrics = {}
    for line in reversed(lines[-500:]):
        if "总成本:" in line:
            m = re.search(
                r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*'
                r'总成本: .(\d+[\d.]*).* 现市值: .(\d+[\d.]*).*'
                r'浮动PnL: .([+-]?\d+[\d.]*).*已实现PnL: .([+-]?\d+[\d.]*).*'
                r'日交易: (\d+)', line
            )
            if m:
                metrics = {
                    "time": m.group(1),
                    "cost": float(m.group(2)),
                    "value": float(m.group(3)),
                    "floating": float(m.group(4)),
                    "realized": float(m.group(5)),
                    "trades": int(m.group(6)),
                    "positions": int(float(m.group(2)) / 20),  # 估算
                }
                metrics["total_pnl"] = metrics["floating"] + metrics["realized"]
                break

    return metrics


def _read_log_tail(max_bytes=100000) -> str:
    """只读日志文件尾部，避免读116MB全文件"""
    if not COPY_LOG.exists():
        return ""
    try:
        file_size = COPY_LOG.stat().st_size
        with open(COPY_LOG, "r", encoding="utf-8", errors="ignore") as f:
            if file_size > max_bytes:
                f.seek(file_size - max_bytes)
                f.readline()  # 跳过不完整行
            return f.read()
    except:
        return ""


def read_position_details() -> list[dict]:
    """从日志读取持仓详情表"""
    content = _read_log_tail(200000)

    positions = []
    # 从持仓表中提取每个持仓的来源和PnL
    pattern = r'│\s*\d+\s*│\s*(.+?)\s*│\s*(.+?)\s*│\s*(共识\(.+?\)|T1:.+?)\s*│\s*\$([0-9.]+)\s*│\s*\$([0-9.]+)\s*│\s*\$([0-9.]+)\s*│\s*\$([+-]?[0-9.]+)\s*\(([+-]?[0-9.]+)%\)'
    for m in re.finditer(pattern, content):
        positions.append({
            "market": m.group(1).strip(),
            "source": m.group(3).strip(),
            "cost": float(m.group(4)),
            "entry": float(m.group(5)),
            "current": float(m.group(6)),
            "pnl": float(m.group(7)),
            "pnl_pct": float(m.group(8)),
        })

    return positions


def read_recent_events() -> dict:
    """读取最近的止盈/止损/交易事件"""
    tail = _read_log_tail(50000)
    if not tail:
        return {"tp": [], "sl": [], "new_trades": 0, "last_trade_ts": ""}
    lines = tail.splitlines()

    result = {"tp": [], "sl": [], "new_trades": 0, "last_trade_ts": ""}

    for line in lines:
        if "止盈成交" in line or "主动止盈" in line:
            m = re.search(r'PnL=\$([+-]?[0-9.]+)', line)
            if m:
                result["tp"].append(float(m.group(1)))
        elif "止损成交" in line:
            m = re.search(r'PnL=\$([+-]?[0-9.]+)', line)
            if m:
                result["sl"].append(float(m.group(1)))
        elif "成交确认" in line:
            result["new_trades"] += 1
            m = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
            if m:
                result["last_trade_ts"] = m.group(1)

    return result


def parse_recent_trades(since_id: int = 0) -> list[dict]:
    """解析最近的成交记录（含来源、市场、方向、金额）

    返回 since_id 之后的新成交。每条包含:
    - id: 成交编号
    - time: 成交时间
    - source: 跟单来源（如 共识(A|B) 或 T1:xxx）
    - market: 市场名称
    - direction: BUY/SELL
    - outcome: 结果选项
    - price: 大佬成交价
    - my_price: 我方报价
    - amount: 金额
    - shares: 股数
    - delay: 延迟秒数
    """
    tail = _read_log_tail(200000)
    if not tail:
        return []

    trades = []
    # 匹配完整的成交块: 模拟买入 → 订单参数 → 成交确认
    # 先找所有成交确认行，再往前回溯找详情
    blocks = tail.split("成交确认 #")
    for block in blocks[1:]:  # 跳过第一个（前面的内容）
        # 提取成交编号
        m_id = re.match(r'(\d+)', block)
        if not m_id:
            continue
        trade_id = int(m_id.group(1))
        if trade_id <= since_id:
            continue

        # 成交确认行: #10806 orderID=... | $20.00 (71.1股) | 持仓=432个
        m_confirm = re.search(r'\$(\d+\.\d+)\s*\((\d+\.?\d*)股\)', block)
        amount = float(m_confirm.group(1)) if m_confirm else 0
        shares = float(m_confirm.group(2)) if m_confirm else 0

        # 时间
        m_time = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]',
                           tail.split(f"成交确认 #{trade_id}")[0][-200:])
        trade_time = m_time.group(1) if m_time else ""

        # 往前找模拟买入块（在成交确认之前的内容）
        pre_text = tail.split(f"成交确认 #{trade_id}")[0][-2000:]

        # 来源: <<< 模拟买入 >>> 共识(A|B) 或 T1:xxx
        m_src = re.search(r'模拟买入 >>>\s*(.+)', pre_text)
        source = m_src.group(1).strip() if m_src else "unknown"

        # 市场名
        m_mkt = re.search(r'市场:\s*(.+)', pre_text)
        market = m_mkt.group(1).strip()[:60] if m_mkt else "unknown"

        # 方向和结果 (来自检测到交易行)
        m_dir = re.search(r'方向:\s*(BUY|SELL)\s*\|\s*结果:\s*(.+?)\s*\|', pre_text)
        direction = m_dir.group(1) if m_dir else "BUY"
        outcome = m_dir.group(2).strip()[:30] if m_dir else ""

        # 价格
        m_price = re.search(r'大佬成交价:\s*\$([0-9.]+)', pre_text)
        boss_price = float(m_price.group(1)) if m_price else 0
        m_my = re.search(r'我方报价:\s*\$([0-9.]+)', pre_text)
        my_price = float(m_my.group(1)) if m_my else 0

        # 延迟
        m_delay = re.search(r'延迟:\s*([0-9.]+)s', pre_text)
        delay = float(m_delay.group(1)) if m_delay else 0

        trades.append({
            "id": trade_id,
            "time": trade_time,
            "source": source,
            "market": market,
            "direction": direction,
            "outcome": outcome,
            "boss_price": boss_price,
            "my_price": my_price,
            "amount": amount,
            "shares": shares,
            "delay": delay,
        })

    return trades


def parse_recent_settlements(since_id: int = 0) -> list[dict]:
    """解析最近的止盈/止损记录（含市场、PnL、持仓时长）

    返回 since_id 之后的结算记录。每条包含:
    - id: 订单编号
    - time: 结算时间
    - type: "止盈" 或 "止损"
    - market: 市场名称
    - entry_price: 入场价
    - exit_price: 卖出价
    - change_pct: 涨跌幅
    - pnl: 盈亏金额
    - pnl_pct: 盈亏百分比
    - hold_time: 持仓时长
    - daily_pnl: 当时日PnL
    """
    tail = _read_log_tail(200000)
    if not tail:
        return []

    settlements = []

    # 止损块: <<< 触发止损 >>> 市场名 → 详情 → ✗ 止损成交 #xxx
    for m in re.finditer(
        r'(?:触发止损|自动止盈)[^>]*>>>\s*(.+?)\n'
        r'\s*入场价:\s*\$([0-9.]+)\s*→\s*现价:\s*\$([0-9.]+)\s*\(([^)]+)\)\n'
        r'\s*(?:动态止损线.*?|峰值价.*?)\|\s*持仓:\s*([0-9.]+)股\s*\|\s*成本:\s*\$([0-9.]+)\n'
        r'\s*卖出价:\s*\$([0-9.]+)\s*\|\s*预计(?:亏损|盈利):\s*\$([+-]?[0-9.]+)\n'
        r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*[✗★]\s*(\S+)\s*#(\d+)',
        tail, re.DOTALL
    ):
        settle_id = int(m.group(11))
        if settle_id <= since_id:
            continue

        settle_type = "止盈" if "止盈" in m.group(10) else "止损"
        pnl = float(m.group(8))

        # 提取持仓时长和日PnL
        line_after = tail[m.end():m.end()+200]
        m_hold = re.search(r'持仓(\d+)min', m.group(0) + line_after)
        hold_time = f"{m_hold.group(1)}min" if m_hold else ""
        m_dpnl = re.search(r'日PnL=\$([+-]?[0-9.]+)', m.group(0) + line_after)
        daily_pnl = float(m_dpnl.group(1)) if m_dpnl else None

        settlements.append({
            "id": settle_id,
            "time": m.group(9),
            "type": settle_type,
            "market": m.group(1).strip()[:50],
            "entry_price": float(m.group(2)),
            "exit_price": float(m.group(7)),
            "change_pct": m.group(4),
            "pnl": pnl,
            "shares": float(m.group(5)),
            "cost": float(m.group(6)),
            "hold_time": hold_time,
            "daily_pnl": daily_pnl,
        })

    return settlements


def get_wallet_performance() -> dict[str, dict]:
    """分析每个钱包来源的表现"""
    content = _read_log_tail(200000)
    if not content:
        return {}

    # 从最后一个持仓表中提取
    wallet_perf = {}
    # 匹配持仓表行
    pattern = r'│[^│]+│[^│]+│\s*(共识\(([^)]+)\)|T1:(\w+))\s*│\s*\$[0-9.]+\s*│\s*\$[0-9.]+\s*│\s*\$[0-9.]+\s*│\s*\$([+-]?[0-9.]+)\s*\(([+-]?[0-9.]+)%\)'

    for m in re.finditer(pattern, content):
        source = m.group(1).strip()
        pnl = float(m.group(4))
        pnl_pct = float(m.group(5))

        # 提取钱包名
        wallet_names = []
        if "共识" in source:
            names_str = m.group(2)
            wallet_names = [n.strip() for n in names_str.split("|")]
        elif m.group(3):
            wallet_names = [m.group(3)]

        for wn in wallet_names:
            if wn not in wallet_perf:
                wallet_perf[wn] = {"total_pnl": 0, "count": 0, "wins": 0, "losses": 0}
            wallet_perf[wn]["total_pnl"] += pnl
            wallet_perf[wn]["count"] += 1
            if pnl >= 0:
                wallet_perf[wn]["wins"] += 1
            else:
                wallet_perf[wn]["losses"] += 1

    return wallet_perf


def read_env_wallets() -> dict[str, str]:
    """读取当前.env中的所有钱包"""
    wallets = {}
    if not ENV_PATH.exists():
        return wallets
    with open(ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            if line.startswith("COPY_WALLET_"):
                key, _, val = line.partition("=")
                val = val.split("#")[0].strip()
                wallets[key] = val
    return wallets


def comment_out_wallet(wallet_key: str, reason: str):
    """注释掉.env中的某个钱包"""
    if not ENV_PATH.exists():
        return
    with open(ENV_PATH, "r") as f:
        lines = f.readlines()

    with open(ENV_PATH, "w") as f:
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(wallet_key + "="):
                f.write(f"# {stripped}  # 自动禁用: {reason} ({datetime.now().strftime('%m-%d %H:%M')})\n")
                logger.info(f"已注释掉钱包: {wallet_key} | 原因: {reason}")
            else:
                f.write(line)


def add_wallet_to_env(key: str, address: str, comment: str):
    """添加钱包到.env"""
    if not ENV_PATH.exists():
        return

    # 检查是否已存在
    with open(ENV_PATH, "r") as f:
        content = f.read()

    if address in content:
        return  # 已存在

    # 在最后一个 COPY_WALLET 行后面添加
    lines = content.split("\n")
    insert_idx = len(lines) - 1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("COPY_WALLET_"):
            insert_idx = i + 1
            break

    new_line = f"{key}={address}  # {comment}"
    lines.insert(insert_idx, new_line)

    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines))

    logger.info(f"新增钱包: {key}={address[:10]}... | {comment}")


# ============================================================
# 排行榜查询
# ============================================================

async def fetch_leaderboard(time_period="WEEK") -> list[dict]:
    """查询排行榜"""
    proxy = PROXY
    url = f"https://data-api.polymarket.com/v1/leaderboard"
    params = {"timePeriod": time_period, "orderBy": "PNL", "limit": 50}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        try:
            async with session.get(url, params=params, proxy=proxy) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.warning(f"排行榜查询失败: {e}")
    return []


async def refresh_wallets():
    """刷新钱包: 淘汰差的 + 补充新的"""
    logger.info("\n" + "=" * 60)
    logger.info("钱包刷新 — 每小时自动执行")
    logger.info("=" * 60)

    # 1. 分析现有钱包表现
    perf = get_wallet_performance()
    current_wallets = read_env_wallets()

    # 淘汰条件: 亏损>$5 且胜率<30%
    removed = []
    for wn, data in perf.items():
        if data["count"] >= 3 and data["total_pnl"] < -5 and data["wins"] / max(data["count"], 1) < 0.3:
            # 找对应的env key
            for key in current_wallets:
                if wn.lower() in key.lower():
                    comment_out_wallet(key, f"PnL=${data['total_pnl']:.1f} 胜率{data['wins']}/{data['count']}")
                    removed.append(wn)
                    break

    if removed:
        logger.info(f"淘汰 {len(removed)} 个钱包: {', '.join(removed)}")
    else:
        logger.info("无需淘汰钱包")

    # 2. 从排行榜补充新钱包
    # 查 WEEK 和 DAY 排行榜
    week_board = await fetch_leaderboard("WEEK")
    day_board = await fetch_leaderboard("DAY")

    # 合并: 周PnL > $100k
    week_top = {}
    for entry in week_board:
        pnl = float(entry.get("pnl", 0) or 0)
        vol = float(entry.get("vol", 0) or 0)
        name = entry.get("userName", "") or ""
        wallet = entry.get("proxyWallet", "") or ""
        if pnl > 100000 and wallet:
            week_top[wallet] = {
                "name": name, "pnl": pnl, "vol": vol,
                "ratio": pnl / max(vol, 1),
            }

    # 日PnL > 0 的过滤
    day_active = set()
    for entry in day_board:
        pnl = float(entry.get("pnl", 0) or 0)
        wallet = entry.get("proxyWallet", "") or ""
        if pnl > 0 and wallet:
            day_active.add(wallet)

    # 钱包黑名单: 高频机器人等永久禁止的钱包名
    _WALLET_BLACKLIST = {
        "0p0jogggg",      # 高频机器人，跟单全亏(125仓亏$38 亏损率73%)
    }

    # 筛选: 周PnL > $100k AND 日PnL > 0 AND 不在当前列表 AND 不在黑名单
    existing_addrs = set(v.lower() for v in current_wallets.values())
    candidates = []
    for wallet, data in week_top.items():
        # 黑名单检查
        if any(bl.lower() in (data.get("name", "") or "").lower() for bl in _WALLET_BLACKLIST):
            logger.info(f"  跳过黑名单钱包: {data.get('name', wallet[:8])}")
            continue
        if wallet in day_active and wallet.lower() not in existing_addrs:
            candidates.append({**data, "wallet": wallet})

    # 按 PnL/Vol 比排序
    candidates.sort(key=lambda x: x["ratio"], reverse=True)

    added = 0
    for c in candidates[:3]:  # 最多加3个
        name = c["name"] or c["wallet"][:8]
        safe_name = re.sub(r'[^a-zA-Z0-9_]', '', name)
        if not safe_name:
            safe_name = c["wallet"][2:10]

        tier = "T1" if c["ratio"] > 0.15 else "T2"
        if tier == "T1":
            key = f"COPY_WALLET_{safe_name}"
        else:
            key = f"COPY_WALLET_T2_{safe_name}"

        comment = (f"自动新增{tier}: 周PnL=${c['pnl']/1000:.0f}k "
                   f"PnL/Vol={c['ratio']:.2f} "
                   f"({datetime.now().strftime('%m-%d %H:%M')})")

        add_wallet_to_env(key, c["wallet"], comment)
        added += 1

    if added:
        logger.info(f"新增 {added} 个钱包")
    else:
        logger.info("排行榜无新候选钱包")

    logger.info(f"当前总钱包数: {len(read_env_wallets())}")


# ============================================================
# 天气播报
# ============================================================

def update_weather_report():
    """读取天气机器人日志，生成/更新播报文件"""
    if not WEATHER_LOG.exists():
        return

    try:
        file_size = WEATHER_LOG.stat().st_size
        with open(WEATHER_LOG, "r", encoding="utf-8", errors="ignore") as f:
            if file_size > 100000:
                f.seek(file_size - 100000)
                f.readline()
            content = f.read()
    except:
        return

    # 提取所有下注记录
    bets = []
    pattern = r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*?>>> 下注 #(\d+).*?\n.*?\[天气\] (.+?)\n.*?预报(.+?)\n.*?价格=\$([0-9.]+) 金额=\$(\d+) 优势=([0-9.]+)%'
    for m in re.finditer(pattern, content, re.DOTALL):
        bets.append({
            "time": m.group(1),
            "num": m.group(2),
            "market": m.group(3).strip(),
            "reason": m.group(4).strip(),
            "price": m.group(5),
            "amount": m.group(6),
            "edge": m.group(7),
        })

    # 也提取分析结果
    analyses = []
    analysis_pattern = r'(\w+)\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s+市场=([0-9.]+)%\s+预测=([0-9.]+)%\s+优势=([+-][0-9.]+)%\s+(BUY [YN])'
    for m in re.finditer(analysis_pattern, content):
        analyses.append({
            "city": m.group(1),
            "date": m.group(2),
            "range": m.group(3).strip(),
            "market": m.group(4),
            "predicted": m.group(5),
            "edge": m.group(6),
            "signal": m.group(7),
        })

    # 写入报告
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = f"""# 天气策略播报

> 最后更新: {now}
> 模式: {"DRY_RUN (模拟)" if config.DRY_RUN else "LIVE (实盘)"}

## 当前下注 ({len(bets)} 笔)

| # | 时间 | 市场 | 价格 | 金额 | 优势 |
|---|------|------|------|------|------|
"""
    for b in bets[-20:]:  # 最近20笔
        report += f"| {b['num']} | {b['time'][-8:]} | {b['market'][:40]} | ${b['price']} | ${b['amount']} | {b['edge']}% |\n"

    report += f"\n## 最新分析信号 (优势>8%)\n\n"
    report += "| 城市 | 日期 | 区间 | 市场价 | 预测 | 优势 | 方向 |\n"
    report += "|------|------|------|--------|------|------|------|\n"

    # 取最近的分析（去重）
    seen = set()
    recent = []
    for a in reversed(analyses):
        key = f"{a['city']}{a['date']}{a['range']}"
        if key not in seen:
            seen.add(key)
            recent.append(a)
        if len(recent) >= 30:
            break

    for a in sorted(recent, key=lambda x: abs(float(x['edge'])), reverse=True):
        report += (f"| {a['city']} | {a['date']} | {a['range']:<12} | "
                   f"{a['market']}% | {a['predicted']}% | {a['edge']}% | {a['signal']} |\n")

    with open(WEATHER_REPORT, "w", encoding="utf-8") as f:
        f.write(report)

    logger.info(f"天气播报已更新: {WEATHER_REPORT} ({len(bets)}笔下注)")


# ============================================================
# 日报
# ============================================================

def generate_daily_report(metrics: dict):
    """生成/追加日报"""
    today = datetime.now().strftime("%Y-%m-%d")

    if state.today != today:
        # 新的一天
        state.today = today
        state.daily_start_pnl = metrics.get("total_pnl", 0)
        state.daily_trades = 0
        state.daily_cost = 0

    day_pnl = metrics.get("total_pnl", 0) - (state.daily_start_pnl or 0)

    entry = f"""
## {today}

| 指标 | 值 |
|------|-----|
| 日期 | {today} |
| 更新时间 | {datetime.now().strftime('%H:%M:%S')} |
| 今日投入 | ${metrics.get('cost', 0):.0f} |
| 当前持仓 | {metrics.get('positions', 0)} 个 |
| 今日交易 | {metrics.get('trades', 0)} 笔 |
| 浮动PnL | ${metrics.get('floating', 0):+.2f} |
| 已实现PnL | ${metrics.get('realized', 0):+.2f} |
| **今日总PnL** | **${day_pnl:+.2f}** |
| 总PnL(累计) | ${metrics.get('total_pnl', 0):+.2f} |
| DRY_RUN | {config.DRY_RUN} |

---
"""

    # 读取已有报告
    existing = ""
    if DAILY_REPORT.exists():
        with open(DAILY_REPORT, "r", encoding="utf-8") as f:
            existing = f.read()

    # 如果今天的报告已存在，替换；否则追加
    header = "# Polymarket 跟单日报\n\n"
    if not existing:
        existing = header

    date_header = f"## {today}"
    if date_header in existing:
        # 替换今天的部分
        parts = existing.split(date_header)
        before = parts[0]
        # 找到下一个 ## 或文件末尾
        after_parts = parts[1].split("\n## ", 1)
        after = "\n## " + after_parts[1] if len(after_parts) > 1 else ""
        existing = before + entry.strip() + "\n" + after
    else:
        # 追加到开头（最新在前）
        if existing.startswith(header):
            existing = header + entry + existing[len(header):]
        else:
            existing = header + entry + existing

    with open(DAILY_REPORT, "w", encoding="utf-8") as f:
        f.write(existing)

    logger.info(f"日报已更新: {DAILY_REPORT} | 今日PnL=${day_pnl:+.2f}")


# ============================================================
# 策略自动调优 — 每30分钟执行
# ============================================================

def _update_env_param(key: str, value: str) -> bool:
    """更新.env中的参数值"""
    if not ENV_PATH.exists():
        return False
    with open(ENV_PATH, "r") as f:
        lines = f.readlines()

    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if found:
        with open(ENV_PATH, "w") as f:
            f.writelines(new_lines)
    return found


def auto_tune_strategy():
    """根据最近的盈亏数据自动调优策略参数

    分析维度:
    1. 止盈/止损比 → 调整止盈止损阈值
    2. 不同价位的胜率 → 调整价格过滤区间
    3. 不同来源钱包的贡献 → 移除亏损钱包
    4. 每单平均盈亏 → 调整单笔金额
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"🧠 策略自动调优 @ {datetime.now().strftime('%H:%M:%S')}")
    logger.info(f"{'='*60}")

    # 读取最近的结算数据
    tail = _read_log_tail(500000)
    if not tail:
        logger.info("  无日志数据，跳过调优")
        return

    adjustments = []

    # ── 分析1: 止盈/止损统计 ──
    tp_events = []
    sl_events = []
    for m in re.finditer(r'主动止盈.*?PnL=\$\+?([0-9.]+)\s*\(\+?([0-9.]+)%\)', tail):
        tp_events.append({"pnl": float(m.group(1)), "pct": float(m.group(2))})
    for m in re.finditer(r'止盈成交.*?PnL=\$\+?([0-9.]+)\s*\(\+?([0-9.]+)%\)', tail):
        tp_events.append({"pnl": float(m.group(1)), "pct": float(m.group(2))})
    for m in re.finditer(r'止损成交.*?PnL=\$-([0-9.]+)', tail):
        sl_events.append({"pnl": -float(m.group(1))})

    tp_count = len(tp_events)
    sl_count = len(sl_events)
    total_settled = tp_count + sl_count

    if total_settled > 0:
        tp_total = sum(e["pnl"] for e in tp_events)
        sl_total = sum(e["pnl"] for e in sl_events)
        net = tp_total + sl_total
        win_rate = tp_count / total_settled * 100

        avg_tp = tp_total / tp_count if tp_count > 0 else 0
        avg_sl = abs(sl_total / sl_count) if sl_count > 0 else 0

        logger.info(f"  📈 结算统计: {tp_count}赢/{sl_count}输 胜率={win_rate:.0f}%")
        logger.info(f"     止盈总额=${tp_total:+.1f} (均${avg_tp:.1f}/单) | 止损总额=${sl_total:+.1f} (均${avg_sl:.1f}/单)")
        logger.info(f"     净利润=${net:+.1f} | 风险回报比=1:{avg_tp/avg_sl:.1f}" if avg_sl > 0 else f"     净利润=${net:+.1f}")

        # 调优逻辑1: 根据盈亏比调整止盈/止损
        # 核心原则: 不频繁改动，只在数据充分且偏离明显时调整
        if tp_events and sl_events and total_settled >= 20:
            avg_tp_pct = sum(e["pct"] for e in tp_events) / len(tp_events)
            profit_loss_ratio = avg_tp / avg_sl if avg_sl > 0 else 1
            logger.info(f"     平均止盈幅度: {avg_tp_pct:.1f}% | 盈亏比: {profit_loss_ratio:.2f}")

            # 冷却: 距上次止盈调整至少1小时
            last_tp_tune = getattr(state, '_last_tp_tune', 0)
            if time.time() - last_tp_tune > 3600:  # 1小时冷却

                # 盈亏比 < 0.5 说明止盈太早，需要放宽
                if profit_loss_ratio < 0.5:
                    # 读当前值
                    with open(ENV_PATH, "r") as f:
                        env_text = f.read()
                    m_tp = re.search(r'COPY_AUTO_TP_PCT=([0-9.]+)', env_text)
                    current_tp = float(m_tp.group(1)) if m_tp else 0.10
                    # 每次最多增加3%，上限25%
                    new_tp = min(0.25, round(current_tp + 0.03, 2))
                    if new_tp != current_tp:
                        _update_env_param("COPY_AUTO_TP_PCT", str(new_tp))
                        adjustments.append(f"止盈{current_tp*100:.0f}%→{new_tp*100:.0f}%(盈亏比{profit_loss_ratio:.2f}过低)")
                        logger.info(f"  🔧 放宽止盈: {current_tp*100:.0f}%→{new_tp*100:.0f}% (盈亏比{profit_loss_ratio:.2f}<0.5)")
                        state._last_tp_tune = time.time()

                # 盈亏比 > 2.0 说明可以适当收紧（锁住利润）
                elif profit_loss_ratio > 2.0 and win_rate < 30:
                    with open(ENV_PATH, "r") as f:
                        env_text = f.read()
                    m_tp = re.search(r'COPY_AUTO_TP_PCT=([0-9.]+)', env_text)
                    current_tp = float(m_tp.group(1)) if m_tp else 0.10
                    new_tp = max(0.06, round(current_tp - 0.02, 2))
                    if new_tp != current_tp:
                        _update_env_param("COPY_AUTO_TP_PCT", str(new_tp))
                        adjustments.append(f"止盈{current_tp*100:.0f}%→{new_tp*100:.0f}%(胜率{win_rate:.0f}%低,锁利)")
                        logger.info(f"  🔧 收紧止盈: {current_tp*100:.0f}%→{new_tp*100:.0f}% (胜率低,先锁利)")
                        state._last_tp_tune = time.time()
            else:
                remain = (3600 - (time.time() - last_tp_tune)) / 3600
                logger.info(f"     止盈调整冷却中 (还需{remain:.1f}h)")

        # 调优逻辑2: 共识钱包数 - 需要冷却+充分数据
        last_consensus_tune = getattr(state, '_last_consensus_tune', 0)
        if total_settled >= 15 and time.time() - last_consensus_tune > 3600:  # 1小时冷却
            with open(ENV_PATH, "r") as f:
                env_content = f.read()
            m_cw = re.search(r'COPY_CONSENSUS_MIN_WALLETS=(\d+)', env_content)
            current_consensus = int(m_cw.group(1)) if m_cw else 3

            if win_rate < 35 and current_consensus < 5:
                new_consensus = current_consensus + 1
                _update_env_param("COPY_CONSENSUS_MIN_WALLETS", str(new_consensus))
                adjustments.append(f"共识{current_consensus}→{new_consensus}(胜率{win_rate:.0f}%过低)")
                logger.info(f"  🔧 收紧共识要求: {current_consensus}→{new_consensus}个钱包 (胜率{win_rate:.0f}%过低)")
                state._last_consensus_tune = time.time()
            elif win_rate > 65 and total_settled >= 20 and current_consensus > 2:
                new_consensus = current_consensus - 1
                _update_env_param("COPY_CONSENSUS_MIN_WALLETS", str(new_consensus))
                adjustments.append(f"共识{current_consensus}→{new_consensus}(胜率{win_rate:.0f}%优秀)")
                logger.info(f"  🔧 放宽共识要求: {current_consensus}→{new_consensus}个钱包 (胜率{win_rate:.0f}%优秀)")
                state._last_consensus_tune = time.time()
        elif total_settled >= 15 and time.time() - last_consensus_tune <= 3600:
            remain = (3600 - (time.time() - last_consensus_tune)) / 3600
            logger.info(f"     共识调整冷却中 (还需{remain:.1f}h)")

    # ── 分析2: 不同价位段的盈亏 ──
    price_buckets = {"低(0.08-0.20)": [], "中(0.20-0.50)": [], "高(0.50-0.90)": []}
    for m in re.finditer(r'入场价:\s*\$([0-9.]+)\s*→\s*现价.*?预计(?:亏损|盈利):\s*\$([+-]?[0-9.]+)', tail):
        entry = float(m.group(1))
        pnl = float(m.group(2))
        if entry < 0.20:
            price_buckets["低(0.08-0.20)"].append(pnl)
        elif entry < 0.50:
            price_buckets["中(0.20-0.50)"].append(pnl)
        else:
            price_buckets["高(0.50-0.90)"].append(pnl)

    if any(price_buckets.values()):
        logger.info(f"\n  📊 价位段盈亏:")
        for bucket, pnls in price_buckets.items():
            if pnls:
                total = sum(pnls)
                wins = sum(1 for p in pnls if p > 0)
                logger.info(f"     {bucket}: {len(pnls)}笔 净${total:+.1f} 胜率{wins/len(pnls)*100:.0f}%")

        # 如果低价位持续亏损，提高最低价格
        low_pnls = price_buckets["低(0.08-0.20)"]
        if len(low_pnls) >= 3 and sum(low_pnls) < -5:
            _update_env_param("COPY_MIN_PRICE", "0.20")
            adjustments.append("最低价0.08→0.20(低价亏损)")
            logger.info(f"  🔧 提高最低价格: → $0.20 (低价位持续亏损)")

    # ── 分析3: 钱包贡献度（从持仓分析） ──
    engine = read_engine_state()
    if engine:
        metrics = analyze_engine_positions(engine)
        active = metrics.get("active_positions", {})
        wallet_perf = analyze_wallet_performance(active)

        if wallet_perf:
            # 按PnL排序
            sorted_wallets = sorted(wallet_perf.items(), key=lambda x: x[1]["pnl_sum"])
            worst = [(n, d) for n, d in sorted_wallets if d["pnl_sum"] < -10 and d["total"] >= 3]

            if worst:
                logger.info(f"\n  🔴 表现最差钱包:")
                for name, data in worst[:5]:
                    loss_rate = data["losses"] / data["total"] * 100
                    logger.info(f"     {name}: {data['total']}仓 PnL=${data['pnl_sum']:.1f} 亏损率{loss_rate:.0f}%")

    # ── 分析4: 交易频率评估 ──
    if engine:
        trades = engine.get("daily_trades", 0)
        positions = len([p for p in engine.get("positions", {}).values() if not p.get("is_sold")])
        if trades > 0 and positions > 0:
            avg_per_trade = metrics.get("total_pnl", 0) / trades if trades > 0 else 0
            logger.info(f"\n  📉 效率: {trades}笔交易 均${avg_per_trade:.3f}/单 持仓{positions}个")

            # 如果每单利润太低(<$0.05)，提高单笔金额 - 需要冷却
            last_size_tune = getattr(state, '_last_size_tune', 0)
            if avg_per_trade < 0.05 and avg_per_trade > -0.1 and time.time() - last_size_tune > 43200:  # 12小时冷却
                with open(ENV_PATH, "r") as f:
                    env_content = f.read()
                m_cs = re.search(r'COPY_CONSENSUS_MIN_SIZE=(\d+)', env_content)
                current_size = int(m_cs.group(1)) if m_cs else 40
                if current_size < 80:
                    new_size = min(80, current_size + 10)
                    _update_env_param("COPY_CONSENSUS_MIN_SIZE", str(new_size))
                    _update_env_param("COPY_CONSENSUS_MAX_SIZE", str(new_size + 20))
                    adjustments.append(f"共识金额{current_size}→{new_size}(提高单笔)")
                    logger.info(f"  🔧 提高共识金额: ${current_size}→${new_size} (提高单笔盈利)")
                    state._last_size_tune = time.time()

    if adjustments:
        logger.info(f"\n  📋 本次调优: {' | '.join(adjustments)}")
        state.tune_history.append({
            "time": datetime.now().isoformat(),
            "adjustments": adjustments,
        })
    else:
        logger.info(f"\n  ✅ 当前参数合理，无需调整")


# ============================================================
# 主循环
# ============================================================

async def check_and_adjust():
    """每5分钟检查: 分析engine_state + 执行5条管理规则 + 自动调整"""
    now_ts = time.time()
    actions = []

    # 优先从 engine_state.json 读取（更准确）
    engine = read_engine_state()
    if engine:
        metrics = analyze_engine_positions(engine)
    else:
        # 降级：从日志解析
        metrics = read_latest_metrics()

    if not metrics:
        logger.warning("无法读取引擎状态或日志")
        return

    total_pnl = metrics["total_pnl"]
    realized = metrics["realized"]
    floating = metrics["floating"]
    trades = metrics["trades"]

    logger.info(f"\n{'━'*60}")
    logger.info(f"📊 自动检查 @ {metrics['time']}")
    logger.info(f"{'─'*60}")
    logger.info(f"  持仓={metrics['positions']} 敞口=${metrics['cost']:.0f} "
                f"浮动=${floating:+.2f} 已实现=${realized:+.2f} 综合=${total_pnl:+.2f} "
                f"交易={trades}笔")

    # ── 新增: 详细成交记录 ──
    new_trades = parse_recent_trades(state.last_seen_trade_id)
    if new_trades:
        logger.info(f"\n  📝 新成交 ({len(new_trades)}笔):")
        logger.info(f"  {'时间':>8} | {'来源':<25} | {'方向':>4} | {'市场':<35} | {'金额':>7} | {'价格':>6} | {'延迟':>5}")
        logger.info(f"  {'─'*8} | {'─'*25} | {'─'*4} | {'─'*35} | {'─'*7} | {'─'*6} | {'─'*5}")
        for t in new_trades:
            logger.info(
                f"  {t['time'][-8:]:>8} | {t['source']:<25.25} | {t['direction']:>4} | "
                f"{t['market']:<35.35} | ${t['amount']:>5.0f} | ${t['my_price']:.3f} | {t['delay']:>4.0f}s"
            )
        state.last_seen_trade_id = max(t["id"] for t in new_trades)

    # ── 新增: 详细结算记录 ──
    new_settles = parse_recent_settlements(state.last_seen_settle_id)
    if new_settles:
        tp_list = [s for s in new_settles if s["type"] == "止盈"]
        sl_list = [s for s in new_settles if s["type"] == "止损"]
        tp_total = sum(s["pnl"] for s in tp_list)
        sl_total = sum(s["pnl"] for s in sl_list)
        logger.info(f"\n  💰 结算明细 ({len(new_settles)}笔: {len(tp_list)}止盈 {len(sl_list)}止损 | 止盈${tp_total:+.1f} 止损${sl_total:+.1f}):")
        logger.info(f"  {'时间':>8} | {'类型':>4} | {'市场':<30} | {'入场':>6} | {'出场':>6} | {'涨跌':>8} | {'盈亏':>8} | {'持仓':>6}")
        logger.info(f"  {'─'*8} | {'─'*4} | {'─'*30} | {'─'*6} | {'─'*6} | {'─'*8} | {'─'*8} | {'─'*6}")
        for s in new_settles:
            icon = "✅" if s["type"] == "止盈" else "❌"
            logger.info(
                f"  {s['time'][-8:]:>8} | {icon}{s['type']} | {s['market']:<30.30} | "
                f"${s['entry_price']:.3f} | ${s['exit_price']:.3f} | {s['change_pct']:>8} | "
                f"${s['pnl']:>+7.2f} | {s['hold_time']:>6}"
            )
        state.last_seen_settle_id = max(s["id"] for s in new_settles)

    # ── 规则 a: 移除全仓亏损钱包 ──
    if engine and "active_positions" in metrics:
        wallet_perf = analyze_wallet_performance(metrics["active_positions"])
        bad = rule_a_remove_bad_wallets(wallet_perf)
        if bad:
            names = [f"{b[0]}({b[1]}仓)" for b in bad]
            actions.append(f"移除钱包: {','.join(names)}")

    # ── 规则 b: PnL改善>$5 ──
    if state.last_pnl is not None:
        pnl_change = total_pnl - state.last_pnl
        if pnl_change > PNL_IMPROVE_THRESHOLD:
            logger.info(f"  🟢 规则b: PnL改善 ${pnl_change:+.2f} (${state.last_pnl:+.2f}→${total_pnl:+.2f})")
            actions.append(f"PnL改善${pnl_change:+.1f}")

    # ── 规则 c: PnL恶化>$10 → 触发分析+钱包刷新 ──
    if state.last_pnl is not None:
        pnl_change = total_pnl - state.last_pnl
        if pnl_change < PNL_WORSEN_THRESHOLD:
            logger.warning(f"  🔴 规则c: PnL恶化 ${pnl_change:.2f}! (${state.last_pnl:+.2f}→${total_pnl:+.2f})")
            actions.append(f"PnL恶化${pnl_change:.1f}")
            # 恶化严重时触发钱包刷新
            if pnl_change < -20:
                await refresh_wallets()
                actions.append("触发钱包刷新")

    # ── 规则 d: 已实现PnL趋势 ──
    if state.last_realized is not None:
        realized_change = realized - state.last_realized
        if realized_change > 0:
            logger.info(f"  ✓ 规则d: 已实现PnL好转 ${realized_change:+.2f}")
        elif realized_change < -5:
            logger.info(f"  ✗ 规则d: 已实现PnL下降 ${realized_change:+.2f}")

    # ── 规则 e: 30分钟无新交易 ──
    if state.last_trade_count > 0 and trades == state.last_trade_count:
        # 交易数未增长，检查时间
        if state.last_trade_time:
            idle_sec = now_ts - state.last_trade_time
            if idle_sec > NO_TRADE_TIMEOUT:
                logger.warning(f"  ⚠ 规则e: {idle_sec/60:.0f}分钟无新交易")
                actions.append(f"{idle_sec/60:.0f}min无交易")
                await refresh_wallets()
    else:
        state.last_trade_time = now_ts

    state.last_trade_count = trades

    # ── 每小时定期刷新钱包排行榜 ──
    if now_ts - state.last_wallet_refresh > 3600:
        await refresh_wallets()
        state.last_wallet_refresh = now_ts

    # ── 策略自动调优（每30分钟） ──
    if now_ts - state.last_tune_time > TUNE_INTERVAL:
        try:
            auto_tune_strategy()
        except Exception as e:
            logger.error(f"调优异常: {e}")
        state.last_tune_time = now_ts

    # ── 天气播报（每30分钟更新一次） ──
    if now_ts - state.last_weather_report > 1800:
        update_weather_report()
        state.last_weather_report = now_ts

    # ── 日报更新 ──
    generate_daily_report(metrics)

    # ── 更新状态并持久化 ──
    state.last_pnl = total_pnl
    state.last_realized = realized
    state.last_floating = floating
    state.save_persistent()

    if actions:
        logger.info(f"  📋 本次操作: {' | '.join(actions)}")
    else:
        logger.info(f"  ✅ 无需调整")


async def main():
    logger.info("=" * 60)
    logger.info("🚀 自动管理员启动 — 24小时无人值守")
    logger.info("=" * 60)
    logger.info(f"  引擎状态: {ENGINE_STATE}")
    logger.info(f"  跟单日志: {COPY_LOG}")
    logger.info(f"  管理状态: {MANAGER_STATE}")
    logger.info(f"  日报输出: {DAILY_REPORT}")
    logger.info(f"  检查间隔: {CHECK_INTERVAL}秒 ({CHECK_INTERVAL//60}分钟)")
    logger.info(f"  钱包刷新: 每小时 + PnL恶化>$20时")
    logger.info(f"  策略调优: 每{TUNE_INTERVAL//60}分钟自动分析盈亏+调参")
    logger.info(f"  管理规则: a)移除全亏钱包 b)PnL改善报告 c)PnL恶化分析 d)趋势追踪 e)活跃度监控 f)策略自动调优")
    logger.info(f"  代理: {PROXY}")
    if state.removed_wallets:
        logger.info(f"  已移除钱包: {len(state.removed_wallets)}个")

    # 初始化
    state.last_wallet_refresh = time.time()  # 首次延后1小时再刷新
    state.last_weather_report = 0  # 立即更新天气

    while True:
        try:
            await check_and_adjust()
        except Exception as e:
            logger.error(f"检查异常: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
