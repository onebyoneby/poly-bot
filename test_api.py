"""快速测试 API 连通性"""
import asyncio
import sys
import os
import aiohttp
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()


async def test_direct():
    """直接测试 API 是否可达"""
    proxy = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    print(f"[1] 代理设置: {proxy or '(无)'}")

    urls = [
        ("Gamma API", "https://gamma-api.polymarket.com/markets?limit=2&active=true"),
        ("CLOB API", "https://clob.polymarket.com/midpoint?token_id=test"),
        ("Data API", "https://data-api.polymarket.com/v1/leaderboard?timePeriod=DAY&limit=1"),
    ]

    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for name, url in urls:
            print(f"\n[2] 测试 {name}: {url[:60]}...")
            try:
                async with session.get(url, proxy=proxy) as resp:
                    status = resp.status
                    body = await resp.text()
                    print(f"    HTTP {status} | 响应长度: {len(body)} 字符")
                    if status == 200 and len(body) > 10:
                        print(f"    --> OK! 数据正常")
                        print(f"    预览: {body[:150]}...")
                    else:
                        print(f"    --> 异常: {body[:200]}")
            except aiohttp.ClientConnectorError as e:
                print(f"    --> 连接失败: {e}")
                print(f"    可能原因: 网络不通或被防火墙拦截")
            except asyncio.TimeoutError:
                print(f"    --> 超时 (15s)")
                print(f"    可能原因: API 被墙, 需要代理")
            except Exception as e:
                print(f"    --> 错误: {type(e).__name__}: {e}")


async def test_with_proxy(proxy_url: str):
    """使用指定代理测试"""
    print(f"\n{'='*60}")
    print(f"使用代理重试: {proxy_url}")
    print(f"{'='*60}")

    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    url = "https://gamma-api.polymarket.com/markets?limit=2&active=true"
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, proxy=proxy_url) as resp:
                body = await resp.text()
                print(f"HTTP {resp.status} | 响应长度: {len(body)}")
                if resp.status == 200 and len(body) > 10:
                    print("--> 代理可用! API 正常返回数据")
                    print(f"预览: {body[:200]}...")
                else:
                    print(f"--> 代理连通但返回异常: {body[:200]}")
        except Exception as e:
            print(f"--> 代理连接失败: {type(e).__name__}: {e}")


async def main():
    await test_direct()

    print("\n" + "=" * 60)
    print("诊断结论:")
    print("=" * 60)
    print("""
如果上面显示 '超时' 或 '连接失败', 说明 Polymarket API 在你的网络下不可直达。

解决方案: 在 .env 文件中添加代理设置, 例如:

  # Clash/V2Ray 默认端口
  https_proxy=http://127.0.0.1:7890
  http_proxy=http://127.0.0.1:7890

  # 或 SOCKS5 代理
  # https_proxy=socks5://127.0.0.1:1080

常见代理端口:
  Clash: 7890
  V2Ray: 10809 或 10808
  SSR:   1080

添加后重新运行此脚本测试。
""")

    # 如果命令行传了代理地址, 自动测试
    if len(sys.argv) > 1:
        await test_with_proxy(sys.argv[1])


asyncio.run(main())
