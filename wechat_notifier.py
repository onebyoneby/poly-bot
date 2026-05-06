"""
微信自动汇报模块
================
支持两种推送渠道:
  1. 企业微信群机器人 (WeCom Webhook) — 推送到企业微信群
  2. PushPlus — 推送到个人微信

功能:
  - 定时日报 (每天早10点、晚10点)
  - 实时告警 (止损、大额盈亏、日亏损停机)
  - 每周汇总 (周日晚推送)
  - 可视化图表 (PnL走势、胜率饼图)

配置 (.env):
  WECHAT_PUSH_ENABLED=true
  WECHAT_CHANNEL=wecom          # wecom / pushplus
  WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
  PUSHPLUS_TOKEN=xxx
  WECHAT_REPORT_HOURS=10,22     # 日报推送时间
  WECHAT_ALERT_ENABLED=true     # 实时告警开关
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

logger = logging.getLogger("wechat_notifier")

# ============================================================
# 配置
# ============================================================

PUSH_ENABLED = os.getenv("WECHAT_PUSH_ENABLED", "false").lower() == "true"
PUSH_CHANNEL = os.getenv("WECHAT_CHANNEL", "wecom")  # wecom / pushplus
WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
REPORT_HOURS = [int(h) for h in os.getenv("WECHAT_REPORT_HOURS", "10,22").split(",")]
ALERT_ENABLED = os.getenv("WECHAT_ALERT_ENABLED", "true").lower() == "true"
WEEKLY_ENABLED = os.getenv("WECHAT_WEEKLY_ENABLED", "true").lower() == "true"

ENGINE_STATE_PATH = os.path.join(os.path.dirname(__file__), "logs", "engine_state.json")
LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "copy_engine.log")
CHART_DIR = os.path.join(os.path.dirname(__file__), "logs", "charts")

# 垃圾市场关键词
_GARBAGE_KW = ["bitcoin up or down", "solana up or down", "ethereum up or down"]


# ============================================================
# 推送接口
# ============================================================

async def _send_wecom_text(content: str):
    """企业微信群机器人 — 发送 Markdown 消息"""
    if not WECOM_WEBHOOK_URL:
        logger.warning("WECOM_WEBHOOK_URL 未配置")
        return False
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": content}
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(WECOM_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if data.get("errcode") == 0:
                    logger.info("企业微信推送成功")
                    return True
                logger.error(f"企业微信推送失败: {data}")
                return False
    except Exception as e:
        logger.error(f"企业微信推送异常: {e}")
        return False


async def _send_wecom_image(image_base64: str, md5: str):
    """企业微信群机器人 — 发送图片"""
    if not WECOM_WEBHOOK_URL:
        return False
    payload = {
        "msgtype": "image",
        "image": {"base64": image_base64, "md5": md5}
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(WECOM_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                return data.get("errcode") == 0
    except Exception as e:
        logger.error(f"企业微信图片推送异常: {e}")
        return False


async def _send_pushplus(title: str, content: str):
    """PushPlus — 推送到个人微信"""
    if not PUSHPLUS_TOKEN:
        logger.warning("PUSHPLUS_TOKEN 未配置")
        return False
    url = "http://www.pushplus.plus/send"
    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "markdown",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if data.get("code") == 200:
                    logger.info("PushPlus 推送成功")
                    return True
                logger.error(f"PushPlus 推送失败: {data}")
                return False
    except Exception as e:
        logger.error(f"PushPlus 推送异常: {e}")
        return False


async def push_message(title: str, content: str):
    """统一推送接口"""
    if not PUSH_ENABLED:
        logger.debug("微信推送未启用，跳过")
        return False

    if PUSH_CHANNEL == "pushplus":
        return await _send_pushplus(title, content)
    else:
        # wecom markdown 中 title 放在内容头部
        full = f"## {title}\n{content}"
        return await _send_wecom_text(full)


async def push_image(image_bytes: bytes):
    """推送图片（仅企业微信支持）"""
    if not PUSH_ENABLED or PUSH_CHANNEL != "wecom":
        return False
    import hashlib
    img_b64 = base64.b64encode(image_bytes).decode()
    img_md5 = hashlib.md5(image_bytes).hexdigest()
    return await _send_wecom_image(img_b64, img_md5)


# ============================================================
# 数据采集
# ============================================================

def _load_engine_state() -> dict:
    try:
        with open(ENGINE_STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _is_garbage(name: str) -> bool:
    nl = name.lower()
    return any(kw in nl for kw in _GARBAGE_KW)


def _parse_today_trades(state: dict) -> dict:
    """解析今日交易统计"""
    today = datetime.now().strftime("%Y-%m-%d")
    trades = state.get("trade_history", [])
    today_trades = [t for t in trades if t.get("time", "").startswith(today)]

    stats = {
        "total": 0, "buys": 0, "sells": 0,
        "wins": 0, "losses": 0,
        "realized_pnl": 0.0,
        "total_cost": 0.0,
        "best_trade": None, "worst_trade": None,
        "markets": set(),
    }

    for t in today_trades:
        if _is_garbage(t.get("market", "")):
            continue
        stats["total"] += 1
        side = t.get("side", "")
        pnl = t.get("pnl", 0)

        if "买入" in side or side == "BUY":
            stats["buys"] += 1
            stats["total_cost"] += t.get("size_usd", 0)
        else:
            stats["sells"] += 1
            stats["realized_pnl"] += pnl
            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1

            if stats["best_trade"] is None or pnl > stats["best_trade"]["pnl"]:
                stats["best_trade"] = t
            if stats["worst_trade"] is None or pnl < stats["worst_trade"]["pnl"]:
                stats["worst_trade"] = t

        stats["markets"].add(t.get("market", "")[:40])

    stats["markets"] = len(stats["markets"])
    return stats


def _parse_log_daily_pnl(days: int = 7) -> list[dict]:
    """从日志文件解析每日PnL（最近N天）"""
    results = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            log_text = f.read()
    except Exception:
        return results

    daily = defaultdict(lambda: {"buys": 0, "sells": 0, "pnl": 0.0, "settled_win": 0, "settled_loss": 0})
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    for line in log_text.split("\n"):
        m = re.match(r"\[(\d{4}-\d{2}-\d{2})", line)
        if not m:
            continue
        day = m.group(1)
        if day < cutoff:
            continue

        # 过滤垃圾市场
        if any(kw in line.lower() for kw in _GARBAGE_KW):
            continue

        if "模拟买入" in line or "实盘买入" in line:
            daily[day]["buys"] += 1
        elif "止损成交" in line or "止盈成交" in line or "追踪止盈" in line:
            daily[day]["sells"] += 1
            pnl_m = re.search(r"PnL=\$([+-]?[\d.]+)", line)
            if pnl_m:
                daily[day]["pnl"] += float(pnl_m.group(1))
        elif "市场结算" in line:
            pnl_m = re.search(r"PnL=\$([+-]?[\d.]+)", line)
            if pnl_m:
                pnl_val = float(pnl_m.group(1))
                daily[day]["pnl"] += pnl_val
                if pnl_val >= 0:
                    daily[day]["settled_win"] += 1
                else:
                    daily[day]["settled_loss"] += 1

    for day in sorted(daily.keys()):
        results.append({"date": day, **daily[day]})

    return results


# ============================================================
# 图表生成
# ============================================================

def _generate_pnl_chart(daily_data: list[dict]) -> bytes | None:
    """生成PnL走势+每日柱状图"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.warning("matplotlib 未安装，跳过图表生成")
        return None

    if len(daily_data) < 2:
        return None

    os.makedirs(CHART_DIR, exist_ok=True)

    dates = [datetime.strptime(d["date"], "%Y-%m-%d") for d in daily_data]
    pnls = [d["pnl"] for d in daily_data]
    cum_pnl = []
    total = 0
    for p in pnls:
        total += p
        cum_pnl.append(total)

    # 设置中文字体
    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Heiti SC", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [2, 1]})
    fig.suptitle("Polymarket 跟单收益报告", fontsize=16, fontweight="bold")

    # 上图: 累计PnL曲线
    colors_line = ["#2ecc71" if p >= 0 else "#e74c3c" for p in cum_pnl]
    ax1.plot(dates, cum_pnl, color="#3498db", linewidth=2.5, marker="o", markersize=6)
    ax1.fill_between(dates, cum_pnl, 0,
                     where=[p >= 0 for p in cum_pnl], color="#2ecc71", alpha=0.15)
    ax1.fill_between(dates, cum_pnl, 0,
                     where=[p < 0 for p in cum_pnl], color="#e74c3c", alpha=0.15)
    ax1.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
    ax1.set_ylabel("累计 PnL ($)", fontsize=12)
    ax1.set_title("7日累计收益走势", fontsize=13)
    ax1.grid(True, alpha=0.3)

    for i, (d, v) in enumerate(zip(dates, cum_pnl)):
        ax1.annotate(f"${v:.0f}", (d, v), textcoords="offset points",
                     xytext=(0, 10), ha="center", fontsize=9,
                     color="#2ecc71" if v >= 0 else "#e74c3c")

    # 下图: 每日PnL柱状图
    bar_colors = ["#2ecc71" if p >= 0 else "#e74c3c" for p in pnls]
    bars = ax2.bar(dates, pnls, color=bar_colors, width=0.6, edgecolor="white", linewidth=0.5)
    ax2.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
    ax2.set_ylabel("每日 PnL ($)", fontsize=12)
    ax2.set_title("每日收益明细", fontsize=13)
    ax2.grid(True, alpha=0.3, axis="y")

    for bar, val in zip(bars, pnls):
        y_pos = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width() / 2., y_pos,
                 f"${val:.0f}", ha="center",
                 va="bottom" if val >= 0 else "top",
                 fontsize=9, fontweight="bold",
                 color="#2ecc71" if val >= 0 else "#e74c3c")

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)

    # 同时保存到本地
    chart_path = os.path.join(CHART_DIR, f"pnl_{datetime.now().strftime('%Y%m%d')}.png")
    with open(chart_path, "wb") as f:
        f.write(buf.getvalue())
    logger.info(f"图表已保存: {chart_path}")

    buf.seek(0)
    return buf.read()


def _generate_winrate_chart(state: dict) -> bytes | None:
    """生成胜率饼图"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    trades = state.get("trade_history", [])
    wins, losses, pending = 0, 0, 0
    for t in trades:
        if _is_garbage(t.get("market", "")):
            continue
        side = t.get("side", "")
        if "赢" in side or "止盈" in side:
            wins += 1
        elif "输" in side or "止损" in side:
            losses += 1
        elif "买入" in side:
            pending += 1

    if wins + losses == 0:
        return None

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Heiti SC", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(6, 6))
    labels = []
    sizes = []
    colors = []

    if wins > 0:
        labels.append(f"盈利 ({wins})")
        sizes.append(wins)
        colors.append("#2ecc71")
    if losses > 0:
        labels.append(f"亏损 ({losses})")
        sizes.append(losses)
        colors.append("#e74c3c")
    if pending > 0:
        labels.append(f"持仓中 ({pending})")
        sizes.append(pending)
        colors.append("#f39c12")

    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=colors, autopct="%1.1f%%",
        startangle=90, pctdistance=0.75,
        wedgeprops=dict(width=0.4, edgecolor="white", linewidth=2)
    )
    for t in autotexts:
        t.set_fontsize(12)
        t.set_fontweight("bold")

    winrate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    ax.text(0, 0, f"{winrate:.0f}%\n胜率", ha="center", va="center",
            fontsize=20, fontweight="bold", color="#2c3e50")
    ax.set_title("交易胜率分布", fontsize=14, fontweight="bold", pad=20)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ============================================================
# 报告模板
# ============================================================

def _build_daily_report() -> tuple[str, str]:
    """构建每日报告 → (title, markdown内容)"""
    state = _load_engine_state()
    if not state:
        return "Polymarket 日报", "引擎状态文件不可读"

    now = datetime.now()
    today_stats = _parse_today_trades(state)
    positions = state.get("positions", {})
    cum_pnl = state.get("cumulative_pnl", 0)
    floating = state.get("floating_pnl", 0)
    daily_pnl = state.get("daily_pnl", 0)
    is_stopped = state.get("is_stopped", False)

    # 持仓分析
    active_pos = 0
    total_cost = 0
    pos_profit = 0
    pos_loss = 0
    for pid, pos in positions.items():
        cp = pos.get("current_price", pos.get("entry_price", 0))
        ep = pos.get("entry_price", 0)
        if cp <= 0.02 or cp >= 0.98:
            continue
        active_pos += 1
        total_cost += pos.get("cost", 0)
        unrealized = (cp - ep) * pos.get("shares", 0)
        if unrealized >= 0:
            pos_profit += 1
        else:
            pos_loss += 1

    status_emoji = "🔴 停机" if is_stopped else "🟢 运行中"
    pnl_emoji = "📈" if daily_pnl >= 0 else "📉"

    title = f"Polymarket 日报 {now.strftime('%m/%d %H:%M')}"

    lines = [
        f"**状态**: {status_emoji}",
        f"**时间**: {now.strftime('%Y-%m-%d %H:%M')}",
        "",
        "---",
        f"### {pnl_emoji} 今日收益",
        f"> 今日PnL: **${daily_pnl:+.2f}**",
        f"> 今日交易: {today_stats['total']}笔 (买{today_stats['buys']} / 卖{today_stats['sells']})",
        f"> 胜/负: {today_stats['wins']}W / {today_stats['losses']}L",
        f"> 涉及市场: {today_stats['markets']}个",
    ]

    if today_stats["best_trade"]:
        bt = today_stats["best_trade"]
        lines.append(f"> 最佳交易: {bt['market'][:30]} **${bt['pnl']:+.2f}**")
    if today_stats["worst_trade"]:
        wt = today_stats["worst_trade"]
        lines.append(f"> 最差交易: {wt['market'][:30]} **${wt['pnl']:+.2f}**")

    lines += [
        "",
        "---",
        "### 💰 总览",
        f"> 累计总PnL: **${cum_pnl:+,.2f}**",
        f"> 持仓浮动: ${floating:+.2f}",
        f"> 合计: ${cum_pnl + floating:+,.2f}",
        "",
        "---",
        "### 📊 持仓状态",
        f"> 活跃持仓: {active_pos}个",
        f"> 持仓成本: ${total_cost:,.0f}",
        f"> 盈利中: {pos_profit}个 | 亏损中: {pos_loss}个",
    ]

    if is_stopped:
        lines += [
            "",
            "---",
            "### ⚠️ 风控告警",
            f"> 日亏损 ${abs(daily_pnl):.2f} 触发停机保护",
            "> 次日自动恢复交易",
        ]

    return title, "\n".join(lines)


def _build_weekly_report() -> tuple[str, str]:
    """构建周报"""
    state = _load_engine_state()
    daily_data = _parse_log_daily_pnl(days=7)

    if not daily_data:
        return "Polymarket 周报", "无7天数据"

    total_pnl = sum(d["pnl"] for d in daily_data)
    total_buys = sum(d["buys"] for d in daily_data)
    total_sells = sum(d["sells"] for d in daily_data)
    win_days = sum(1 for d in daily_data if d["pnl"] > 0)
    loss_days = sum(1 for d in daily_data if d["pnl"] < 0)
    best_day = max(daily_data, key=lambda d: d["pnl"])
    worst_day = min(daily_data, key=lambda d: d["pnl"])

    title = f"Polymarket 周报 {datetime.now().strftime('%m/%d')}"
    lines = [
        f"### 📊 本周汇总 ({daily_data[0]['date']} ~ {daily_data[-1]['date']})",
        "",
        f"> 周收益: **${total_pnl:+,.2f}**",
        f"> 盈利天数: {win_days}天 / 亏损天数: {loss_days}天",
        f"> 总交易: {total_buys + total_sells}笔",
        f"> 最佳日: {best_day['date']} (${best_day['pnl']:+.2f})",
        f"> 最差日: {worst_day['date']} (${worst_day['pnl']:+.2f})",
        "",
        "---",
        "### 每日明细",
        "",
        "| 日期 | 买入 | 卖出 | PnL |",
        "| --- | --- | --- | --- |",
    ]

    for d in daily_data:
        pnl_str = f"${d['pnl']:+.2f}"
        lines.append(f"| {d['date']} | {d['buys']} | {d['sells']} | {pnl_str} |")

    cum_pnl = state.get("cumulative_pnl", 0)
    lines += [
        "",
        "---",
        f"> 累计总PnL: **${cum_pnl:+,.2f}**",
    ]

    return title, "\n".join(lines)


# ============================================================
# 实时告警
# ============================================================

_last_alert_time: dict[str, float] = {}
_ALERT_COOLDOWN = 300  # 同类告警冷却5分钟


async def alert_stop_loss(market: str, pnl: float, entry_price: float, exit_price: float):
    """止损告警"""
    if not ALERT_ENABLED or not PUSH_ENABLED:
        return
    key = f"sl_{market[:30]}"
    if time.time() - _last_alert_time.get(key, 0) < _ALERT_COOLDOWN:
        return
    _last_alert_time[key] = time.time()

    content = (
        f"### 🔴 止损告警\n"
        f"> **市场**: {market[:50]}\n"
        f"> **PnL**: ${pnl:+.2f}\n"
        f"> **买入价**: ${entry_price:.4f} → 卖出价: ${exit_price:.4f}\n"
        f"> **时间**: {datetime.now().strftime('%H:%M:%S')}"
    )
    await push_message("止损告警", content)


async def alert_take_profit(market: str, pnl: float, pnl_pct: float):
    """止盈告警"""
    if not ALERT_ENABLED or not PUSH_ENABLED:
        return
    key = f"tp_{market[:30]}"
    if time.time() - _last_alert_time.get(key, 0) < _ALERT_COOLDOWN:
        return
    _last_alert_time[key] = time.time()

    content = (
        f"### 🟢 止盈成功\n"
        f"> **市场**: {market[:50]}\n"
        f"> **盈利**: ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
        f"> **时间**: {datetime.now().strftime('%H:%M:%S')}"
    )
    await push_message("止盈通知", content)


async def alert_daily_limit(daily_pnl: float, limit: float):
    """日亏损停机告警"""
    if not ALERT_ENABLED or not PUSH_ENABLED:
        return
    key = "daily_limit"
    if time.time() - _last_alert_time.get(key, 0) < 3600:  # 1小时冷却
        return
    _last_alert_time[key] = time.time()

    content = (
        f"### ⚠️ 日亏损停机\n"
        f"> **今日亏损**: ${abs(daily_pnl):.2f}\n"
        f"> **亏损上限**: ${limit:.2f}\n"
        f"> **状态**: 已停止交易，次日自动恢复\n"
        f"> **时间**: {datetime.now().strftime('%H:%M:%S')}"
    )
    await push_message("日亏损停机", content)


async def alert_settlement(market: str, pnl: float, result: str):
    """结算告警（仅大额）"""
    if not ALERT_ENABLED or not PUSH_ENABLED:
        return
    if abs(pnl) < 20:  # 小额不推送
        return

    emoji = "🏆" if pnl > 0 else "💀"
    content = (
        f"### {emoji} 市场结算\n"
        f"> **市场**: {market[:50]}\n"
        f"> **结果**: {result}\n"
        f"> **PnL**: ${pnl:+.2f}\n"
        f"> **时间**: {datetime.now().strftime('%H:%M:%S')}"
    )
    await push_message("市场结算", content)


# ============================================================
# 定时任务调度
# ============================================================

class WeChatReporter:
    """微信汇报调度器 — 集成到 run_all.py"""

    def __init__(self):
        self._last_report_hour: int = -1
        self._last_weekly_day: int = -1
        self._started = False

    async def run(self, shutdown_event: asyncio.Event):
        """主循环 — 每分钟检查是否该推送"""
        if not PUSH_ENABLED:
            logger.info("微信推送未启用 (WECHAT_PUSH_ENABLED=false)")
            return

        logger.info(f"微信汇报启动 | 渠道={PUSH_CHANNEL} | 日报时间={REPORT_HOURS}")
        self._started = True

        # 启动通知
        await push_message(
            "🤖 Bot 已启动",
            f"> **时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"> **模式**: {'模拟' if os.getenv('DRY_RUN', 'true').lower() == 'true' else '实盘'}\n"
            f"> 微信汇报已就绪"
        )

        while not shutdown_event.is_set():
            try:
                now = datetime.now()

                # 定时日报
                if now.hour in REPORT_HOURS and now.hour != self._last_report_hour:
                    self._last_report_hour = now.hour
                    await self._send_daily_report()

                # 周日晚10点周报
                if (WEEKLY_ENABLED and now.weekday() == 6
                        and now.hour == 22 and now.day != self._last_weekly_day):
                    self._last_weekly_day = now.day
                    await self._send_weekly_report()

            except Exception as e:
                logger.error(f"微信汇报异常: {e}", exc_info=True)

            # 每60秒检查一次
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                pass

        logger.info("微信汇报已停止")

    async def _send_daily_report(self):
        """发送日报 + 图表"""
        logger.info("生成并推送日报...")
        title, content = _build_daily_report()
        await push_message(title, content)

        # 发送图表
        daily_data = _parse_log_daily_pnl(days=7)
        chart = _generate_pnl_chart(daily_data)
        if chart:
            await push_image(chart)

        winrate_chart = _generate_winrate_chart(_load_engine_state())
        if winrate_chart:
            await push_image(winrate_chart)

    async def _send_weekly_report(self):
        """发送周报"""
        logger.info("生成并推送周报...")
        title, content = _build_weekly_report()
        await push_message(title, content)

    async def send_now(self):
        """立即发送一次报告（供手动调用）"""
        await self._send_daily_report()


# ============================================================
# 快捷函数 — 供 realtime_copy.py 等模块直接调用
# ============================================================

_reporter: WeChatReporter | None = None


def get_reporter() -> WeChatReporter:
    global _reporter
    if _reporter is None:
        _reporter = WeChatReporter()
    return _reporter


async def send_report_now():
    """立即推送当前报告"""
    await get_reporter().send_now()


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    async def _test():
        print("=== 微信推送测试 ===")
        print(f"渠道: {PUSH_CHANNEL}")
        print(f"启用: {PUSH_ENABLED}")

        if not PUSH_ENABLED:
            print("\n未启用推送，生成本地预览...")

        # 生成报告预览
        title, content = _build_daily_report()
        print(f"\n--- {title} ---")
        print(content)

        # 生成图表
        daily_data = _parse_log_daily_pnl(days=7)
        if daily_data:
            chart = _generate_pnl_chart(daily_data)
            if chart:
                path = os.path.join(CHART_DIR, "test_pnl.png")
                print(f"\n图表已保存: {path}")

            state = _load_engine_state()
            wr = _generate_winrate_chart(state)
            if wr:
                path = os.path.join(CHART_DIR, "test_winrate.png")
                with open(path, "wb") as f:
                    f.write(wr)
                print(f"胜率图已保存: {path}")

        # 如果启用了推送，发送测试消息
        if PUSH_ENABLED:
            print("\n发送测试消息...")
            ok = await push_message("🧪 推送测试", "这是一条测试消息，如果你收到说明配置成功！")
            print(f"发送结果: {'成功' if ok else '失败'}")

    asyncio.run(_test())
