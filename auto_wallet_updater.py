#!/usr/bin/env python3
"""
钱包自动更新器 — 每4小时自动优化 .env 跟单钱包列表
====================================================
根据历史操作推断的规则：

【新增规则】
  - 月+周+日三榜 + 套利<10%           → T1
  - 月+周双榜   + 套利<10%            → T1
  - 单榜Top20   + 套利<5% + PNL/Vol>0.3 → T1
  - 日榜Top50   + PNL/Vol<0.10        → T2（共识）

【移除规则】
  - 套利占比 > 30%（从交易历史检测）    → 注释移除
  - 实现PNL < -$3                      → 注释移除
  - 止损率 = 100% 且有3+笔卖出         → 注释移除
  - PNL/Vol < 0.10（排行榜数据）       → 降级T2

【保护规则】
  - 无卖出记录 → 不做移除判断，继续观察
  - 手动注释带日期的 → 不自动恢复（尊重手动决策）

用法:
  python auto_wallet_updater.py           # 立即执行一次
  python auto_wallet_updater.py --daemon  # 每4小时循环执行
  python auto_wallet_updater.py --dry-run # 只分析不修改 .env
"""
import asyncio
import argparse
import json
import os
import re
import shutil
import time
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── 路径 ─────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "logs" / "engine_state.json"
ENV_FILE = BASE_DIR / ".env"
REPORT_DIR = BASE_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── 日志 ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WalletUpdater] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "wallet_updater.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("WalletUpdater")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 规则阈值（基于用户历史操作推断）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 新增
ADD_ARB_MAX_MULTI_BOARD = 0.10       # 多榜上榜: 套利<10% 可新增
ADD_ARB_MAX_SINGLE_BOARD = 0.05      # 单榜: 套利<5%
ADD_PNL_VOL_MIN_T1 = 0.30           # PNL/Vol>0.3 → T1
ADD_PNL_VOL_MIN_T2 = 0.0            # 只要上榜就可以做 T2
ADD_PNL_VOL_ARB_THRESHOLD = 0.10    # PNL/Vol<0.1 → T2 而非 T1
ADD_MIN_DAILY_PNL = 15000           # 日榜PNL最低门槛
ADD_MAX_WALLETS = 50                # 活跃钱包上限

# 移除
REMOVE_ARB_THRESHOLD = 0.30         # 套利>30% → 移除
REMOVE_PNL_THRESHOLD = -3.0         # 实现PNL<-$3 → 移除
REMOVE_STOP_LOSS_RATE = 1.0         # 止损率100% → 移除
REMOVE_MIN_SELLS = 3                # 至少N笔卖出才做移除判断

# 降级
DOWNGRADE_PNL_VOL = 0.10            # PNL/Vol<0.1 → 降级T2

# 周期
UPDATE_INTERVAL_HOURS = 4


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# .env 读写
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def read_env_lines() -> list[str]:
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        return f.readlines()


def parse_env_wallets(lines: list[str]) -> dict:
    """解析 .env 中所有 COPY_WALLET 行（含注释的）"""
    wallets = {}
    for i, line in enumerate(lines):
        raw = line.strip()
        is_commented = raw.startswith("#")
        clean = raw.lstrip("# ").strip()
        m = re.match(r"COPY_WALLET_(T[12]_)?(\w+)=(0x[a-fA-F0-9]+)", clean)
        if m:
            tier_prefix = m.group(1) or ""
            name = m.group(2)
            addr = m.group(3).lower()
            tier = "T2" if "T2" in tier_prefix else "T1"
            # 检查是否有手动移除日期标记
            has_date_comment = bool(re.search(r"202[5-9]-\d{2}-\d{2}", raw))
            wallets[name] = {
                "address": addr,
                "tier": tier,
                "active": not is_commented,
                "line_idx": i,
                "has_date_comment": has_date_comment,
                "original_line": raw,
            }
    return wallets


def write_env_update(lines: list[str], changes: list[dict]) -> list[str]:
    """应用变更到 .env 行列表"""
    new_lines = list(lines)

    for change in changes:
        action = change["action"]
        name = change["name"]
        line_idx = change.get("line_idx")

        if action == "comment_out" and line_idx is not None:
            # 注释掉一行
            old = new_lines[line_idx].rstrip("\n")
            if not old.strip().startswith("#"):
                reason = change.get("reason", "")
                date = datetime.now().strftime("%Y-%m-%d")
                new_lines[line_idx] = f"# {old.strip()}       # {reason} ({date}移除)\n"

        elif action == "downgrade_t2" and line_idx is not None:
            # T1 → T2
            old = new_lines[line_idx].rstrip("\n")
            if "COPY_WALLET_T2_" not in old and not old.strip().startswith("#"):
                new_line = re.sub(r"COPY_WALLET_(\w+)=", r"COPY_WALLET_T2_\1=", old)
                reason = change.get("reason", "")
                date = datetime.now().strftime("%Y-%m-%d")
                new_lines[line_idx] = f"{new_line}  # 自动降级T2: {reason} ({date})\n"

        elif action == "add":
            # 新增钱包 — 在钱包区域末尾追加
            addr = change["address"]
            tier = change.get("tier", "T1")
            reason = change.get("reason", "")
            date = datetime.now().strftime("%Y-%m-%d")
            prefix = "COPY_WALLET_T2_" if tier == "T2" else "COPY_WALLET_"
            new_line = f"{prefix}{name}={addr}  # 自动新增{tier}: {reason} ({date})\n"
            # 找到钱包区域末尾（在 "实时跟单参数" 之前插入）
            insert_idx = len(new_lines)
            for idx, l in enumerate(new_lines):
                if "实时跟单参数" in l:
                    insert_idx = idx - 1
                    break
            new_lines.insert(insert_idx, new_line)

    return new_lines


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 交易数据分析
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def analyze_copy_performance(state: dict) -> dict:
    """分析每个钱包的跟单绩效"""
    trades = state.get("trade_history", [])
    positions = state.get("positions", {})

    # market → trader 映射
    market_to_trader = {}
    for t in trades:
        if t["side"] == "BUY":
            m = re.search(r"跟单(\S+)", t.get("source", ""))
            if m:
                market_to_trader[t.get("market", "")] = m.group(1)
    for pos in positions.values():
        m = re.search(r"跟单(\S+)", pos.get("source", ""))
        if m:
            market_to_trader[pos.get("market_name", "")[:60]] = m.group(1)

    stats = defaultdict(lambda: {
        "buys": 0, "sells": 0, "wins": 0, "losses": 0,
        "stop_losses": 0, "take_profits": 0,
        "realized_pnl": 0.0, "pnl_list": [],
        "positions": 0, "unrealized_pnl": 0.0,
    })

    for t in trades:
        src = t.get("source", "")
        m = re.search(r"跟单(\S+)", src)
        traders = [m.group(1)] if m else []
        if not traders and "SELL" in t.get("side", ""):
            matched = market_to_trader.get(t.get("market", ""))
            if matched:
                traders = [matched]
        for name in traders:
            s = stats[name]
            if t["side"] == "BUY":
                s["buys"] += 1
            elif "SELL" in t["side"]:
                s["sells"] += 1
                pnl = t.get("pnl", 0)
                s["realized_pnl"] += pnl
                s["pnl_list"].append(pnl)
                if pnl > 0:
                    s["wins"] += 1
                else:
                    s["losses"] += 1
                if "止损" in t["side"]:
                    s["stop_losses"] += 1
                elif "止盈" in t["side"]:
                    s["take_profits"] += 1

    # 当前持仓
    for pos in positions.values():
        m = re.search(r"跟单(\S+)", pos.get("source", ""))
        if m:
            name = m.group(1)
            s = stats[name]
            s["positions"] += 1
            entry = pos.get("entry_price", 0)
            current = pos.get("current_price", 0)
            shares = pos.get("shares", 0)
            s["unrealized_pnl"] += (current - entry) * shares

    return dict(stats)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 排行榜 + 套利检测
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def fetch_leaderboards(client) -> dict:
    """拉取日/周/月排行榜"""
    result = {}
    for period in ["DAY", "WEEK", "MONTH"]:
        data = await client.get_leaderboard(time_period=period, limit=50)
        entries = {}
        for i, entry in enumerate(data, 1):
            name = entry.get("userName") or ""
            addr = (entry.get("proxyWallet") or "").lower()
            if not name or not addr:
                continue
            entries[name] = {
                "rank": i,
                "pnl": float(entry.get("pnl", 0)),
                "volume": float(entry.get("vol", 0)),
                "wallet": addr,
            }
        result[period] = entries
    return result


async def check_arbitrage_rate(client, address: str, sample_size: int = 50) -> float:
    """通过 Activity API 检测套利率"""
    try:
        activities = await client.get_user_trades(address, limit=sample_size)
        if not activities or not isinstance(activities, list):
            return 0.0

        market_sides = defaultdict(set)
        for trade in activities:
            cid = trade.get("conditionId") or trade.get("market", "")
            asset = trade.get("asset") or trade.get("assetId") or trade.get("tokenId", "")
            if cid and asset:
                market_sides[cid].add(asset)

        if not market_sides:
            return 0.0

        arb_markets = sum(1 for v in market_sides.values() if len(v) > 1)
        return arb_markets / len(market_sides)
    except Exception as e:
        log.warning(f"套利检测失败 {address[:10]}: {e}")
        return 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 核心决策引擎
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def decide_changes(
    env_wallets: dict,
    perf: dict,
    leaderboards: dict,
    client,
) -> list[dict]:
    """根据规则决定钱包变更"""
    changes = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ──────────── 第一步：评估现有活跃钱包 ────────────
    for name, info in env_wallets.items():
        if not info["active"]:
            continue

        ws = perf.get(name, {})
        sell_count = ws.get("sells", 0)

        # 保护：无卖出记录 → 不做移除判断
        if sell_count < REMOVE_MIN_SELLS:
            continue

        # 保护：手动标记的不自动恢复
        # (此处只检查移除，不恢复)

        realized_pnl = ws.get("realized_pnl", 0)
        wins = ws.get("wins", 0)
        losses = ws.get("losses", 0)
        stop_losses = ws.get("stop_losses", 0)
        win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0
        stop_loss_rate = stop_losses / sell_count if sell_count > 0 else 0

        # 规则1: 实现PNL < -$3 → 移除
        if realized_pnl < REMOVE_PNL_THRESHOLD:
            changes.append({
                "action": "comment_out",
                "name": name,
                "line_idx": info["line_idx"],
                "reason": f"亏损${realized_pnl:.1f}",
            })
            log.info(f"[移除] {name}: 实现PNL=${realized_pnl:.2f}")
            continue

        # 规则2: 止损率100% + 3笔以上 → 移除
        if stop_loss_rate >= REMOVE_STOP_LOSS_RATE and sell_count >= REMOVE_MIN_SELLS:
            changes.append({
                "action": "comment_out",
                "name": name,
                "line_idx": info["line_idx"],
                "reason": f"止损率{stop_loss_rate:.0%}({sell_count}笔)",
            })
            log.info(f"[移除] {name}: 止损率{stop_loss_rate:.0%}")
            continue

    # ──────────── 第二步：排行榜交叉检查现有钱包 ────────────
    for name, info in env_wallets.items():
        if not info["active"] or info["tier"] == "T2":
            continue
        # 已被第一步标记移除的跳过
        if any(c["name"] == name and c["action"] == "comment_out" for c in changes):
            continue

        # 检查 PNL/Vol 比（从日榜）
        day_entries = leaderboards.get("DAY", {})
        if name in day_entries:
            entry = day_entries[name]
            vol = entry["volume"]
            pnl = entry["pnl"]
            if vol > 0:
                ratio = pnl / vol
                if ratio < DOWNGRADE_PNL_VOL:
                    changes.append({
                        "action": "downgrade_t2",
                        "name": name,
                        "line_idx": info["line_idx"],
                        "reason": f"PNL/Vol={ratio:.2f}疑似套利",
                    })
                    log.info(f"[降级T2] {name}: PNL/Vol={ratio:.2f}")

    # ──────────── 第三步：发现新钱包候选 ────────────
    # 统计每个候选在几个榜上出现
    active_count = sum(1 for w in env_wallets.values() if w["active"])
    active_count -= sum(1 for c in changes if c["action"] == "comment_out")
    existing_addrs = {w["address"] for w in env_wallets.values()}
    existing_names = {n.lower() for n in env_wallets.keys()}

    candidate_boards = defaultdict(lambda: {"boards": set(), "best_pnl": 0, "best_vol": 0, "addr": ""})
    for period, entries in leaderboards.items():
        for uname, entry in entries.items():
            # 跳过已有的
            if uname.lower() in existing_names or entry["wallet"] in existing_addrs:
                continue
            # 跳过匿名（0x开头长地址名）
            if re.match(r"^0x[a-fA-F0-9]{30,}", uname):
                continue
            # 日榜最低PNL门槛
            if period == "DAY" and entry["pnl"] < ADD_MIN_DAILY_PNL:
                continue

            c = candidate_boards[uname]
            c["boards"].add(period)
            c["addr"] = entry["wallet"]
            if entry["pnl"] > c["best_pnl"]:
                c["best_pnl"] = entry["pnl"]
            if entry["volume"] > c["best_vol"]:
                c["best_vol"] = entry["volume"]

    # 按优先级排序：三榜 > 双榜 > 单榜，然后按 PNL
    candidates = sorted(
        candidate_boards.items(),
        key=lambda x: (-len(x[1]["boards"]), -x[1]["best_pnl"]),
    )

    add_count = 0
    max_adds = max(0, ADD_MAX_WALLETS - active_count)  # 不超过上限

    for uname, info in candidates:
        if add_count >= max_adds or add_count >= 10:  # 每次最多新增10个
            break

        boards = info["boards"]
        addr = info["addr"]
        pnl = info["best_pnl"]
        vol = info["best_vol"]
        pnl_vol = pnl / vol if vol > 0 else 999

        # 套利检测
        arb_rate = await check_arbitrage_rate(client, addr, sample_size=50)
        await asyncio.sleep(0.3)  # rate limit

        # 决定是否新增及 tier
        board_count = len(boards)
        tier = None
        reason_parts = []

        if board_count >= 2:
            # 多榜上榜: 三榜且PNL/Vol>0.15 → T1; 双榜且>0.3 → T1; 否则 T2
            if arb_rate <= ADD_ARB_MAX_MULTI_BOARD:
                if board_count >= 3 and pnl_vol >= 0.15:
                    tier = "T1"
                elif pnl_vol >= ADD_PNL_VOL_MIN_T1:
                    tier = "T1"
                else:
                    tier = "T2"
                board_names = "+".join(sorted(boards))
                reason_parts.append(f"{board_names}")
                reason_parts.append(f"套利{arb_rate:.0%}")
                if pnl_vol < 999:
                    reason_parts.append(f"PNL/Vol={pnl_vol:.2f}")
            else:
                log.info(f"[跳过] {uname}: 多榜但套利{arb_rate:.0%}过高")
                continue
        elif board_count == 1:
            # 单榜
            board_name = list(boards)[0]
            if arb_rate <= ADD_ARB_MAX_SINGLE_BOARD and pnl_vol >= ADD_PNL_VOL_MIN_T1:
                tier = "T1"
                reason_parts.append(f"{board_name}榜")
                reason_parts.append(f"套利{arb_rate:.0%}")
                reason_parts.append(f"PNL/Vol={pnl_vol:.2f}")
            elif arb_rate <= ADD_ARB_MAX_MULTI_BOARD:
                tier = "T2"
                reason_parts.append(f"{board_name}榜")
                reason_parts.append(f"套利{arb_rate:.0%}")
            else:
                log.info(f"[跳过] {uname}: 单榜套利{arb_rate:.0%}过高")
                continue

        if tier:
            reason = " ".join(reason_parts)
            changes.append({
                "action": "add",
                "name": uname,
                "address": addr,
                "tier": tier,
                "reason": reason,
            })
            add_count += 1
            log.info(f"[新增{tier}] {uname}: {reason}")

    return changes


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 变更日志
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def write_changelog(changes: list[dict], report_path: Path):
    """记录变更日志"""
    now = datetime.now()
    lines = [
        f"# 钱包自动更新日志 — {now.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    removes = [c for c in changes if c["action"] == "comment_out"]
    downgrades = [c for c in changes if c["action"] == "downgrade_t2"]
    adds = [c for c in changes if c["action"] == "add"]

    if removes:
        lines.append("## 移除")
        for c in removes:
            lines.append(f"- **{c['name']}**: {c.get('reason','')}")
    if downgrades:
        lines.append("## 降级 T2")
        for c in downgrades:
            lines.append(f"- **{c['name']}**: {c.get('reason','')}")
    if adds:
        lines.append("## 新增")
        for c in adds:
            tier = c.get("tier", "T1")
            lines.append(f"- **{c['name']}** ({tier}): {c.get('reason','')}")

    if not changes:
        lines.append("无变更。所有钱包状态正常。")

    lines.append("")
    lines.append(f"---")
    lines.append(f"*自动生成 by auto_wallet_updater.py*")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主流程
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def run_update(dry_run: bool = False):
    """执行一次钱包更新"""
    from market_client import MarketClient

    now = datetime.now()
    log.info(f"{'='*50}")
    log.info(f"开始钱包自动更新 — {now.strftime('%Y-%m-%d %H:%M')}")
    log.info(f"模式: {'DRY-RUN(仅分析)' if dry_run else '实际更新.env'}")

    # 1. 读取当前状态
    log.info("读取 engine_state.json ...")
    if not STATE_FILE.exists():
        log.error(f"状态文件不存在: {STATE_FILE}")
        return
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    log.info(f"  交易历史: {len(state.get('trade_history', []))} 笔")
    log.info(f"  当前持仓: {len(state.get('positions', {}))} 个")

    # 2. 分析跟单绩效
    log.info("分析跟单绩效 ...")
    perf = analyze_copy_performance(state)
    log.info(f"  有数据的钱包: {len(perf)} 个")

    # 3. 读取 .env
    log.info("读取 .env ...")
    env_lines = read_env_lines()
    env_wallets = parse_env_wallets(env_lines)
    active_count = sum(1 for w in env_wallets.values() if w["active"])
    log.info(f"  总钱包: {len(env_wallets)}, 活跃: {active_count}")

    # 4. 拉取排行榜
    client = MarketClient()
    try:
        log.info("拉取排行榜 ...")
        leaderboards = await fetch_leaderboards(client)
        for period, entries in leaderboards.items():
            log.info(f"  {period}榜: {len(entries)} 人")

        # 5. 决策
        log.info("分析变更 ...")
        changes = await decide_changes(env_wallets, perf, leaderboards, client)

        if not changes:
            log.info("✅ 无需变更，所有钱包状态正常")
        else:
            log.info(f"计划变更 {len(changes)} 项:")
            for c in changes:
                log.info(f"  [{c['action']}] {c['name']}: {c.get('reason','')}")

        # 6. 写入变更日志
        date_str = now.strftime("%Y%m%d_%H%M")
        changelog_path = REPORT_DIR / f"wallet_update_{date_str}.md"
        write_changelog(changes, changelog_path)
        log.info(f"变更日志: {changelog_path}")

        # 7. 应用变更
        if changes and not dry_run:
            # 备份 .env
            backup_path = BASE_DIR / f".env.bak.{date_str}"
            shutil.copy2(ENV_FILE, backup_path)
            log.info(f".env 备份: {backup_path}")

            # 写入
            new_lines = write_env_update(env_lines, changes)
            with open(ENV_FILE, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            log.info("✅ .env 已更新（热加载将在60秒内生效）")

            # 追加到总日志
            summary_path = REPORT_DIR / "update_history.log"
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{now.strftime('%Y-%m-%d %H:%M')}] ")
                removes = sum(1 for c in changes if c["action"] == "comment_out")
                downgrades = sum(1 for c in changes if c["action"] == "downgrade_t2")
                adds = sum(1 for c in changes if c["action"] == "add")
                f.write(f"移除{removes} 降级{downgrades} 新增{adds}\n")
                for c in changes:
                    f.write(f"  {c['action']:15s} {c['name']:25s} {c.get('reason','')}\n")

        elif changes and dry_run:
            log.info("[DRY-RUN] 以下变更未实际应用:")
            for c in changes:
                log.info(f"  {c['action']:15s} {c['name']:25s} {c.get('reason','')}")

    finally:
        await client.close()

    log.info(f"更新完成 — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"{'='*50}")


async def daemon_loop(dry_run: bool = False):
    """守护进程模式: 每4小时执行一次"""
    log.info(f"守护模式启动，每 {UPDATE_INTERVAL_HOURS} 小时更新一次")
    while True:
        try:
            await run_update(dry_run)
        except Exception as e:
            log.error(f"更新异常: {e}", exc_info=True)
        log.info(f"下次更新: {UPDATE_INTERVAL_HOURS} 小时后")
        await asyncio.sleep(UPDATE_INTERVAL_HOURS * 3600)


async def main():
    parser = argparse.ArgumentParser(description="Polymarket 跟单钱包自动更新器")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式(每4小时)")
    parser.add_argument("--dry-run", action="store_true", help="只分析不修改.env")
    args = parser.parse_args()

    if args.daemon:
        await daemon_loop(args.dry_run)
    else:
        await run_update(args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
