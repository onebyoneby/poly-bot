"""检查所有跟单地址的最近交易活动"""
import asyncio
import sys
import time
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

from market_client import MarketClient

WALLETS = {
    "ChompChomp888": "0x842ac934cfff1467f27c167c42c318001e407422",
    "C4C4": "0x1455445e9a775cfa3fe9fc4b02bb4d2f682ae5cd",
    "weflyhigh": "0x03e8a544e97eeff5753bc1e90d46e5ef22af1697",
    "CemeterySun": "0x37c1874a60d348903594a96703e0507c518fc53a",
    "0p0jogggg": "0x6ac5bb06a9eb05641fd5e82640268b92f3ab4b6e",
    "DLEK": "0x6e82b93eb57b01a63027bd0c6d2f3f04934a752c",
    "TheOnlyHuman": "0x6ade597c0e2b43c0bf3542cada8a5e330d73f5b0",
    "0x2a2C": "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1",
}


async def main():
    client = MarketClient()
    now = int(time.time())
    print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')} (ts={now})")
    print(f"代理: {client._proxy or '无'}")

    for name, addr in WALLETS.items():
        print(f"\n{'='*70}")
        print(f"【{name}】 {addr}")
        print(f"{'='*70}")

        # 1) Activity API (跟单引擎用的)
        print(f"\n  --- Activity API (type=TRADE, limit=5) ---")
        activity = await client.get_user_activity_fast(addr, limit=5, start_ts=0)
        print(f"  返回 {len(activity)} 条")
        for i, a in enumerate(activity[:5], 1):
            side = a.get("side", "?")
            price = a.get("price", "?")
            size = a.get("size", "?")
            title = (a.get("title") or a.get("slug") or "?")[:45]
            ts = a.get("timestamp", 0)
            age = now - int(ts) if ts else 0
            ts_str = time.strftime("%m-%d %H:%M:%S", time.localtime(int(ts))) if ts else "?"
            print(f"    {i}. [{side:>4}] ${float(price) if price != '?' else 0:.3f} x "
                  f"{float(size) if size != '?' else 0:.0f}  |  {title}  |  {ts_str} ({age}s前)")

        # 2) Trades API (对比)
        print(f"\n  --- Trades API (limit=5) ---")
        trades = await client.get_user_trades_fast(addr, limit=5)
        print(f"  返回 {len(trades)} 条")
        for i, t in enumerate(trades[:5], 1):
            side = t.get("side", "?")
            price = t.get("price", "?")
            size = t.get("size", "?")
            title = (t.get("title") or t.get("market_slug") or t.get("slug") or "?")[:45]
            ts = t.get("timestamp", 0)
            age = now - int(ts) if ts else 0
            ts_str = time.strftime("%m-%d %H:%M:%S", time.localtime(int(ts))) if ts else "?"
            print(f"    {i}. [{side:>4}] ${float(price) if price != '?' else 0:.3f} x "
                  f"{float(size) if size != '?' else 0:.0f}  |  {title}  |  {ts_str} ({age}s前)")

        # 最后交易时间摘要
        latest_ts = 0
        for a in (activity or []):
            t = int(a.get("timestamp", 0))
            if t > latest_ts:
                latest_ts = t
        for t in (trades or []):
            ts_val = int(t.get("timestamp", 0))
            if ts_val > latest_ts:
                latest_ts = ts_val

        if latest_ts > 0:
            age_min = (now - latest_ts) / 60
            print(f"\n  >>> 最近交易: {time.strftime('%m-%d %H:%M:%S', time.localtime(latest_ts))}"
                  f" ({age_min:.0f}分钟前)")
        else:
            print(f"\n  >>> 未找到任何交易记录!")

    await client.close()


asyncio.run(main())
