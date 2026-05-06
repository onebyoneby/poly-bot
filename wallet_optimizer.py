#!/usr/bin/env python3
"""
钱包优化器 — Polymarket 跟单钱包绩效分析与报告生成
====================================================
功能:
  1. 从 engine_state.json 读取交易历史，按钱包维度分析绩效
  2. 实时拉取排行榜数据交叉验证
  3. 识别高撤回/高亏损钱包，自动建议移除或降级
  4. 分析钱包共同性（市场重合、交易时段、方向偏好）
  5. 生成每日/每周 Markdown 分析报告

用法:
  python wallet_optimizer.py                # 完整分析 + 报告
  python wallet_optimizer.py --daily        # 仅日报
  python wallet_optimizer.py --weekly       # 仅周报
  python wallet_optimizer.py --optimize     # 分析 + 输出 .env 建议
  python wallet_optimizer.py --live-check   # 拉取排行榜交叉验证
"""
import asyncio
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── 路径 ─────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "logs" / "engine_state.json"
REPORT_DIR = BASE_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)

# ── 阈值配置 ─────────────────────────────────────────
# 钱包评估阈值
MIN_TRADES_FOR_EVAL = 3          # 至少 N 笔交易才评估
WIN_RATE_WARN = 0.35             # 胜率低于 35% 告警
WIN_RATE_CRITICAL = 0.20         # 胜率低于 20% 建议移除
LOSS_PCT_WARN = -15.0            # 平均亏损超 15% 告警
MAX_DRAWDOWN_WARN = -50.0        # 最大单笔回撤超 50% 告警
PNL_VOL_RATIO_ARB = 0.10         # PNL/Volume < 0.10 疑似套利
STOP_LOSS_RATE_WARN = 0.50       # 止损率超 50% 告警
IDLE_DAYS_WARN = 7               # 超过 7 天无信号的钱包


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据加载
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def load_state() -> dict:
    if not STATE_FILE.exists():
        print(f"[ERROR] 状态文件不存在: {STATE_FILE}")
        sys.exit(1)
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_trader_from_source(source: str) -> list[str]:
    """从 source 字段提取交易者名称"""
    m = re.search(r"跟单(\S+)", source)
    if m:
        return [m.group(1)]
    if "共识" in source:
        names = re.findall(r"[A-Za-z0-9_]+", source.replace("共识", ""))
        return names
    return []


def load_env_wallets() -> dict:
    """读取 .env 中所有 COPY_WALLET 配置（含注释掉的）"""
    env_file = BASE_DIR / ".env"
    wallets = {}  # name -> {address, tier, active}
    if not env_file.exists():
        return wallets
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            is_commented = line.startswith("#")
            clean = line.lstrip("# ").strip()
            m = re.match(r"COPY_WALLET_(T[12]_)?(\w+)=(0x[a-fA-F0-9]+)", clean)
            if m:
                tier_prefix = m.group(1) or ""
                name = m.group(2)
                addr = m.group(3).lower()
                tier = "T2" if "T2" in tier_prefix else "T1"
                wallets[name] = {
                    "address": addr,
                    "tier": tier,
                    "active": not is_commented,
                }
    return wallets


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 核心分析
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class WalletStats:
    """单个钱包的统计数据"""

    def __init__(self, name: str):
        self.name = name
        self.buys: list[dict] = []
        self.sells: list[dict] = []
        self.total_trades = 0
        self.total_buy_usd = 0.0
        self.total_sell_usd = 0.0
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.stop_losses = 0
        self.take_profits = 0
        self.pnl_list: list[float] = []      # 每笔卖出 PNL
        self.pnl_pct_list: list[float] = []   # 每笔卖出 PNL%
        self.hold_minutes: list[float] = []
        self.markets: set = set()
        self.trade_hours: list[int] = []
        self.first_trade: str = ""
        self.last_trade: str = ""
        self.delays: list[float] = []         # 信号延迟

    def add_trade(self, trade: dict):
        self.total_trades += 1
        trade_time = trade.get("time", "")
        if trade_time:
            if not self.first_trade or trade_time < self.first_trade:
                self.first_trade = trade_time
            if not self.last_trade or trade_time > self.last_trade:
                self.last_trade = trade_time
            try:
                hour = int(trade_time.split(" ")[1].split(":")[0])
                self.trade_hours.append(hour)
            except (IndexError, ValueError):
                pass

        market = trade.get("market", "")
        if market:
            self.markets.add(market)

        if trade["side"] == "BUY":
            self.buys.append(trade)
            self.total_buy_usd += trade.get("size_usd", 0)
            delay = trade.get("delay_s")
            if delay is not None:
                self.delays.append(delay)
        elif "SELL" in trade["side"]:
            self.sells.append(trade)
            pnl = trade.get("pnl", 0)
            pnl_pct = trade.get("pnl_pct", 0)
            self.realized_pnl += pnl
            self.pnl_list.append(pnl)
            self.pnl_pct_list.append(pnl_pct)
            hold = trade.get("hold_min", 0)
            if hold:
                self.hold_minutes.append(hold)
            if pnl > 0:
                self.wins += 1
            else:
                self.losses += 1
            if "止损" in trade["side"]:
                self.stop_losses += 1
            elif "止盈" in trade["side"]:
                self.take_profits += 1

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0

    @property
    def stop_loss_rate(self) -> float:
        total = len(self.sells)
        return self.stop_losses / total if total > 0 else 0

    @property
    def avg_pnl(self) -> float:
        return sum(self.pnl_list) / len(self.pnl_list) if self.pnl_list else 0

    @property
    def avg_pnl_pct(self) -> float:
        return sum(self.pnl_pct_list) / len(self.pnl_pct_list) if self.pnl_pct_list else 0

    @property
    def max_loss_pct(self) -> float:
        return min(self.pnl_pct_list) if self.pnl_pct_list else 0

    @property
    def max_win_pct(self) -> float:
        return max(self.pnl_pct_list) if self.pnl_pct_list else 0

    @property
    def avg_hold_min(self) -> float:
        return sum(self.hold_minutes) / len(self.hold_minutes) if self.hold_minutes else 0

    @property
    def avg_delay(self) -> float:
        return sum(self.delays) / len(self.delays) if self.delays else 0

    @property
    def market_count(self) -> int:
        return len(self.markets)

    @property
    def days_since_last(self) -> float:
        if not self.last_trade:
            return 999
        try:
            last = datetime.strptime(self.last_trade, "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - last).total_seconds() / 86400
        except ValueError:
            return 999

    def score(self) -> float:
        """综合评分（0-100），越高越好"""
        s = 50.0  # 基础分

        # 胜率加减分 (±20)
        if self.wins + self.losses >= MIN_TRADES_FOR_EVAL:
            s += (self.win_rate - 0.5) * 40

        # 总 PNL 加减分 (±15)
        s += max(-15, min(15, self.realized_pnl * 3))

        # 止损率扣分 (最多 -15)
        s -= self.stop_loss_rate * 15

        # 市场多元化加分 (最多 +10)
        s += min(10, self.market_count * 2)

        # 交易活跃度 (最多 +10)
        if self.days_since_last < 1:
            s += 10
        elif self.days_since_last < 3:
            s += 5

        return max(0, min(100, s))


def analyze_wallets(state: dict) -> dict[str, WalletStats]:
    """按钱包维度分析所有交易。
    SELL 记录通常无 source 字段，需通过 market 关联回 BUY 的 source。
    同时也用 positions 中的 source 做补充关联。
    """
    stats: dict[str, WalletStats] = {}

    trades = state.get("trade_history", [])
    positions = state.get("positions", {})

    # 建立 market -> trader 映射（从 BUY 记录 + 当前持仓）
    market_to_trader: dict[str, str] = {}
    for trade in trades:
        if trade["side"] == "BUY":
            source = trade.get("source", "")
            traders = parse_trader_from_source(source)
            market = trade.get("market", "")
            if traders and market:
                market_to_trader[market] = traders[0]
    for pos in positions.values():
        source = pos.get("source", "")
        traders = parse_trader_from_source(source)
        market_name = pos.get("market_name", "")[:60]
        if traders and market_name:
            market_to_trader[market_name] = traders[0]

    for trade in trades:
        source = trade.get("source", "")
        traders = parse_trader_from_source(source)

        # SELL 无 source 时，通过 market 关联
        if not traders and "SELL" in trade.get("side", ""):
            market = trade.get("market", "")
            matched = market_to_trader.get(market)
            if matched:
                traders = [matched]

        for name in traders:
            if name not in stats:
                stats[name] = WalletStats(name)
            stats[name].add_trade(trade)

    return stats


def analyze_positions(state: dict) -> dict[str, dict]:
    """分析当前持仓按钱包分组"""
    wallet_positions = defaultdict(lambda: {"count": 0, "total_usd": 0, "unrealized_pnl": 0, "markets": set()})
    for pos_key, pos in state.get("positions", {}).items():
        source = pos.get("source", "")
        traders = parse_trader_from_source(source)
        for name in traders:
            wp = wallet_positions[name]
            wp["count"] += 1
            wp["total_usd"] += pos.get("size_usd", 0)
            entry = pos.get("entry_price", 0)
            current = pos.get("current_price", 0)
            shares = pos.get("shares", 0)
            wp["unrealized_pnl"] += (current - entry) * shares
            wp["markets"].add(pos.get("market_name", "")[:40])
    return dict(wallet_positions)


def find_commonalities(stats: dict[str, WalletStats]) -> dict:
    """分析钱包间的共同性"""
    # 市场重合度
    market_counter = Counter()
    wallet_markets = {}
    for name, ws in stats.items():
        wallet_markets[name] = ws.markets
        for m in ws.markets:
            market_counter[m] += 1

    # 多人参与的市场
    shared_markets = {m: c for m, c in market_counter.items() if c >= 2}

    # 交易时段分布
    hour_dist = Counter()
    for ws in stats.values():
        for h in ws.trade_hours:
            hour_dist[h] += 1

    # 方向偏好（买入价格分布 → 高确定性 vs 低赔率）
    high_prob_wallets = []  # 买入价 > 0.8
    low_prob_wallets = []   # 买入价 < 0.5
    for name, ws in stats.items():
        prices = [t.get("price", 0.5) for t in ws.buys if t.get("price")]
        if prices:
            avg_price = sum(prices) / len(prices)
            if avg_price > 0.80:
                high_prob_wallets.append((name, avg_price))
            elif avg_price < 0.50:
                low_prob_wallets.append((name, avg_price))

    # 钱包间市场重合矩阵
    overlap_pairs = []
    names = list(wallet_markets.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            n1, n2 = names[i], names[j]
            common = wallet_markets[n1] & wallet_markets[n2]
            if common:
                overlap_pairs.append((n1, n2, len(common), common))

    return {
        "shared_markets": shared_markets,
        "hour_distribution": dict(sorted(hour_dist.items())),
        "high_prob_wallets": sorted(high_prob_wallets, key=lambda x: -x[1]),
        "low_prob_wallets": sorted(low_prob_wallets, key=lambda x: x[1]),
        "overlap_pairs": sorted(overlap_pairs, key=lambda x: -x[2]),
    }


def generate_recommendations(
    stats: dict[str, WalletStats],
    positions: dict[str, dict],
    env_wallets: dict,
) -> list[dict]:
    """生成钱包优化建议"""
    recs = []
    for name, ws in stats.items():
        issues = []
        action = "保持"
        severity = "OK"

        sell_count = ws.wins + ws.losses

        if ws.total_trades < MIN_TRADES_FOR_EVAL:
            if ws.days_since_last > IDLE_DAYS_WARN:
                issues.append(f"交易过少({ws.total_trades}笔)且{ws.days_since_last:.0f}天无活动")
                action = "观察"
                severity = "WARN"
            continue  # 数据不足，跳过详细评估

        # 无卖出记录 → 纯持仓中，不做胜率评估
        if sell_count < MIN_TRADES_FOR_EVAL:
            if ws.total_trades >= 5:
                issues.append(f"仅{sell_count}笔卖出，持仓观察中")
            continue

        # 胜率检查
        if ws.win_rate < WIN_RATE_CRITICAL:
            issues.append(f"胜率极低: {ws.win_rate:.0%}")
            action = "移除"
            severity = "CRITICAL"
        elif ws.win_rate < WIN_RATE_WARN:
            issues.append(f"胜率偏低: {ws.win_rate:.0%}")
            if action != "移除":
                action = "降级T2"
                severity = "WARN"

        # 止损率检查
        if ws.stop_loss_rate > STOP_LOSS_RATE_WARN:
            issues.append(f"止损率过高: {ws.stop_loss_rate:.0%}")
            if severity != "CRITICAL":
                action = "降级T2"
                severity = "WARN"

        # 最大回撤
        if ws.max_loss_pct < MAX_DRAWDOWN_WARN:
            issues.append(f"最大单笔回撤: {ws.max_loss_pct:.1f}%")
            if severity != "CRITICAL":
                severity = "WARN"

        # 总 PNL 负数
        if ws.realized_pnl < -10:
            issues.append(f"累计亏损: ${ws.realized_pnl:.2f}")
            if severity == "OK":
                action = "观察"
                severity = "WARN"

        # 持仓敞口
        pos_info = positions.get(name, {})
        unrealized = pos_info.get("unrealized_pnl", 0)
        if unrealized < -20:
            issues.append(f"未实现亏损: ${unrealized:.2f}")

        if not issues:
            issues.append("表现正常")

        # 当前 tier
        env_info = env_wallets.get(name, {})
        current_tier = env_info.get("tier", "?")
        is_active = env_info.get("active", False)

        recs.append({
            "name": name,
            "score": ws.score(),
            "action": action,
            "severity": severity,
            "issues": issues,
            "current_tier": current_tier,
            "active": is_active,
            "win_rate": ws.win_rate,
            "pnl": ws.realized_pnl,
            "trades": ws.total_trades,
            "stop_loss_rate": ws.stop_loss_rate,
        })

    return sorted(recs, key=lambda x: x["score"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 排行榜交叉验证
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def fetch_leaderboard_data() -> dict:
    """拉取日/周/月排行榜 Top 50"""
    from market_client import MarketClient
    client = MarketClient()
    result = {}
    try:
        for period in ["DAY", "WEEK", "MONTH"]:
            data = await client.get_leaderboard(time_period=period, limit=50)
            entries = {}
            for i, entry in enumerate(data, 1):
                name = entry.get("userName") or entry.get("proxyWallet", "")[:10]
                entries[name.lower()] = {
                    "rank": i,
                    "name": name,
                    "pnl": float(entry.get("pnl", 0)),
                    "volume": float(entry.get("vol", 0)),
                    "wallet": entry.get("proxyWallet", "").lower(),
                }
            result[period] = entries
    finally:
        await client.close()
    return result


def cross_reference_leaderboard(
    env_wallets: dict, leaderboard: dict
) -> list[dict]:
    """将 .env 钱包与排行榜交叉对比"""
    results = []
    for name, info in env_wallets.items():
        if not info["active"]:
            continue
        addr = info["address"]
        appearances = {}
        for period, entries in leaderboard.items():
            # 按地址或名称匹配
            for lb_name, lb_info in entries.items():
                if lb_info["wallet"] == addr or lb_name == name.lower():
                    appearances[period] = lb_info
                    break
        pnl_vol_ratio = None
        if "DAY" in appearances and appearances["DAY"]["volume"] > 0:
            pnl_vol_ratio = appearances["DAY"]["pnl"] / appearances["DAY"]["volume"]

        results.append({
            "name": name,
            "tier": info["tier"],
            "on_daily": "DAY" in appearances,
            "on_weekly": "WEEK" in appearances,
            "on_monthly": "MONTH" in appearances,
            "daily_rank": appearances.get("DAY", {}).get("rank"),
            "daily_pnl": appearances.get("DAY", {}).get("pnl"),
            "daily_vol": appearances.get("DAY", {}).get("volume"),
            "pnl_vol_ratio": pnl_vol_ratio,
            "suspected_arb": pnl_vol_ratio is not None and pnl_vol_ratio < PNL_VOL_RATIO_ARB,
        })
    return sorted(results, key=lambda x: (not x["on_daily"], -(x["daily_pnl"] or 0)))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 报告生成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def generate_report(
    stats: dict[str, WalletStats],
    positions: dict[str, dict],
    env_wallets: dict,
    commonalities: dict,
    recommendations: list[dict],
    leaderboard_cross: list[dict] | None = None,
    report_type: str = "daily",
) -> str:
    """生成 Markdown 分析报告"""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    title = "每日" if report_type == "daily" else "每周"

    lines = [
        f"# Polymarket 跟单钱包{title}分析报告",
        f"",
        f"**生成时间**: {date_str} {time_str}",
        f"**报告类型**: {title}分析",
        f"**活跃钱包数**: {sum(1 for w in env_wallets.values() if w['active'])}",
        f"**总交易笔数**: {sum(ws.total_trades for ws in stats.values())}",
        f"**总实现PNL**: ${sum(ws.realized_pnl for ws in stats.values()):.2f}",
        f"",
        "---",
        "",
    ]

    # ── 1. 钱包绩效排名 ──
    lines.append("## 1. 钱包绩效排名")
    lines.append("")
    lines.append("| 排名 | 钱包 | Tier | 评分 | 交易数 | 胜率 | 止损率 | 实现PNL | 最大回撤 | 状态 |")
    lines.append("|------|------|------|------|--------|------|--------|---------|----------|------|")

    sorted_stats = sorted(stats.items(), key=lambda x: -x[1].score())
    for i, (name, ws) in enumerate(sorted_stats, 1):
        env_info = env_wallets.get(name, {})
        tier = env_info.get("tier", "?")
        active = "✅" if env_info.get("active", False) else "❌"
        sell_count = ws.wins + ws.losses
        wr = f"{ws.win_rate:.0%}" if sell_count >= MIN_TRADES_FOR_EVAL else "N/A"
        slr = f"{ws.stop_loss_rate:.0%}" if sell_count >= MIN_TRADES_FOR_EVAL else "N/A"
        lines.append(
            f"| {i} | {name} | {tier} | {ws.score():.0f} | {ws.total_trades} | "
            f"{wr} | {slr} | ${ws.realized_pnl:+.2f} | {ws.max_loss_pct:.1f}% | {active} |"
        )
    lines.append("")

    # ── 2. 当前持仓分布 ──
    lines.append("## 2. 当前持仓分布")
    lines.append("")
    lines.append("| 钱包 | 持仓数 | 总敞口 | 未实现PNL | 参与市场数 |")
    lines.append("|------|--------|--------|-----------|-----------|")
    sorted_pos = sorted(positions.items(), key=lambda x: -x[1]["count"])
    for name, pos_info in sorted_pos:
        markets_count = len(pos_info.get("markets", set()))
        lines.append(
            f"| {name} | {pos_info['count']} | ${pos_info['total_usd']:.0f} | "
            f"${pos_info['unrealized_pnl']:+.2f} | {markets_count} |"
        )
    lines.append("")

    # ── 3. 优化建议 ──
    lines.append("## 3. 优化建议")
    lines.append("")
    critical = [r for r in recommendations if r["severity"] == "CRITICAL"]
    warns = [r for r in recommendations if r["severity"] == "WARN"]
    if critical:
        lines.append("### 🔴 建议移除")
        for r in critical:
            lines.append(f"- **{r['name']}** (当前{r['current_tier']}): {', '.join(r['issues'])}")
        lines.append("")
    if warns:
        lines.append("### 🟡 建议降级/观察")
        for r in warns:
            lines.append(f"- **{r['name']}** ({r['action']}): {', '.join(r['issues'])}")
        lines.append("")
    if not critical and not warns:
        lines.append("✅ 所有钱包表现正常，无需调整。")
        lines.append("")

    # ── 4. 钱包共同性分析 ──
    lines.append("## 4. 钱包共同性分析")
    lines.append("")

    # 4a. 交易时段
    lines.append("### 4.1 交易时段热力图")
    lines.append("```")
    hour_dist = commonalities.get("hour_distribution", {})
    max_count = max(hour_dist.values()) if hour_dist else 1
    for h in range(24):
        count = hour_dist.get(h, 0)
        bar = "█" * int(count / max_count * 30) if max_count > 0 else ""
        lines.append(f"  {h:02d}:00  {bar} ({count})")
    lines.append("```")
    lines.append("")

    # 4b. 方向偏好
    lines.append("### 4.2 方向偏好")
    high_prob = commonalities.get("high_prob_wallets", [])
    low_prob = commonalities.get("low_prob_wallets", [])
    if high_prob:
        lines.append("**高确定性偏好 (买入均价>0.80)** — 薄利多单型:")
        for name, avg_p in high_prob:
            lines.append(f"  - {name}: 均价 {avg_p:.3f}")
    if low_prob:
        lines.append("**低概率偏好 (买入均价<0.50)** — 高赔率博弈型:")
        for name, avg_p in low_prob:
            lines.append(f"  - {name}: 均价 {avg_p:.3f}")
    lines.append("")

    # 4c. 市场重合
    lines.append("### 4.3 多钱包共同参与的市场")
    shared = commonalities.get("shared_markets", {})
    if shared:
        for market, count in sorted(shared.items(), key=lambda x: -x[1])[:15]:
            lines.append(f"  - [{count}人] {market}")
    else:
        lines.append("  暂无多人共同参与的市场")
    lines.append("")

    # 4d. 钱包重合
    overlap = commonalities.get("overlap_pairs", [])
    if overlap:
        lines.append("### 4.4 钱包间市场重合度")
        lines.append("| 钱包A | 钱包B | 重合市场数 |")
        lines.append("|-------|-------|-----------|")
        for n1, n2, cnt, _ in overlap[:10]:
            lines.append(f"| {n1} | {n2} | {cnt} |")
        lines.append("")

    # ── 5. 排行榜交叉验证 ──
    if leaderboard_cross:
        lines.append("## 5. 排行榜交叉验证")
        lines.append("")
        lines.append("| 钱包 | Tier | 日榜 | 周榜 | 月榜 | 日PNL | 日Volume | PNL/Vol | 套利嫌疑 |")
        lines.append("|------|------|------|------|------|-------|----------|---------|---------|")
        for r in leaderboard_cross:
            daily_r = f"#{r['daily_rank']}" if r["daily_rank"] else "-"
            dpnl = f"${r['daily_pnl']:,.0f}" if r["daily_pnl"] is not None else "-"
            dvol = f"${r['daily_vol']:,.0f}" if r["daily_vol"] is not None else "-"
            ratio = f"{r['pnl_vol_ratio']:.2f}" if r["pnl_vol_ratio"] is not None else "-"
            arb = "⚠️" if r["suspected_arb"] else "✅"
            lines.append(
                f"| {r['name']} | {r['tier']} | "
                f"{'✅' if r['on_daily'] else '❌'}{daily_r} | "
                f"{'✅' if r['on_weekly'] else '❌'} | "
                f"{'✅' if r['on_monthly'] else '❌'} | "
                f"{dpnl} | {dvol} | {ratio} | {arb} |"
            )
        lines.append("")

    # ── 6. .env 钱包覆盖率 ──
    lines.append("## 6. .env 钱包覆盖率")
    lines.append("")
    active_wallets = {n: w for n, w in env_wallets.items() if w["active"]}
    traded_names = set(stats.keys())
    has_data = {n for n in active_wallets if n in traded_names}
    no_data = {n for n in active_wallets if n not in traded_names}
    extra = traded_names - set(active_wallets.keys())

    lines.append(f"- 活跃钱包: **{len(active_wallets)}** 个")
    lines.append(f"- 有交易数据: **{len(has_data)}** 个 ({len(has_data)/max(len(active_wallets),1):.0%})")
    lines.append(f"- 无交易数据: **{len(no_data)}** 个 ({len(no_data)/max(len(active_wallets),1):.0%})")
    lines.append("")

    if no_data:
        lines.append("**无交易数据的钱包**（轮询正常但未发出信号）:")
        for n in sorted(no_data):
            tier = active_wallets[n]["tier"]
            lines.append(f"  - {n} ({tier})")
        lines.append("")

    if extra:
        lines.append("**有历史但已从 .env 移除的钱包**:")
        for n in sorted(extra):
            lines.append(f"  - {n}")
        lines.append("")

    # ── 7. 关键指标摘要 ──
    lines.append("## 7. 关键指标摘要")
    lines.append("")
    total_pnl = sum(ws.realized_pnl for ws in stats.values())
    total_wins = sum(ws.wins for ws in stats.values())
    total_losses = sum(ws.losses for ws in stats.values())
    total_stop = sum(ws.stop_losses for ws in stats.values())
    total_tp = sum(ws.take_profits for ws in stats.values())
    total_sell = total_wins + total_losses
    overall_wr = total_wins / total_sell if total_sell > 0 else 0

    lines.append(f"- **总实现PNL**: ${total_pnl:+.2f}")
    lines.append(f"- **总胜率**: {overall_wr:.0%} ({total_wins}胜 / {total_losses}负)")
    lines.append(f"- **止盈/止损比**: {total_tp}:{total_stop}")
    lines.append(f"- **止损率**: {total_stop / total_sell:.0%}" if total_sell > 0 else "- **止损率**: N/A")
    lines.append(f"- **平均持仓时间**: {sum(ws.avg_hold_min for ws in stats.values()) / max(len(stats), 1):.1f} 分钟")
    avg_delay = [ws.avg_delay for ws in stats.values() if ws.avg_delay > 0]
    if avg_delay:
        lines.append(f"- **平均信号延迟**: {sum(avg_delay) / len(avg_delay):.1f} 秒")

    # 当前持仓汇总
    total_pos_count = sum(p["count"] for p in positions.values())
    total_pos_usd = sum(p["total_usd"] for p in positions.values())
    total_unrealized = sum(p["unrealized_pnl"] for p in positions.values())
    lines.append(f"- **当前持仓**: {total_pos_count} 笔, 总敞口 ${total_pos_usd:.0f}")
    lines.append(f"- **未实现PNL**: ${total_unrealized:+.2f}")
    lines.append("")

    lines.append("---")
    lines.append(f"*报告由 wallet_optimizer.py 自动生成 | {date_str} {time_str}*")

    return "\n".join(lines)


def generate_env_suggestions(recommendations: list[dict]) -> str:
    """生成 .env 修改建议"""
    lines = ["# ── 钱包优化建议 ──", "# 以下为 wallet_optimizer 自动生成的 .env 调整建议", ""]
    changes = [r for r in recommendations if r["action"] in ("移除", "降级T2")]
    if not changes:
        lines.append("# ✅ 无需调整")
        return "\n".join(lines)

    for r in changes:
        if r["action"] == "移除":
            lines.append(f"# 🔴 移除 {r['name']}: {', '.join(r['issues'])}")
            lines.append(f"# COPY_WALLET_{r['name']}=... → 注释掉")
        elif r["action"] == "降级T2":
            lines.append(f"# 🟡 降级 {r['name']}: {', '.join(r['issues'])}")
            lines.append(f"# COPY_WALLET_{r['name']} → COPY_WALLET_T2_{r['name']}")
        lines.append("")
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 终端输出（Rich 表格）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def print_console_summary(
    stats: dict[str, WalletStats],
    recommendations: list[dict],
    positions: dict[str, dict],
):
    """在终端输出彩色汇总"""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
    except ImportError:
        # fallback 纯文本
        print("\n=== 钱包绩效排名 ===")
        for name, ws in sorted(stats.items(), key=lambda x: -x[1].score()):
            print(f"  {name:25s} 评分:{ws.score():5.0f}  PNL:${ws.realized_pnl:+7.2f}  胜率:{ws.win_rate:.0%}")
        return

    console = Console()

    # 绩效表
    table = Table(title="钱包绩效排名", show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("钱包", style="cyan", width=25)
    table.add_column("评分", style="bold", width=5)
    table.add_column("交易", width=5)
    table.add_column("胜率", width=6)
    table.add_column("止损率", width=6)
    table.add_column("实现PNL", width=10)
    table.add_column("最大回撤", width=8)
    table.add_column("持仓数", width=6)
    table.add_column("建议", width=10)

    sorted_stats = sorted(stats.items(), key=lambda x: -x[1].score())
    for i, (name, ws) in enumerate(sorted_stats, 1):
        sell_count = ws.wins + ws.losses
        wr = f"{ws.win_rate:.0%}" if sell_count >= MIN_TRADES_FOR_EVAL else "N/A"
        slr = f"{ws.stop_loss_rate:.0%}" if sell_count >= MIN_TRADES_FOR_EVAL else "N/A"
        pos_count = positions.get(name, {}).get("count", 0)

        rec = next((r for r in recommendations if r["name"] == name), None)
        action = rec["action"] if rec else "保持"
        action_style = {"移除": "[red bold]移除[/]", "降级T2": "[yellow]降级T2[/]", "观察": "[yellow]观察[/]"}.get(action, "[green]保持[/]")

        score = ws.score()
        score_style = "green" if score >= 60 else ("yellow" if score >= 40 else "red")
        pnl_style = "green" if ws.realized_pnl >= 0 else "red"

        table.add_row(
            str(i), name, f"[{score_style}]{score:.0f}[/]",
            str(ws.total_trades), wr, slr,
            f"[{pnl_style}]${ws.realized_pnl:+.2f}[/]",
            f"{ws.max_loss_pct:.1f}%",
            str(pos_count), action_style,
        )

    console.print(table)

    # 建议面板
    critical = [r for r in recommendations if r["severity"] == "CRITICAL"]
    warns = [r for r in recommendations if r["severity"] == "WARN"]
    if critical or warns:
        msg_parts = []
        for r in critical:
            msg_parts.append(f"[red bold]🔴 移除 {r['name']}[/]: {', '.join(r['issues'])}")
        for r in warns:
            msg_parts.append(f"[yellow]🟡 {r['action']} {r['name']}[/]: {', '.join(r['issues'])}")
        console.print(Panel("\n".join(msg_parts), title="优化建议", border_style="yellow"))
    else:
        console.print(Panel("[green]✅ 所有钱包表现正常[/]", title="优化建议", border_style="green"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def main():
    parser = argparse.ArgumentParser(description="Polymarket 跟单钱包优化器")
    parser.add_argument("--daily", action="store_true", help="生成日报")
    parser.add_argument("--weekly", action="store_true", help="生成周报")
    parser.add_argument("--optimize", action="store_true", help="输出 .env 优化建议")
    parser.add_argument("--live-check", action="store_true", help="拉取排行榜交叉验证")
    parser.add_argument("--no-console", action="store_true", help="不输出终端表格")
    args = parser.parse_args()

    # 默认：全部执行
    if not any([args.daily, args.weekly, args.optimize, args.live_check]):
        args.daily = True
        args.optimize = True
        args.live_check = True

    print("📊 加载数据...")
    state = load_state()
    env_wallets = load_env_wallets()

    print(f"  交易历史: {len(state.get('trade_history', []))} 笔")
    print(f"  当前持仓: {len(state.get('positions', {}))} 个")
    print(f"  .env 钱包: {len(env_wallets)} 个 ({sum(1 for w in env_wallets.values() if w['active'])} 活跃)")

    # 分析
    print("\n🔍 分析钱包绩效...")
    stats = analyze_wallets(state)
    positions = analyze_positions(state)
    commonalities = find_commonalities(stats)
    recommendations = generate_recommendations(stats, positions, env_wallets)

    # 终端输出
    if not args.no_console:
        print_console_summary(stats, recommendations, positions)

    # 排行榜交叉验证
    leaderboard_cross = None
    if args.live_check:
        print("\n🌐 拉取排行榜数据...")
        try:
            leaderboard = await fetch_leaderboard_data()
            leaderboard_cross = cross_reference_leaderboard(env_wallets, leaderboard)
            print(f"  日榜命中: {sum(1 for r in leaderboard_cross if r['on_daily'])} 个")
            print(f"  周榜命中: {sum(1 for r in leaderboard_cross if r['on_weekly'])} 个")
            print(f"  月榜命中: {sum(1 for r in leaderboard_cross if r['on_monthly'])} 个")
            arb_count = sum(1 for r in leaderboard_cross if r["suspected_arb"])
            if arb_count:
                print(f"  ⚠️ 疑似套利: {arb_count} 个")
        except Exception as e:
            print(f"  [WARN] 排行榜拉取失败: {e}")

    # 生成报告
    report_type = "weekly" if args.weekly else "daily"
    date_str = datetime.now().strftime("%Y-%m-%d")

    if args.daily or args.weekly:
        print(f"\n📝 生成{('周' if args.weekly else '日')}报...")
        report = generate_report(
            stats, positions, env_wallets, commonalities,
            recommendations, leaderboard_cross, report_type,
        )
        filename = f"wallet_report_{report_type}_{date_str}.md"
        report_path = REPORT_DIR / filename
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"  ✅ 报告已保存: {report_path}")

    if args.optimize:
        print("\n💡 .env 优化建议:")
        suggestions = generate_env_suggestions(recommendations)
        print(suggestions)

    print("\n✅ 分析完成")


if __name__ == "__main__":
    asyncio.run(main())
