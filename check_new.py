import asyncio, sys, time
from dotenv import load_dotenv
sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()
from market_client import MarketClient

WALLETS = {
    "CemeterySun": "0x37c1874a60d348903594a96703e0507c518fc53a",
    "0p0jogggg": "0x6ac5bb06a9eb05641fd5e82640268b92f3ab4b6e",
}

async def main():
    c = MarketClient()
    now = int(time.time())
    print(f"当前: {time.strftime('%H:%M:%S')}  代理: {c._proxy}")
    for name, addr in WALLETS.items():
        print(f"\n{'='*60}")
        print(f"【{name}】 {addr}")
        activity = await c.get_user_activity_fast(addr, limit=5, start_ts=0)
        if isinstance(activity, Exception):
            print(f"  Activity API 失败: {activity}")
            activity = []
        print(f"  Activity: {len(activity)} 条")
        for i, a in enumerate(activity[:5], 1):
            ts = int(a.get("timestamp", 0))
            age = (now - ts) if ts else 0
            print(f"    {i}. [{a.get('side','?'):>4}] ${float(a.get('price',0)):.3f} x {float(a.get('size',0)):.0f}"
                  f"  |  {(a.get('title') or '?')[:40]}  |  {time.strftime('%H:%M:%S', time.localtime(ts))} ({age//60}分钟前)")

        trades = await c.get_user_trades(addr, limit=5)
        print(f"  Trades: {len(trades)} 条")
        for i, t in enumerate(trades[:5], 1):
            ts = int(t.get("timestamp", 0))
            age = (now - ts) if ts else 0
            print(f"    {i}. [{t.get('side','?'):>4}] ${float(t.get('price',0)):.3f} x {float(t.get('size',0)):.0f}"
                  f"  |  {(t.get('title') or t.get('slug') or '?')[:40]}  |  {time.strftime('%H:%M:%S', time.localtime(ts))} ({age//60}分钟前)")
    await c.close()

asyncio.run(main())
