"""
macOS 桌面通知模块
=================
零配置，直接用 macOS 通知中心推送告警。
无需登录任何账号。

功能:
  - 止损/止盈实时弹窗通知
  - 日亏损停机通知
  - 定时收益摘要通知
  - 大额盈亏变动通知
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime

logger = logging.getLogger("desktop_notify")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_STATE_PATH = os.path.join(BASE_DIR, "logs", "engine_state.json")


def notify(title: str, message: str, sound: str = "default"):
    """发送 macOS 桌面通知"""
    # 转义特殊字符
    title = title.replace('"', '\\"').replace("'", "\\'")
    message = message.replace('"', '\\"').replace("'", "\\'")

    script = f'display notification "{message}" with title "{title}" sound name "{sound}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5
        )
        logger.debug(f"桌面通知: {title}")
    except Exception as e:
        logger.error(f"桌面通知失败: {e}")


def notify_stop_loss(market: str, pnl: float):
    notify("🔴 止损告警", f"{market[:40]}\nPnL: ${pnl:+.2f}", "Basso")


def notify_take_profit(market: str, pnl: float):
    notify("🟢 止盈成功", f"{market[:40]}\n盈利: ${pnl:+.2f}", "Glass")


def notify_daily_limit(daily_pnl: float):
    notify("⚠️ 日亏损停机", f"今日亏损 ${abs(daily_pnl):.2f}\n已停止交易", "Sosumi")


def notify_settlement(market: str, pnl: float):
    if abs(pnl) < 15:
        return
    emoji = "🏆" if pnl > 0 else "💀"
    notify(f"{emoji} 市场结算", f"{market[:40]}\nPnL: ${pnl:+.2f}")


def notify_summary(cum_pnl: float, daily_pnl: float, positions: int):
    emoji = "📈" if daily_pnl >= 0 else "📉"
    notify(
        f"{emoji} 收益摘要",
        f"今日: ${daily_pnl:+.2f} | 累计: ${cum_pnl:+,.0f}\n持仓: {positions}个"
    )


# ============================================================
# 测试
# ============================================================

if __name__ == "__main__":
    print("发送测试通知...")
    notify("🤖 Polymarket Bot", "桌面通知测试成功!\n如果看到这条说明配置OK")
    print("完成! 检查右上角通知中心")
