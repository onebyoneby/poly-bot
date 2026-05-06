"""查询 C4C4 的最近交易"""
import asyncio
import sys
import json
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

from market_client import MarketClient

C4C4_WALLET = "0x1455445e9a775cfa3fe9fc4b02bb4d2f682ae5cd"


async def main():
    client = MarketClient()
    print(f"代理: {client._proxy or '无'}")
    print(f"查询地址: {C4C4_WALLET}")
    print()

    # 1) Activity API
    print("=" * 70)
    print("【Activity API】C4C4 最近活动")
    print("=" * 70)
    activity = await client.get_user_activity_fast(C4C4_WALLET, limit=20, start_ts=0)
    print(f"返回 {len(activity)} 条记录\n")
    for i, a in enumerate(activity, 1):
        side = a.get("side", "?")
        price = a.get("price", "?")
        size = a.get("size", "?")
        title = (
            a.get("title")
            or a.get("question")
            or a.get("outcome")
            or a.get("slug")
            or a.get("market_slug", "")
        )
        ts = a.get("timestamp") or a.get("createdAt") or a.get("created_at", "?")
        asset_id = (a.get("asset_id") or a.get("asset") or "")[:16]
        print(f"  {i:>2}. [{side:>4}] ${float(price) if price != '?' else 0:.4f} x "
              f"{float(size) if size != '?' else 0:.1f}  |  {title[:50]}  |  {ts}")

    # 2) Trades API
    print()
    print("=" * 70)
    print("【Trades API】C4C4 最近交易")
    print("=" * 70)
    trades = await client.get_user_trades(C4C4_WALLET, limit=20)
    print(f"返回 {len(trades)} 条记录\n")
    for i, t in enumerate(trades, 1):
        side = t.get("side", "?")
        price = t.get("price", "?")
        size = t.get("size", "?")
        outcome = t.get("outcome", "?")
        market = t.get("market_slug") or t.get("title") or t.get("market", "")
        ts = t.get("timestamp") or t.get("createdAt") or t.get("matchTime", "?")
        print(f"  {i:>2}. [{side:>4}] ${float(price) if price != '?' else 0:.4f} x "
              f"{float(size) if size != '?' else 0:.1f}  |  {outcome}  |  {market[:40]}  |  {ts}")

    # 3) Positions API
    print()
    print("=" * 70)
    print("【Positions API】C4C4 当前持仓")
    print("=" * 70)
    positions = await client.get_user_positions(C4C4_WALLET)
    print(f"返回 {len(positions)} 条持仓\n")
    for i, p in enumerate(positions, 1):
        title = p.get("title") or p.get("question") or p.get("market_slug") or p.get("slug", "?")
        outcome = p.get("outcome", "?")
        size = p.get("size", 0)
        avg_price = p.get("avgPrice") or p.get("avg_price", "?")
        cur_price = p.get("curPrice") or p.get("cur_price", "?")
        pnl = p.get("pnl") or p.get("realizedPnl", "?")
        print(f"  {i:>2}. {title[:45]}  |  {outcome}  |  "
              f"量={size}  均价={avg_price}  现价={cur_price}  PnL={pnl}")

    # 4) 打印原始 JSON（前 2 条）方便调试
    print()
    print("=" * 70)
    print("【原始数据样本】Activity 前 2 条")
    print("=" * 70)
    for a in activity[:2]:
        print(json.dumps(a, indent=2, ensure_ascii=False)[:500])
        print()

    if trades:
        print("【原始数据样本】Trades 前 2 条")
        print("=" * 70)
        for t in trades[:2]:
            print(json.dumps(t, indent=2, ensure_ascii=False)[:500])
            print()

    await client.close()
    print("查询完成")


asyncio.run(main())
