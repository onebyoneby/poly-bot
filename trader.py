"""交易执行器 - 使用 py-clob-client 下单"""
import asyncio
import logging
import time
import uuid
from typing import Optional
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
import config

logger = logging.getLogger(__name__)

_dry_run_seq = 0

def _gen_dry_order_id() -> str:
    global _dry_run_seq
    _dry_run_seq += 1
    ts = int(time.time() * 1000)
    return f"DRY-{ts}-{_dry_run_seq:04d}-{uuid.uuid4().hex[:8]}"


class Trader:
    """交易执行封装"""

    def __init__(self):
        self.dry_run = config.DRY_RUN
        self._client: Optional[ClobClient] = None
        self._initialized = False

    def _init_client(self):
        """延迟初始化CLOB客户端(需要私钥)"""
        if self._initialized:
            return
        if not config.PRIVATE_KEY or config.PRIVATE_KEY.startswith("你的"):
            logger.warning("未配置私钥，仅可使用DRY_RUN模式")
            self.dry_run = True
            self._initialized = True
            return

        self._client = ClobClient(
            config.CLOB_HOST,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=0,  # EOA钱包
            funder=config.FUNDER_ADDRESS,
        )
        # 创建或获取API凭证
        try:
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            logger.info("CLOB客户端初始化成功")
        except Exception as e:
            logger.error(f"CLOB客户端初始化失败: {e}")
            self.dry_run = True
        self._initialized = True

    def _resolve_order_type(self, order_type) -> OrderType:
        """将字符串或 OrderType 枚举统一为 OrderType"""
        if order_type is None:
            return OrderType.GTC
        if isinstance(order_type, str):
            return getattr(OrderType, order_type.upper(), OrderType.GTC)
        return order_type

    def buy(self, token_id: str, price: float, size: float,
            market_name: str = "", order_type=None) -> Optional[str]:
        """
        买入限价单
        token_id: 代币ID
        price: 价格 (0-1)
        size: 数量
        order_type: 订单类型 - None/"GTC"/"FOK"/"GTD" 或 OrderType 枚举
        返回: 订单ID 或 None
        """
        self._init_client()
        ot = self._resolve_order_type(order_type)

        if size > config.MAX_POSITION_SIZE:
            logger.warning(f"下单金额 ${size} 超过单笔限额 ${config.MAX_POSITION_SIZE}，已截断")
            size = config.MAX_POSITION_SIZE

        if price <= 0 or price >= 1:
            logger.error(f"价格 {price} 不合法，必须在 0-1 之间")
            return None

        ot_name = ot.name if hasattr(ot, 'name') else str(ot)
        if self.dry_run:
            order_id = _gen_dry_order_id()
            cost_usdc = round(price * size, 2)
            logger.info(
                f"[模拟买入] {market_name}\n"
                f"         POST {config.CLOB_HOST}/order\n"
                f"         OrderArgs(side=BUY, price={price:.4f}, size={size:.2f}, type={ot_name})\n"
                f"         token_id={token_id[:50]}...\n"
                f"         orderID={order_id}\n"
                f"         预计花费: ${cost_usdc} USDC"
            )
            return order_id

        try:
            order_args = OrderArgs(
                price=price,
                size=size,
                side="BUY",
                token_id=token_id,
            )
            signed_order = self._client.create_order(order_args)
            result = self._client.post_order(signed_order, ot)
            order_id = result.get("orderID") if isinstance(result, dict) else None
            if not order_id:
                logger.error(f"买入异常: 交易所未返回有效orderID | response={result}")
                return None
            logger.info(f"买入成功 {market_name} | 价格={price:.4f} | 量={size:.2f} | {ot} | 订单={order_id}")
            return order_id
        except Exception as e:
            logger.error(f"买入失败: {e}")
            return None

    def sell(self, token_id: str, price: float, size: float,
             market_name: str = "", order_type=None) -> Optional[str]:
        """卖出限价单"""
        self._init_client()
        ot = self._resolve_order_type(order_type)

        if price <= 0 or price >= 1:
            logger.error(f"价格 {price} 不合法")
            return None

        ot_name = ot.name if hasattr(ot, 'name') else str(ot)
        if self.dry_run:
            order_id = _gen_dry_order_id()
            revenue_usdc = round(price * size, 2)
            logger.info(
                f"[模拟卖出] {market_name}\n"
                f"         POST {config.CLOB_HOST}/order\n"
                f"         OrderArgs(side=SELL, price={price:.4f}, size={size:.2f}, type={ot_name})\n"
                f"         token_id={token_id[:50]}...\n"
                f"         orderID={order_id}\n"
                f"         预计收回: ${revenue_usdc} USDC"
            )
            return order_id

        try:
            order_args = OrderArgs(
                price=price,
                size=size,
                side="SELL",
                token_id=token_id,
            )
            signed_order = self._client.create_order(order_args)
            result = self._client.post_order(signed_order, ot)
            order_id = result.get("orderID") if isinstance(result, dict) else None
            if not order_id:
                logger.error(f"卖出异常: 交易所未返回有效orderID | response={result}")
                return None
            logger.info(f"卖出成功 {market_name} | 价格={price:.4f} | 量={size:.2f} | {ot} | 订单={order_id}")
            return order_id
        except Exception as e:
            logger.error(f"卖出失败: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        self._init_client()
        if self.dry_run:
            logger.info(f"[模拟] 取消订单 {order_id}")
            return True
        try:
            self._client.cancel(order_id)
            logger.info(f"取消成功: {order_id}")
            return True
        except Exception as e:
            logger.error(f"取消失败: {e}")
            return False

    def cancel_all(self) -> bool:
        """取消所有订单"""
        self._init_client()
        if self.dry_run:
            logger.info("[模拟] 取消所有订单")
            return True
        try:
            self._client.cancel_all()
            logger.info("已取消所有订单")
            return True
        except Exception as e:
            logger.error(f"取消所有订单失败: {e}")
            return False

    def get_open_orders(self) -> list:
        """获取所有未成交订单"""
        self._init_client()
        if self.dry_run:
            return []
        try:
            return self._client.get_orders() or []
        except Exception as e:
            logger.error(f"获取订单失败: {e}")
            return []

    # ===== 异步包装（避免实盘 HTTP 调用阻塞事件循环）=====

    async def buy_async(self, token_id: str, price: float, size: float,
                        market_name: str = "", order_type=None) -> Optional[str]:
        """异步买入 — DRY_RUN 直接返回，实盘走线程池"""
        if self.dry_run or not self._initialized:
            return self.buy(token_id, price, size, market_name, order_type)
        return await asyncio.to_thread(
            self.buy, token_id, price, size, market_name, order_type
        )

    async def sell_async(self, token_id: str, price: float, size: float,
                         market_name: str = "", order_type=None) -> Optional[str]:
        """异步卖出 — DRY_RUN 直接返回，实盘走线程池"""
        if self.dry_run or not self._initialized:
            return self.sell(token_id, price, size, market_name, order_type)
        return await asyncio.to_thread(
            self.sell, token_id, price, size, market_name, order_type
        )
