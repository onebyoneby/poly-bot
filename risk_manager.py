"""风控管理器 - 保护资金安全"""
import json
import logging
import os
import time
from datetime import date
from typing import Dict, Optional
import config

logger = logging.getLogger(__name__)

_STATE_FILE = "logs/risk_state.json"


class Position:
    """持仓记录"""
    def __init__(self, token_id: str, market_name: str, side: str,
                 entry_price: float, size: float, order_id: str):
        self.token_id = token_id
        self.market_name = market_name
        self.side = side
        self.entry_price = entry_price
        self.size = size
        self.order_id = order_id
        self.entry_time = time.time()
        self.pnl = 0.0

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id, "market_name": self.market_name,
            "side": self.side, "entry_price": self.entry_price,
            "size": self.size, "order_id": self.order_id,
            "entry_time": self.entry_time, "pnl": self.pnl,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        pos = cls(d["token_id"], d["market_name"], d["side"],
                  d["entry_price"], d["size"], d["order_id"])
        pos.entry_time = d.get("entry_time", time.time())
        pos.pnl = d.get("pnl", 0.0)
        return pos


class RiskManager:
    """风险管理"""

    def __init__(self):
        self.positions: Dict[str, Position] = {}  # token_id -> Position
        self.total_pnl = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.daily_loss = 0.0
        self._daily_loss_date = str(date.today())  # 日亏损对应日期
        self.max_daily_loss = config.MAX_POSITION_SIZE * 5  # 最大日亏损
        self._load_state()

    @property
    def total_exposure(self) -> float:
        return sum(p.size for p in self.positions.values())

    @property
    def win_rate(self) -> float:
        return self.win_count / self.trade_count if self.trade_count > 0 else 0

    def _reset_daily_loss_if_new_day(self):
        """如果跨天则重置日亏损"""
        today = str(date.today())
        if today != self._daily_loss_date:
            logger.info(f"跨日重置: 昨日亏损 ${self.daily_loss:.2f} → $0")
            self.daily_loss = 0.0
            self._daily_loss_date = today

    def can_open_position(self, size: float) -> tuple[bool, str]:
        """检查是否允许开新仓"""
        self._reset_daily_loss_if_new_day()
        if len(self.positions) >= config.MAX_POSITIONS:
            return False, f"已达最大持仓数 {config.MAX_POSITIONS}"

        if self.total_exposure + size > config.MAX_TOTAL_EXPOSURE:
            return False, f"总敞口将超过 ${config.MAX_TOTAL_EXPOSURE}"

        if size > config.MAX_POSITION_SIZE:
            return False, f"单笔超过 ${config.MAX_POSITION_SIZE}"

        if self.daily_loss >= self.max_daily_loss:
            return False, f"已达日最大亏损 ${self.max_daily_loss}"

        return True, "OK"

    def open_position(self, token_id: str, market_name: str, side: str,
                      entry_price: float, size: float, order_id: str):
        """记录开仓"""
        pos = Position(token_id, market_name, side, entry_price, size, order_id)
        self.positions[token_id] = pos
        logger.info(f"开仓: {market_name} | {side} | 价={entry_price:.4f} | 额=${size:.2f}")
        self._save_state()

    def close_position(self, token_id: str, exit_price: float) -> Optional[float]:
        """记录平仓，返回盈亏"""
        self._reset_daily_loss_if_new_day()
        if token_id not in self.positions:
            return None

        pos = self.positions.pop(token_id)
        if pos.side == "BUY":
            pnl = (exit_price - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - exit_price) * pos.size

        self.total_pnl += pnl
        self.trade_count += 1
        if pnl > 0:
            self.win_count += 1
        else:
            self.daily_loss += abs(pnl)

        logger.info(
            f"平仓: {pos.market_name} | PnL=${pnl:.2f} | "
            f"总PnL=${self.total_pnl:.2f} | 胜率={self.win_rate:.1%}"
        )
        self._save_state()
        return pnl

    def check_stop_loss(self, token_id: str, current_price: float) -> bool:
        """检查是否触发止损"""
        if token_id not in self.positions:
            return False

        pos = self.positions[token_id]
        if pos.side == "BUY":
            loss_pct = (pos.entry_price - current_price) / pos.entry_price
        else:
            loss_pct = (current_price - pos.entry_price) / pos.entry_price

        if loss_pct >= config.STOP_LOSS_PCT:
            logger.warning(f"触发止损: {pos.market_name} | 亏损 {loss_pct:.1%}")
            return True
        return False

    def get_summary(self) -> str:
        """获取风控摘要"""
        return (
            f"持仓={len(self.positions)} | 敞口=${self.total_exposure:.2f} | "
            f"交易={self.trade_count} | 胜率={self.win_rate:.1%} | "
            f"总PnL=${self.total_pnl:.2f} | 日亏=${self.daily_loss:.2f}"
        )

    def _save_state(self):
        """持久化风控状态到 JSON（崩溃恢复用）"""
        try:
            os.makedirs("logs", exist_ok=True)
            state = {
                "positions": {k: v.to_dict() for k, v in self.positions.items()},
                "total_pnl": self.total_pnl,
                "trade_count": self.trade_count,
                "win_count": self.win_count,
                "daily_loss": self.daily_loss,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp = _STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _STATE_FILE)
        except Exception as e:
            logger.warning(f"保存风控状态失败: {e}")

    def _load_state(self):
        """启动时恢复风控状态"""
        if not os.path.exists(_STATE_FILE):
            return
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            for tid, pd in state.get("positions", {}).items():
                self.positions[tid] = Position.from_dict(pd)
            self.total_pnl = state.get("total_pnl", 0.0)
            self.trade_count = state.get("trade_count", 0)
            self.win_count = state.get("win_count", 0)
            self.daily_loss = state.get("daily_loss", 0.0)
            logger.info(
                f"已恢复风控状态: {len(self.positions)}个持仓, "
                f"PnL=${self.total_pnl:.2f}, 交易={self.trade_count}"
            )
        except Exception as e:
            logger.warning(f"加载风控状态失败(重新开始): {e}")
