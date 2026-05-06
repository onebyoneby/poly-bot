"""验证 Polymarket Activity API 的 start 参数行为"""
import asyncio, sys, time, os
from dotenv import load_dotenv
sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()
import aiohttp

C4C4 = "0x1455445e9a775cfa3fe9fc4b02bb4d2f682ae5cd"
PROXY = os.environ.get("https_proxy") or os.environ.get("http_proxy")

async def main():
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        print(f"代理: {PROXY}")
        now = int(time.time())

        # 测试1: 不带 start 参数
        url = "https://data-api.polymarket.com/activity"
        params1 = {"user": C4C4, "limit": 5, "type": "TRADE"}
        print(f"\n=== 测试1: 不带 start 参数 ===")
        async with s.get(url, params=params1, proxy=PROXY) as r:
            data1 = await r.json()
            print(f"HTTP {r.status}, 返回 {len(data1)} 条")
            for i, t in enumerate(data1[:3]):
                ts = t.get("timestamp", 0)
                print(f"  {i+1}. id={str(t.get('id','?'))[:20]} ts={ts} ({int(now-float(ts))}s前) side={t.get('side')} price={t.get('price')}")

        # 测试2: start = 1小时前 (应该返回1小时内的交易)
        start_1h = now - 3600
        params2 = {"user": C4C4, "limit": 5, "type": "TRADE", "start": start_1h}
        print(f"\n=== 测试2: start={start_1h} (1小时前) ===")
        async with s.get(url, params=params2, proxy=PROXY) as r:
            data2 = await r.json()
            print(f"HTTP {r.status}, 返回 {len(data2)} 条")
            for i, t in enumerate(data2[:3]):
                ts = t.get("timestamp", 0)
                print(f"  {i+1}. id={str(t.get('id','?'))[:20]} ts={ts} ({int(now-float(ts))}s前)")

        # 测试3: start = 当前时间 (应该返回空/很少)
        params3 = {"user": C4C4, "limit": 5, "type": "TRADE", "start": now}
        print(f"\n=== 测试3: start={now} (当前时间) ===")
        async with s.get(url, params=params3, proxy=PROXY) as r:
            data3 = await r.json()
            print(f"HTTP {r.status}, 返回 {len(data3)} 条")
            for i, t in enumerate(data3[:3]):
                ts = t.get("timestamp", 0)
                print(f"  {i+1}. id={str(t.get('id','?'))[:20]} ts={ts} ({int(now-float(ts))}s前)")

        # 测试4: start = 未来时间 (应该返回空)
        params4 = {"user": C4C4, "limit": 5, "type": "TRADE", "start": now + 9999}
        print(f"\n=== 测试4: start={now+9999} (未来) ===")
        async with s.get(url, params=params4, proxy=PROXY) as r:
            data4 = await r.json()
            print(f"HTTP {r.status}, 返回 {len(data4)} 条")

        # 对比结论
        print(f"\n=== 结论 ===")
        ids1 = {str(t.get("id")) for t in data1}
        ids2 = {str(t.get("id")) for t in data2}
        ids3 = {str(t.get("id")) for t in data3}
        print(f"无start: {len(data1)}条, start=1h前: {len(data2)}条, start=now: {len(data3)}条, start=未来: {len(data4)}条")
        if ids1 == ids2 and ids1 == ids3:
            print("!!! start 参数被完全忽略 - 三组返回相同的 ID !!!")
        elif len(data3) == 0:
            print("start=now 返回空 - start 参数工作正常 (过滤了历史交易)")
        elif len(data3) == len(data1):
            print("!!! start=now 返回同样数量 - start 参数可能被忽略 !!!")
        else:
            print(f"start 参数有效但行为不明确: 无start={len(data1)}, start=now={len(data3)}")

asyncio.run(main())
