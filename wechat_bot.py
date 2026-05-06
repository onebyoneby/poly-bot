#!/usr/bin/env python3
"""
微信交互机器人
==============
扫码登录个人微信，直接在微信中查看跟单状态、收益报告、图表。

功能:
  - 发送 "报告" / "日报"  → 推送当日收益报告
  - 发送 "持仓"           → 推送持仓概览
  - 发送 "图表" / "走势"  → 推送PnL走势图+胜率图
  - 发送 "周报"           → 推送7天汇总
  - 发送 "状态"           → 推送Bot运行状态
  - 发送 "帮助"           → 查看所有命令
  - 自动定时推送 (早10点/晚10点)
  - 止损/止盈/停机实时告警

用法:
  python wechat_bot.py                  # 启动(扫码登录)
  python wechat_bot.py --file-helper    # 发送到文件传输助手(默认)
  python wechat_bot.py --group 群名     # 发送到指定群
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import itchat
from itchat.content import TEXT

logger = logging.getLogger("wechat_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

# ============================================================
# 路径配置
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_STATE_PATH = os.path.join(BASE_DIR, "logs", "engine_state.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "copy_engine.log")
CHART_DIR = os.path.join(BASE_DIR, "logs", "charts")
HOT_STORAGE = os.path.join(BASE_DIR, "logs", "wechat_login.pkl")
os.makedirs(CHART_DIR, exist_ok=True)

_GARBAGE_KW = ["bitcoin up or down", "solana up or down", "ethereum up or down"]

# 推送目标: filehelper(文件传输助手) 或群名
TARGET_USER = "filehelper"
TARGET_GROUP = ""


# ============================================================
# 数据采集 (复用 wechat_notifier 逻辑)
# ============================================================

def _load_state() -> dict:
    try:
        with open(ENGINE_STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _is_garbage(name: str) -> bool:
    return any(kw in name.lower() for kw in _GARBAGE_KW)


def _today_stats(state: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    trades = [t for t in state.get("trade_history", [])
              if t.get("time", "").startswith(today) and not _is_garbage(t.get("market", ""))]

    s = {"total": 0, "buys": 0, "sells": 0, "wins": 0, "losses": 0,
         "realized": 0.0, "best": None, "worst": None, "markets": set()}

    for t in trades:
        s["total"] += 1
        side = t.get("side", "")
        pnl = t.get("pnl", 0)
        if "买入" in side or side == "BUY":
            s["buys"] += 1
        else:
            s["sells"] += 1
            s["realized"] += pnl
            if pnl > 0:
                s["wins"] += 1
            elif pnl < 0:
                s["losses"] += 1
            if s["best"] is None or pnl > s["best"]["pnl"]:
                s["best"] = t
            if s["worst"] is None or pnl < s["worst"]["pnl"]:
                s["worst"] = t
        s["markets"].add(t.get("market", "")[:35])
    s["markets"] = len(s["markets"])
    return s


def _daily_pnl_from_log(days: int = 7) -> list[dict]:
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return []

    daily = defaultdict(lambda: {"buys": 0, "sells": 0, "pnl": 0.0})
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    for line in text.split("\n"):
        m = re.match(r"\[(\d{4}-\d{2}-\d{2})", line)
        if not m or m.group(1) < cutoff:
            continue
        if any(kw in line.lower() for kw in _GARBAGE_KW):
            continue
        day = m.group(1)
        if "模拟买入" in line or "实盘买入" in line:
            daily[day]["buys"] += 1
        elif "止损成交" in line or "止盈成交" in line or "追踪止盈" in line or "市场结算" in line:
            daily[day]["sells"] += 1
            pm = re.search(r"PnL=\$([+-]?[\d.]+)", line)
            if pm:
                daily[day]["pnl"] += float(pm.group(1))

    return [{"date": d, **daily[d]} for d in sorted(daily.keys())]


# ============================================================
# 图表生成
# ============================================================

def _make_pnl_chart(daily_data: list[dict]) -> str | None:
    """生成PnL图表，返回文件路径"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return None

    if len(daily_data) < 2:
        return None

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Heiti SC"]
    plt.rcParams["axes.unicode_minus"] = False

    dates = [datetime.strptime(d["date"], "%Y-%m-%d") for d in daily_data]
    pnls = [d["pnl"] for d in daily_data]
    cum = []
    t = 0
    for p in pnls:
        t += p
        cum.append(t)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [2, 1]})
    fig.suptitle("Polymarket 跟单收益报告", fontsize=16, fontweight="bold")

    ax1.plot(dates, cum, color="#3498db", linewidth=2.5, marker="o", markersize=6)
    ax1.fill_between(dates, cum, 0, where=[c >= 0 for c in cum], color="#2ecc71", alpha=0.15)
    ax1.fill_between(dates, cum, 0, where=[c < 0 for c in cum], color="#e74c3c", alpha=0.15)
    ax1.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax1.set_ylabel("累计 PnL ($)")
    ax1.set_title("7日累计收益走势")
    ax1.grid(True, alpha=0.3)
    for d, v in zip(dates, cum):
        ax1.annotate(f"${v:.0f}", (d, v), textcoords="offset points",
                     xytext=(0, 10), ha="center", fontsize=9,
                     color="#2ecc71" if v >= 0 else "#e74c3c")

    bar_colors = ["#2ecc71" if p >= 0 else "#e74c3c" for p in pnls]
    bars = ax2.bar(dates, pnls, color=bar_colors, width=0.6)
    ax2.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax2.set_ylabel("每日 PnL ($)")
    ax2.set_title("每日收益明细")
    ax2.grid(True, alpha=0.3, axis="y")
    for bar, val in zip(bars, pnls):
        ax2.text(bar.get_x() + bar.get_width() / 2., bar.get_height(),
                 f"${val:.0f}", ha="center",
                 va="bottom" if val >= 0 else "top", fontsize=9, fontweight="bold",
                 color="#2ecc71" if val >= 0 else "#e74c3c")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

    plt.tight_layout()
    path = os.path.join(CHART_DIR, f"wx_pnl_{datetime.now().strftime('%Y%m%d_%H%M')}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def _make_winrate_chart(state: dict) -> str | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    trades = state.get("trade_history", [])
    wins = sum(1 for t in trades if not _is_garbage(t.get("market", ""))
               and ("赢" in t.get("side", "") or "止盈" in t.get("side", "")))
    losses = sum(1 for t in trades if not _is_garbage(t.get("market", ""))
                 and ("输" in t.get("side", "") or "止损" in t.get("side", "")))
    if wins + losses == 0:
        return None

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Heiti SC"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(6, 6))
    labels = [f"盈利 ({wins})", f"亏损 ({losses})"]
    sizes = [wins, losses]
    colors = ["#2ecc71", "#e74c3c"]
    ax.pie(sizes, labels=labels, colors=colors, autopct="%1.1f%%",
           startangle=90, pctdistance=0.75,
           wedgeprops=dict(width=0.4, edgecolor="white", linewidth=2))
    wr = wins / (wins + losses) * 100
    ax.text(0, 0, f"{wr:.0f}%\n胜率", ha="center", va="center",
            fontsize=20, fontweight="bold", color="#2c3e50")
    ax.set_title("交易胜率分布", fontsize=14, fontweight="bold", pad=20)

    path = os.path.join(CHART_DIR, f"wx_wr_{datetime.now().strftime('%Y%m%d_%H%M')}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


# ============================================================
# 消息构建
# ============================================================

def build_report() -> str:
    state = _load_state()
    if not state:
        return "引擎状态不可读，请检查 Bot 是否运行中"

    ts = _today_stats(state)
    cum = state.get("cumulative_pnl", 0)
    floating = state.get("floating_pnl", 0)
    daily = state.get("daily_pnl", 0)
    stopped = state.get("is_stopped", False)
    now = datetime.now().strftime("%m/%d %H:%M")

    status = "🔴停机" if stopped else "🟢运行中"
    emoji = "📈" if daily >= 0 else "📉"

    lines = [
        f"━━ Polymarket 日报 {now} ━━",
        f"状态: {status}",
        "",
        f"{emoji} 今日收益: ${daily:+.2f}",
        f"交易: {ts['total']}笔 (买{ts['buys']}/卖{ts['sells']})",
        f"胜负: {ts['wins']}W / {ts['losses']}L",
        f"市场: {ts['markets']}个",
    ]

    if ts["best"]:
        lines.append(f"最佳: {ts['best']['market'][:25]} ${ts['best']['pnl']:+.2f}")
    if ts["worst"]:
        lines.append(f"最差: {ts['worst']['market'][:25]} ${ts['worst']['pnl']:+.2f}")

    lines += [
        "",
        f"💰 累计PnL: ${cum:+,.2f}",
        f"📊 持仓浮动: ${floating:+.2f}",
        f"🏦 合计: ${cum + floating:+,.2f}",
    ]

    if stopped:
        lines += ["", f"⚠️ 日亏损 ${abs(daily):.2f} 触发停机"]

    return "\n".join(lines)


def build_positions() -> str:
    state = _load_state()
    if not state:
        return "引擎状态不可读"

    positions = state.get("positions", {})
    active = []
    for pid, pos in positions.items():
        cp = pos.get("current_price", pos.get("entry_price", 0))
        if cp <= 0.02 or cp >= 0.98:
            continue
        ep = pos.get("entry_price", 0)
        shares = pos.get("shares", 0)
        pnl = (cp - ep) * shares
        active.append({
            "name": pos.get("market_name", pid)[:30],
            "cost": pos.get("cost", 0),
            "pnl": pnl,
            "pnl_pct": (cp / ep - 1) * 100 if ep > 0 else 0,
        })

    active.sort(key=lambda x: x["pnl"], reverse=True)

    lines = [
        f"━━ 持仓概览 ({len(active)}个) ━━",
        "",
    ]

    # Top 5 盈利
    if active:
        lines.append("📈 Top盈利:")
        for p in active[:5]:
            lines.append(f"  {p['name']} ${p['pnl']:+.1f} ({p['pnl_pct']:+.0f}%)")

    # Top 5 亏损
    losers = [p for p in active if p["pnl"] < 0]
    if losers:
        losers.sort(key=lambda x: x["pnl"])
        lines.append("\n📉 Top亏损:")
        for p in losers[:5]:
            lines.append(f"  {p['name']} ${p['pnl']:+.1f} ({p['pnl_pct']:+.0f}%)")

    total_cost = sum(p["cost"] for p in active)
    total_pnl = sum(p["pnl"] for p in active)
    profit_count = sum(1 for p in active if p["pnl"] >= 0)
    loss_count = sum(1 for p in active if p["pnl"] < 0)

    lines += [
        "",
        f"总成本: ${total_cost:,.0f}",
        f"总浮动: ${total_pnl:+.2f}",
        f"盈利: {profit_count}个 | 亏损: {loss_count}个",
    ]

    return "\n".join(lines)


def build_status() -> str:
    state = _load_state()
    if not state:
        return "引擎不可读"

    saved = state.get("saved_at", "?")
    pos_count = state.get("position_count", 0)
    unsettled = state.get("unsettled_count", 0)
    daily_trades = state.get("daily_trades", 0)
    cum = state.get("cumulative_pnl", 0)
    stopped = state.get("is_stopped", False)

    # 检查进程
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-f", "run_all.py"], capture_output=True, text=True)
        pid = r.stdout.strip().split("\n")[0] if r.stdout.strip() else None
    except Exception:
        pid = None

    lines = [
        "━━ Bot 状态 ━━",
        f"进程: {'🟢 PID ' + pid if pid else '🔴 未运行'}",
        f"引擎: {'🔴 停机' if stopped else '🟢 运行中'}",
        f"最后更新: {saved}",
        f"持仓数: {pos_count} (活跃{unsettled})",
        f"今日交易: {daily_trades}笔",
        f"累计PnL: ${cum:+,.2f}",
    ]
    return "\n".join(lines)


def build_weekly() -> str:
    data = _daily_pnl_from_log(7)
    if not data:
        return "无近7天数据"

    total = sum(d["pnl"] for d in data)
    best = max(data, key=lambda d: d["pnl"])
    worst = min(data, key=lambda d: d["pnl"])

    lines = [
        f"━━ 7日周报 ━━",
        f"周收益: ${total:+,.2f}",
        f"最佳: {best['date']} ${best['pnl']:+.2f}",
        f"最差: {worst['date']} ${worst['pnl']:+.2f}",
        "",
    ]
    for d in data:
        emoji = "🟢" if d["pnl"] >= 0 else "🔴"
        lines.append(f"{emoji} {d['date']} | {d['buys']}买/{d['sells']}卖 | ${d['pnl']:+.2f}")

    return "\n".join(lines)


HELP_TEXT = """━━ 命令列表 ━━
📊 报告 / 日报 — 今日收益报告
💼 持仓 — 持仓概览(Top盈亏)
📈 图表 / 走势 — PnL走势图+胜率图
📅 周报 — 7天汇总
🔧 状态 — Bot运行状态
❓ 帮助 — 查看此列表

自动推送: 每天10:00/22:00
告警: 止损/止盈/停机实时推送"""


# ============================================================
# 微信消息发送
# ============================================================

def send_text(text: str, to_user: str = None):
    """发送文本消息"""
    target = to_user or TARGET_USER
    if TARGET_GROUP and not to_user:
        rooms = itchat.search_chatrooms(name=TARGET_GROUP)
        if rooms:
            itchat.send(text, toUserName=rooms[0]["UserName"])
            return
    itchat.send(text, toUserName=target)


def send_image(path: str, to_user: str = None):
    """发送图片"""
    if not path or not os.path.exists(path):
        return
    target = to_user or TARGET_USER
    if TARGET_GROUP and not to_user:
        rooms = itchat.search_chatrooms(name=TARGET_GROUP)
        if rooms:
            itchat.send_image(path, toUserName=rooms[0]["UserName"])
            return
    itchat.send_image(path, toUserName=target)


# ============================================================
# 消息处理 — 收到微信消息时触发
# ============================================================

@itchat.msg_register(TEXT)
def on_text(msg):
    """处理收到的文本消息"""
    text = msg["Text"].strip()
    from_user = msg["FromUserName"]

    # 只响应文件传输助手或自己发的消息
    if from_user != "filehelper" and from_user != msg["ToUserName"]:
        # 也响应来自目标群的消息
        if not TARGET_GROUP:
            return
        # 群消息另外处理
        return

    _handle_command(text, from_user)


@itchat.msg_register(TEXT, isGroupChat=True)
def on_group_text(msg):
    """处理群消息 — 需要@或特定关键词"""
    if not TARGET_GROUP:
        return
    # 群名匹配
    group = msg.get("User", {}).get("NickName", "")
    if TARGET_GROUP not in group:
        return

    text = msg["Text"].strip()
    # 去掉@部分
    text = re.sub(r"@\S+\s*", "", text).strip()
    if text:
        _handle_command(text, msg["FromUserName"])


def _handle_command(text: str, reply_to: str):
    """命令路由"""
    cmd = text.lower().strip()

    if cmd in ("报告", "日报", "report", "今日", "收益"):
        send_text(build_report(), reply_to)

    elif cmd in ("持仓", "仓位", "positions"):
        send_text(build_positions(), reply_to)

    elif cmd in ("图表", "走势", "chart", "图"):
        send_text("生成图表中...", reply_to)
        state = _load_state()
        data = _daily_pnl_from_log(7)
        p1 = _make_pnl_chart(data)
        p2 = _make_winrate_chart(state)
        if p1:
            send_image(p1, reply_to)
        if p2:
            send_image(p2, reply_to)
        if not p1 and not p2:
            send_text("图表生成失败，数据不足", reply_to)

    elif cmd in ("周报", "weekly", "7天"):
        send_text(build_weekly(), reply_to)

    elif cmd in ("状态", "status"):
        send_text(build_status(), reply_to)

    elif cmd in ("帮助", "help", "?", "？"):
        send_text(HELP_TEXT, reply_to)

    # 不认识的命令不回复，避免干扰正常聊天


# ============================================================
# 定时推送 + 告警
# ============================================================

class ScheduledPusher:
    """后台线程: 定时推送 + 监控告警"""

    def __init__(self):
        self._last_report_hour = -1
        self._last_weekly_day = -1
        self._last_daily_pnl = 0.0
        self._last_alert_time: dict[str, float] = {}
        self._running = True

    def run(self):
        logger.info("定时推送线程启动")
        time.sleep(10)  # 等待登录稳定

        while self._running:
            try:
                now = datetime.now()

                # 定时日报 (10:00, 22:00)
                if now.hour in (10, 22) and now.hour != self._last_report_hour:
                    self._last_report_hour = now.hour
                    logger.info(f"定时推送日报 ({now.hour}:00)")
                    send_text(build_report())
                    # 推送图表
                    data = _daily_pnl_from_log(7)
                    p1 = _make_pnl_chart(data)
                    if p1:
                        send_image(p1)
                    p2 = _make_winrate_chart(_load_state())
                    if p2:
                        send_image(p2)

                # 周日周报
                if now.weekday() == 6 and now.hour == 22 and now.day != self._last_weekly_day:
                    self._last_weekly_day = now.day
                    send_text(build_weekly())

                # 监控告警: 检查 engine_state 变化
                self._check_alerts()

            except Exception as e:
                logger.error(f"定时推送异常: {e}")

            time.sleep(60)

    def _check_alerts(self):
        state = _load_state()
        if not state:
            return

        daily_pnl = state.get("daily_pnl", 0)
        is_stopped = state.get("is_stopped", False)

        # 日亏损停机告警
        if is_stopped and "daily_stop" not in self._last_alert_time:
            self._last_alert_time["daily_stop"] = time.time()
            send_text(f"⚠️ 日亏损停机告警\n今日亏损: ${abs(daily_pnl):.2f}\n已停止交易，次日自动恢复")

        # 每日重置
        if not is_stopped and "daily_stop" in self._last_alert_time:
            del self._last_alert_time["daily_stop"]
            send_text(f"🟢 交易已恢复\n新的一天开始!")

        # 大额盈亏变动 (>$50)
        if abs(daily_pnl - self._last_daily_pnl) > 50:
            now = time.time()
            if now - self._last_alert_time.get("big_move", 0) > 1800:
                self._last_alert_time["big_move"] = now
                emoji = "📈" if daily_pnl > self._last_daily_pnl else "📉"
                send_text(f"{emoji} 收益大幅变动\n当前日PnL: ${daily_pnl:+.2f}\n变动: ${daily_pnl - self._last_daily_pnl:+.2f}")
            self._last_daily_pnl = daily_pnl

    def stop(self):
        self._running = False


# ============================================================
# 主入口
# ============================================================

def main():
    global TARGET_USER, TARGET_GROUP

    import argparse
    parser = argparse.ArgumentParser(description="微信交互机器人")
    parser.add_argument("--group", type=str, default="", help="发送到指定微信群")
    parser.add_argument("--file-helper", action="store_true", default=True,
                        help="发送到文件传输助手(默认)")
    args = parser.parse_args()

    if args.group:
        TARGET_GROUP = args.group
        logger.info(f"目标群: {TARGET_GROUP}")
    else:
        TARGET_USER = "filehelper"
        logger.info("目标: 文件传输助手")

    print("=" * 50)
    print("  Polymarket 微信交互机器人")
    print("=" * 50)
    print()
    print("请用手机微信扫描二维码登录...")
    print("登录后在微信「文件传输助手」发送命令:")
    print("  报告  持仓  图表  周报  状态  帮助")
    print()

    # 登录
    itchat.auto_login(
        hotReload=True,
        statusStorageDir=HOT_STORAGE,
        enableCmdQR=2,  # 终端显示二维码
    )

    logger.info("微信登录成功!")

    # 发送上线通知
    send_text("🤖 Polymarket Bot 微信助手已上线!\n\n发送「帮助」查看所有命令\n自动推送: 每天 10:00 / 22:00")

    # 启动定时推送线程
    pusher = ScheduledPusher()
    pusher_thread = threading.Thread(target=pusher.run, daemon=True)
    pusher_thread.start()

    # 信号处理
    def _shutdown(sig, frame):
        logger.info("收到停止信号，退出...")
        pusher.stop()
        itchat.logout()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 运行消息监听
    itchat.run(debug=False, blockThread=True)


if __name__ == "__main__":
    main()
