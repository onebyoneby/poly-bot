#!/usr/bin/env python3
"""
Polymarket 统一运行器
====================
一个进程运行所有模块:
  1. 跟单引擎 (RealtimeCopyEngine)
  2. 自动管理员 (auto_manager)
  3. 天气机器人 (WeatherBot)

用法:
  python run_all.py              # 运行所有模块 (DRY_RUN)
  python run_all.py --live       # 实盘模式
  python run_all.py --no-copy    # 不启动跟单引擎
  python run_all.py --no-manager # 不启动自动管理员
  python run_all.py --no-weather # 不启动天气机器人
"""
import asyncio
import argparse
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from rich.console import Console
from rich.panel import Panel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from market_client import MarketClient
from trader import Trader
from strategies.realtime_copy import RealtimeCopyEngine
from weather_bot import WeatherBot, SCAN_INTERVAL as WEATHER_SCAN_INTERVAL
import auto_manager
from wechat_notifier import WeChatReporter, PUSH_ENABLED

console = Console()
shutdown_event = asyncio.Event()


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                "trading.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            ),
        ],
    )


# ============================================================
# 模块包装器 — 每个模块独立运行，互不影响
# ============================================================

async def run_copy_engine(client: MarketClient):
    """跟单引擎 — 2秒轮询大佬钱包"""
    logger = logging.getLogger("runner.copy")
    logger.info("跟单引擎启动")
    trader = Trader()
    engine = RealtimeCopyEngine(client, trader)

    try:
        # 用 shutdown_event 配合引擎停止
        task = asyncio.create_task(engine.run())
        await asyncio.wait(
            [task, asyncio.create_task(shutdown_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not task.done():
            engine.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    except Exception as e:
        logger.error(f"跟单引擎异常退出: {e}", exc_info=True)
    finally:
        logger.info("跟单引擎已停止")


async def run_auto_manager():
    """自动管理员 — 每5分钟检查并调整"""
    logger = logging.getLogger("runner.manager")
    logger.info("自动管理员启动")

    # 初始化 auto_manager 的状态
    auto_manager.state.last_wallet_refresh = time.time()
    auto_manager.state.last_weather_report = 0

    try:
        while not shutdown_event.is_set():
            try:
                await auto_manager.check_and_adjust()
            except Exception as e:
                logger.error(f"管理员检查异常: {e}", exc_info=True)

            # 可中断的等待
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=auto_manager.CHECK_INTERVAL,
                )
                break  # shutdown triggered
            except asyncio.TimeoutError:
                pass  # normal timeout, continue loop
    except Exception as e:
        logger.error(f"自动管理员异常退出: {e}", exc_info=True)
    finally:
        logger.info("自动管理员已停止")


async def run_weather_bot():
    """天气机器人 — 每小时扫描天气市场"""
    logger = logging.getLogger("runner.weather")
    logger.info("天气机器人启动")

    bot = WeatherBot()
    try:
        while not shutdown_event.is_set():
            try:
                await bot.run_once()
            except Exception as e:
                logger.error(f"天气扫描异常: {e}", exc_info=True)

            # 可中断的等待
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=WEATHER_SCAN_INTERVAL,
                )
                break
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        logger.error(f"天气机器人异常退出: {e}", exc_info=True)
    finally:
        await bot.close()
        logger.info("天气机器人已停止")


# ============================================================
# 状态 API — 供 WebUI 读取
# ============================================================

def write_runner_state(modules: dict):
    """写入运行器状态供 WebUI 读取"""
    import json
    state = {
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "modules": modules,
        "dry_run": config.DRY_RUN,
    }
    state_path = os.path.join(os.path.dirname(__file__), "logs", "runner_state.json")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


# ============================================================
# 主入口
# ============================================================

async def main():
    parser = argparse.ArgumentParser(description="Polymarket 统一运行器")
    parser.add_argument("--live", action="store_true", help="实盘模式")
    parser.add_argument("--no-copy", action="store_true", help="不启动跟单引擎")
    parser.add_argument("--no-manager", action="store_true", help="不启动自动管理员")
    parser.add_argument("--no-weather", action="store_true", help="不启动天气机器人")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("runner")

    if args.live:
        config.DRY_RUN = False

    # 信号处理
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: shutdown_event.set())
        except (OSError, NotImplementedError):
            signal.signal(sig, lambda s, f: shutdown_event.set())

    mode = "[red bold]实盘交易[/red bold]" if not config.DRY_RUN else "[green bold]模拟交易(DRY_RUN)[/green bold]"
    modules_info = []
    if not args.no_copy:
        modules_info.append("跟单引擎")
    if not args.no_manager:
        modules_info.append("自动管理员")
    if not args.no_weather:
        modules_info.append("天气机器人")

    console.print(Panel(
        f"模式: {mode}\n"
        f"模块: {' | '.join(modules_info)}\n"
        f"PID: {os.getpid()}\n"
        f"钱包: T1={len(config.COPY_TIER1_WALLETS)} T2={len(config.COPY_TIER2_WALLETS)}",
        title="[bold cyan]Polymarket 统一运行器[/bold cyan]",
        border_style="cyan",
    ))

    if not config.DRY_RUN:
        console.print("[yellow]⚠ 实盘模式! 3秒后启动...[/yellow]")
        await asyncio.sleep(3)

    # 共享 MarketClient
    client = MarketClient()

    # 记录模块状态
    active_modules = {}
    tasks = []

    if not args.no_copy:
        active_modules["copy_engine"] = "running"
        tasks.append(asyncio.create_task(run_copy_engine(client), name="copy_engine"))

    if not args.no_manager:
        active_modules["auto_manager"] = "running"
        tasks.append(asyncio.create_task(run_auto_manager(), name="auto_manager"))

    if not args.no_weather:
        active_modules["weather_bot"] = "running"
        tasks.append(asyncio.create_task(run_weather_bot(), name="weather_bot"))

    # 微信汇报模块
    if PUSH_ENABLED:
        reporter = WeChatReporter()
        active_modules["wechat_notifier"] = "running"
        tasks.append(asyncio.create_task(
            reporter.run(shutdown_event), name="wechat_notifier"
        ))
        logger.info("微信汇报模块已启用")

    if not tasks:
        console.print("[yellow]没有启用任何模块，退出[/yellow]")
        return

    write_runner_state(active_modules)
    logger.info(f"已启动 {len(tasks)} 个模块: {', '.join(active_modules.keys())}")

    # 等待所有任务完成（或被 shutdown）
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        # 检查是否有异常退出的任务
        for task in done:
            if task.exception():
                logger.error(f"模块 {task.get_name()} 异常退出: {task.exception()}")

        # 如果有任务异常退出，触发全部停止
        if pending and not shutdown_event.is_set():
            logger.warning("有模块异常退出，停止所有模块...")
            shutdown_event.set()
            await asyncio.wait(pending, timeout=10)

    except asyncio.CancelledError:
        pass
    finally:
        # 清理
        shutdown_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()

        # 更新状态
        for k in active_modules:
            active_modules[k] = "stopped"
        write_runner_state(active_modules)

        console.print("[green]所有模块已安全停止[/green]")


if __name__ == "__main__":
    asyncio.run(main())
