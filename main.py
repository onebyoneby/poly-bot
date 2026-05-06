#!/usr/bin/env python3
"""
Polymarket 智能交易系统
========================
策略组合:
  1. 套利策略 - YES+NO<$1时无风险套利 (排行榜 kch123, DrPufferfish 模式)
  2. 闪崩买入 - 概率突然下跌时抄底 (排行榜 CemeterySun, Len9311238 模式)
  3. 跟单策略 - 跟踪多个顶级交易者的共识 (排行榜 Theo4, Fredi9999 模式)
  4. 动量策略 - 跟随价格趋势 (排行榜 walletmobile, RepTrump 模式)
  5. 实时跟单 - 稳健保守方案，2秒轮询大佬钱包 (--copy 模式)

用法:
  python main.py              # 运行所有策略(DRY_RUN模式)
  python main.py --scan       # 只扫描不交易
  python main.py --live       # 实盘交易(需要私钥)
  python main.py --strategy arbitrage  # 只运行套利策略
  python main.py --copy       # 稳健跟单模式(DRY_RUN)
  python main.py --copy --live  # 实盘跟单
"""
import asyncio
import argparse
import logging
import signal
import sys
import time

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout

from market_client import MarketClient
from trader import Trader
from risk_manager import RiskManager
from strategies import ArbitrageStrategy, FlashCrashStrategy, CopyTradeStrategy, MomentumStrategy
from strategies.realtime_copy import RealtimeCopyEngine
import config

console = Console()
running = True


def setup_logging():
    from logging.handlers import RotatingFileHandler
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


def signal_handler(sig, frame):
    global running
    console.print("\n[yellow]收到停止信号，正在安全退出...[/yellow]")
    running = False


async def scan_only(client: MarketClient):
    """只扫描市场机会，不交易"""
    console.print(Panel("[bold cyan]市场扫描模式[/bold cyan]", subtitle="仅查看机会"))

    trader = Trader()
    trader.dry_run = True
    risk_mgr = RiskManager()

    strategies = [
        ArbitrageStrategy(client, trader, risk_mgr),
        FlashCrashStrategy(client, trader, risk_mgr),
        CopyTradeStrategy(client, trader, risk_mgr),
        MomentumStrategy(client, trader, risk_mgr),
    ]

    table = Table(title="扫描到的交易机会")
    table.add_column("策略", style="cyan")
    table.add_column("市场", style="white")
    table.add_column("方向", style="green")
    table.add_column("价格", style="yellow")
    table.add_column("建议金额", style="magenta")
    table.add_column("原因", style="blue")

    for strategy in strategies:
        console.print(f"  扫描中: [cyan]{strategy.name}[/cyan]...")
        try:
            signals = await strategy.scan()
            for sig in signals:
                table.add_row(
                    strategy.name,
                    sig["market_name"][:50],
                    sig["side"],
                    f"${sig['price']:.4f}",
                    f"${sig['size']:.2f}",
                    sig["reason"],
                )
        except Exception as e:
            console.print(f"  [red]{strategy.name} 扫描失败: {e}[/red]")

    console.print(table)


async def run_bot(client: MarketClient, strategy_filter: str = None):
    """运行交易机器人"""
    global running

    trader = Trader()
    risk_mgr = RiskManager()

    all_strategies = {
        "arbitrage": ArbitrageStrategy(client, trader, risk_mgr),
        "flash_crash": FlashCrashStrategy(client, trader, risk_mgr),
        "copy_trade": CopyTradeStrategy(client, trader, risk_mgr),
        "momentum": MomentumStrategy(client, trader, risk_mgr),
    }

    if strategy_filter:
        if strategy_filter not in all_strategies:
            console.print(f"[red]未知策略: {strategy_filter}[/red]")
            console.print(f"可用策略: {', '.join(all_strategies.keys())}")
            return
        strategies = [all_strategies[strategy_filter]]
    else:
        strategies = list(all_strategies.values())

    mode = "[red bold]实盘交易[/red bold]" if not config.DRY_RUN else "[green bold]模拟交易(DRY_RUN)[/green bold]"
    console.print(Panel(
        f"模式: {mode}\n"
        f"策略: {', '.join(s.name for s in strategies)}\n"
        f"单笔限额: ${config.MAX_POSITION_SIZE} | 总敞口: ${config.MAX_TOTAL_EXPOSURE}\n"
        f"扫描间隔: {config.SCAN_INTERVAL}秒",
        title="[bold cyan]Polymarket 交易系统[/bold cyan]",
        border_style="cyan",
    ))

    if not config.DRY_RUN:
        console.print("[yellow]⚠ 实盘模式! 3秒后启动...[/yellow]")
        await asyncio.sleep(3)

    cycle = 0
    while running:
        cycle += 1
        console.rule(f"[dim]第 {cycle} 轮扫描 | {time.strftime('%H:%M:%S')}[/dim]")

        for strategy in strategies:
            try:
                await strategy.run_once()
            except Exception as e:
                console.print(f"[red]{strategy.name} 异常: {e}[/red]")

        # 检查持仓止损
        for token_id in list(risk_mgr.positions.keys()):
            try:
                current = await client.get_midpoint(token_id)
                if current and risk_mgr.check_stop_loss(token_id, current):
                    pos = risk_mgr.positions[token_id]
                    trader.sell(token_id, current, pos.size, f"[止损] {pos.market_name}")
                    risk_mgr.close_position(token_id, current)
            except Exception:
                pass

        # 状态摘要
        console.print(f"  [dim]{risk_mgr.get_summary()}[/dim]")

        # 等待下一轮
        for _ in range(config.SCAN_INTERVAL):
            if not running:
                break
            await asyncio.sleep(1)

    # 退出清理
    console.print("[yellow]正在取消所有挂单...[/yellow]")
    trader.cancel_all()
    console.print("[green]交易系统已安全停止[/green]")


async def show_market_overview(client: MarketClient):
    """显示市场概览"""
    console.print(Panel("[bold cyan]市场概览[/bold cyan]"))

    markets = await client.get_markets(limit=20, active=True)

    table = Table(title="热门市场")
    table.add_column("#", style="dim")
    table.add_column("市场", style="white", max_width=60)
    table.add_column("交易量", style="green")
    table.add_column("流动性", style="cyan")

    for i, m in enumerate(markets[:20], 1):
        question = m.get("question", m.get("slug", ""))[:60]
        volume = float(m.get("volume", 0) or 0)
        liquidity = float(m.get("liquidity", 0) or 0)
        table.add_row(
            str(i),
            question,
            f"${volume:,.0f}",
            f"${liquidity:,.0f}",
        )

    console.print(table)


async def run_copy_mode(client: MarketClient):
    """运行实时跟单模式"""
    global running

    trader = Trader()
    engine = RealtimeCopyEngine(client, trader)

    def _stop(*_):
        global running
        running = False
        engine.stop()
        console.print("\n[yellow]收到停止信号，正在安全退出跟单引擎...[/yellow]")

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _stop)
        except OSError:
            pass

    if not config.DRY_RUN:
        console.print("[yellow]⚠ 实盘跟单模式! 3秒后启动...[/yellow]")
        await asyncio.sleep(3)

    await engine.run()


async def main():
    parser = argparse.ArgumentParser(description="Polymarket 智能交易系统")
    parser.add_argument("--scan", action="store_true", help="只扫描市场，不交易")
    parser.add_argument("--live", action="store_true", help="实盘交易模式")
    parser.add_argument("--overview", action="store_true", help="显示市场概览")
    parser.add_argument("--copy", action="store_true", help="稳健跟单模式（2秒轮询大佬钱包）")
    parser.add_argument("--strategy", type=str, help="只运行指定策略: arbitrage|flash_crash|copy_trade|momentum")
    args = parser.parse_args()

    setup_logging()
    signal.signal(signal.SIGINT, signal_handler)

    if args.live:
        config.DRY_RUN = False

    client = MarketClient()

    try:
        if args.copy:
            await run_copy_mode(client)
        elif args.overview:
            await show_market_overview(client)
        elif args.scan:
            await scan_only(client)
        else:
            await run_bot(client, args.strategy)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
