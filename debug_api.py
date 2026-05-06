"""调试 API 原始响应"""
import asyncio
import sys
import aiohttp
from dotenv import load_dotenv
import os

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy")
C4C4 = "0x1455445e9a775cfa3fe9fc4b02bb4d2f682ae5cd"
WEFLYHIGH = "0x03e8a544e97eeff5753bc1e90d46e5ef22af1697"


async def main():
    print(f"代理: {proxy}")

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:

        # 1) Activity API - C4C4
        url = f"https://data-api.polymarket.com/activity?user={C4C4}&limit=3&type=TRADE"
        print(f"\n[1] Activity API C4C4: {url}")
        async with session.get(url, proxy=proxy) as resp:
            print(f"    HTTP {resp.status}")
            body = await resp.text()
            print(f"    响应长度: {len(body)}")
            print(f"    前500字符: {body[:500]}")

        # 2) Trades API - C4C4
        url = f"https://data-api.polymarket.com/trades?user={C4C4}&limit=3"
        print(f"\n[2] Trades API C4C4: {url}")
        async with session.get(url, proxy=proxy) as resp:
            print(f"    HTTP {resp.status}")
            body = await resp.text()
            print(f"    响应长度: {len(body)}")
            print(f"    前500字符: {body[:500]}")

        # 3) Trades API - weflyhigh
        url = f"https://data-api.polymarket.com/trades?user={WEFLYHIGH}&limit=3"
        print(f"\n[3] Trades API weflyhigh: {url}")
        async with session.get(url, proxy=proxy) as resp:
            print(f"    HTTP {resp.status}")
            body = await resp.text()
            print(f"    响应长度: {len(body)}")
            print(f"    前500字符: {body[:500]}")

        # 4) 对比: 不带 user 参数，查全局最近交易
        url = f"https://data-api.polymarket.com/trades?limit=3"
        print(f"\n[4] Trades API 全局 (无user): {url}")
        async with session.get(url, proxy=proxy) as resp:
            print(f"    HTTP {resp.status}")
            body = await resp.text()
            print(f"    响应长度: {len(body)}")
            print(f"    前500字符: {body[:500]}")

        # 5) 用 profileAddress 参数试试
        url = f"https://data-api.polymarket.com/trades?maker={C4C4}&limit=3"
        print(f"\n[5] Trades API maker=C4C4: {url}")
        async with session.get(url, proxy=proxy) as resp:
            print(f"    HTTP {resp.status}")
            body = await resp.text()
            print(f"    响应长度: {len(body)}")
            print(f"    前500字符: {body[:500]}")

        # 6) Polymarket profile API
        url = f"https://gamma-api.polymarket.com/profiles/{C4C4}"
        print(f"\n[6] Profile API C4C4: {url}")
        try:
            async with session.get(url, proxy=proxy) as resp:
                print(f"    HTTP {resp.status}")
                body = await resp.text()
                print(f"    前500字符: {body[:500]}")
        except Exception as e:
            print(f"    错误: {e}")

        # 7) 排行榜搜 C4C4
        url = f"https://data-api.polymarket.com/v1/leaderboard?userName=c4c4&timePeriod=ALL&limit=1"
        print(f"\n[7] 排行榜搜 c4c4: {url}")
        async with session.get(url, proxy=proxy) as resp:
            print(f"    HTTP {resp.status}")
            body = await resp.text()
            print(f"    前500字符: {body[:500]}")


asyncio.run(main())
