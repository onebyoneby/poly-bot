"""Polymarket 市场数据客户端 - 封装所有API调用"""
import os
import json
import aiohttp
import asyncio
from typing import Optional
import config


class MarketClient:
    """统一的市场数据获取客户端"""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=15)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _get(self, url: str, params: dict = None):
        """统一GET请求，带代理和重试"""
        session = await self._get_session()
        for attempt in range(3):
            try:
                async with session.get(url, params=params, proxy=self._proxy) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json()
                        except (json.JSONDecodeError, aiohttp.ContentTypeError):
                            if attempt < 2:
                                await asyncio.sleep(1)
                                continue
                            return None
                    elif resp.status == 429 or resp.status >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue  # 重试请求
                    else:
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < 2:
                    await asyncio.sleep(1)
        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ========== Gamma API (市场数据) ==========

    @staticmethod
    def _parse_market(m: dict) -> dict:
        """解析市场数据中的JSON字符串字段"""
        for field in ("clobTokenIds", "outcomePrices", "outcomes"):
            val = m.get(field)
            if isinstance(val, str):
                try:
                    m[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
        return m

    async def get_markets(self, limit=100, active=True, closed=False) -> list:
        """获取所有活跃市场"""
        params = {"limit": limit, "active": str(active).lower(), "closed": str(closed).lower()}
        markets = await self._get(f"{config.GAMMA_HOST}/markets", params) or []
        return [self._parse_market(m) for m in markets]

    async def get_market(self, condition_id: str) -> dict:
        """获取单个市场详情"""
        return await self._get(f"{config.GAMMA_HOST}/markets/{condition_id}") or {}

    async def get_events(self, limit=50) -> list:
        """获取活跃事件"""
        params = {"limit": limit, "active": "true", "closed": "false"}
        return await self._get(f"{config.GAMMA_HOST}/events", params) or []

    async def search_markets(self, query: str) -> list:
        """搜索市场"""
        return await self._get(f"{config.GAMMA_HOST}/search", {"query": query}) or []

    # ========== CLOB API (订单簿/价格) ==========

    async def get_orderbook(self, token_id: str) -> dict:
        """获取订单簿"""
        return await self._get(f"{config.CLOB_HOST}/book", {"token_id": token_id}) or {}

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        """获取中间价"""
        data = await self._get(f"{config.CLOB_HOST}/midpoint", {"token_id": token_id})
        if data:
            mid = data.get("mid")
            return float(mid) if mid else None
        return None

    async def get_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """获取买/卖价"""
        data = await self._get(f"{config.CLOB_HOST}/price", {"token_id": token_id, "side": side})
        if data:
            price = data.get("price")
            return float(price) if price else None
        return None

    async def get_spread(self, token_id: str) -> Optional[dict]:
        """获取买卖价差"""
        return await self._get(f"{config.CLOB_HOST}/spread", {"token_id": token_id})

    async def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """获取最新成交价"""
        data = await self._get(f"{config.CLOB_HOST}/last-trade-price", {"token_id": token_id})
        if data:
            price = data.get("price")
            return float(price) if price else None
        return None

    async def get_price_history(self, token_id: str, interval: str = "1h", fidelity: int = 60) -> list:
        """获取价格历史"""
        params = {"market": token_id, "interval": interval, "fidelity": fidelity}
        data = await self._get(f"{config.CLOB_HOST}/prices-history", params)
        if data:
            return data.get("history", [])
        return []

    # ========== Data API (排行榜/交易记录) ==========

    # Leaderboard API v1: timePeriod=DAY|WEEK|MONTH|ALL, orderBy=PNL|VOL
    # 响应包含 proxyWallet, userName, vol, pnl, profileImage

    async def get_leaderboard(self, time_period: str = "DAY",
                              category: str = "OVERALL",
                              limit: int = 25) -> list:
        """获取排行榜
        time_period: DAY, WEEK, MONTH, ALL
        category: OVERALL, SPORTS, CRYPTO, POLITICS ...
        """
        params = {
            "timePeriod": time_period,
            "category": category,
            "orderBy": "PNL",
            "limit": limit,
        }
        return await self._get(f"{config.DATA_HOST}/v1/leaderboard", params) or []

    async def resolve_username(self, username: str) -> Optional[str]:
        """通过用户名查排行榜获取 proxyWallet 地址"""
        data = await self._get(
            f"{config.DATA_HOST}/v1/leaderboard",
            {"userName": username, "timePeriod": "ALL", "limit": 1},
        )
        if data and isinstance(data, list):
            for entry in data:
                if (entry.get("userName") or "").lower() == username.lower():
                    return entry.get("proxyWallet", "")
            if data:
                return data[0].get("proxyWallet", "")
        return None

    async def get_user_trades(self, address: str, limit: int = 50) -> list:
        """获取某地址的交易记录"""
        return await self._get(f"{config.DATA_HOST}/trades", {"user": address, "limit": limit}) or []

    async def get_user_positions(self, address: str) -> list:
        """获取某地址的持仓"""
        return await self._get(f"{config.DATA_HOST}/positions", {"user": address}) or []

    async def get_market_trades(self, condition_id: str, limit: int = 100) -> list:
        """获取某市场的最近交易"""
        return await self._get(f"{config.DATA_HOST}/trades", {"market": condition_id, "limit": limit}) or []

    # ========== 快速轮询（跟单引擎专用）==========

    async def get_user_activity_fast(self, address: str, limit: int = 30):
        """通过 Activity API 快速获取钱包最近交易。
        不使用 start 参数——靠调用方 _seen_set 去重，更可靠。
        返回 list 表示成功（可能为空），返回 Exception 表示请求失败。
        """
        session = await self._get_session()
        url = f"{config.DATA_HOST}/activity"
        params: dict = {
            "user": address,
            "limit": limit,
            "type": "TRADE",
        }
        try:
            fast_timeout = aiohttp.ClientTimeout(total=5, connect=3)
            async with session.get(url, params=params, proxy=self._proxy,
                                   timeout=fast_timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                return ConnectionError(f"HTTP {resp.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return e

    async def get_user_trades_fast(self, address: str, limit: int = 10) -> list:
        """快速获取某地址的最近交易（短超时，适合2秒高频轮询）"""
        session = await self._get_session()
        url = f"{config.DATA_HOST}/trades"
        params = {"user": address, "limit": limit}
        try:
            fast_timeout = aiohttp.ClientTimeout(total=5, connect=3)
            async with session.get(url, params=params, proxy=self._proxy,
                                   timeout=fast_timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        return []

    async def get_user_profile(self, username: str) -> dict:
        """通过用户名获取 Polymarket 个人资料（用于解析钱包地址）"""
        data = await self._get(f"{config.GAMMA_HOST}/public-profile", {"address": username})
        if isinstance(data, dict):
            return data
        return {}
