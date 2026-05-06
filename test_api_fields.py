"""检查 Activity API 返回的字段结构"""
import asyncio, sys, time, os, json
from dotenv import load_dotenv
sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()
import aiohttp

C4C4 = "0x1455445e9a775cfa3fe9fc4b02bb4d2f682ae5cd"
PROXY = os.environ.get("https_proxy") or os.environ.get("http_proxy")

async def main():
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        now = int(time.time())

        # Activity API
        url1 = "https://data-api.polymarket.com/activity"
        params1 = {"user": C4C4, "limit": 2, "type": "TRADE"}
        print("=== Activity API 原始响应 ===")
        async with s.get(url1, params=params1, proxy=PROXY) as r:
            data1 = await r.json()
            for i, t in enumerate(data1):
                print(f"\n--- Activity 第{i+1}条 ---")
                print(f"  所有字段: {sorted(t.keys())}")
                print(f"  id: {repr(t.get('id'))}")
                print(f"  transactionHash: {repr(t.get('transactionHash'))}")
                print(f"  transaction_hash: {repr(t.get('transaction_hash'))}")
                print(f"  timestamp: {t.get('timestamp')}")
                print(f"  side: {t.get('side')}")
                print(f"  price: {t.get('price')}")
                print(f"  size: {t.get('size')}")
                print(f"  title: {str(t.get('title',''))[:50]}")
                print(f"  asset: {str(t.get('asset',''))[:40]}")
                print(f"  asset_id: {repr(t.get('asset_id'))}")
                print(f"  market: {str(t.get('market',''))[:40]}")
                print(f"  conditionId: {repr(t.get('conditionId'))}")
                print(f"  condition_id: {repr(t.get('condition_id'))}")
                print(f"  完整JSON (前500字符):")
                print(f"  {json.dumps(t, ensure_ascii=False)[:500]}")

        # Trades API 对比
        url2 = "https://data-api.polymarket.com/trades"
        params2 = {"user": C4C4, "limit": 2}
        print("\n\n=== Trades API 原始响应 ===")
        async with s.get(url2, params=params2, proxy=PROXY) as r:
            data2 = await r.json()
            for i, t in enumerate(data2):
                print(f"\n--- Trades 第{i+1}条 ---")
                print(f"  所有字段: {sorted(t.keys())}")
                print(f"  id: {repr(t.get('id'))}")
                print(f"  transactionHash: {repr(t.get('transactionHash'))}")
                print(f"  timestamp: {t.get('timestamp')}")
                print(f"  side: {t.get('side')}")
                print(f"  price: {t.get('price')}")
                print(f"  size: {t.get('size')}")
                print(f"  title: {str(t.get('title',''))[:50]}")
                print(f"  asset_id: {repr(t.get('asset_id'))}")
                print(f"  market: {str(t.get('market',''))[:40]}")
                print(f"  conditionId: {repr(t.get('conditionId'))}")

asyncio.run(main())
