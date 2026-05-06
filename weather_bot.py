"""
天气独立下注机器人

工作流程:
1. 扫描 Polymarket 所有活跃天气市场（温度/降水）
2. 调用 Open-Meteo 免费 API 获取天气预报
3. 计算预测概率 vs 市场价格，找到有优势的机会
4. 自动下注（DRY_RUN 模式模拟）

运行: python weather_bot.py
日志: logs/weather_bot.log
"""
import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

# 确保项目根目录在 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from market_client import MarketClient
from trader import Trader

# ============================================================
# 日志配置
# ============================================================
os.makedirs("logs", exist_ok=True)

logger = logging.getLogger("weather_bot")
logger.setLevel(logging.INFO)

# 文件日志
fh = logging.FileHandler("logs/weather_bot.log", encoding="utf-8")
fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(fh)

# 控制台日志
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(ch)

# ============================================================
# 配置
# ============================================================
PROXY = (
    os.environ.get("https_proxy")
    or os.environ.get("http_proxy")
    or "http://192.168.0.155:7897"
)

GAMMA_HOST = config.GAMMA_HOST
MIN_EDGE = float(os.getenv("WEATHER_MIN_EDGE", "0.08"))  # 最小优势 8%
BET_SIZE = float(os.getenv("WEATHER_BET_SIZE", "10"))     # 单笔 $10
MAX_BETS_PER_SCAN = int(os.getenv("WEATHER_MAX_BETS", "10"))
SCAN_INTERVAL = int(os.getenv("WEATHER_SCAN_INTERVAL", "3600"))  # 1小时扫描一次

# 支持的城市
CITIES = {
    "toronto":   (43.65, -79.38),
    "lucknow":   (26.85, 80.95),
    "london":    (51.51, -0.13),
    "tokyo":     (35.68, 139.69),
    "paris":     (48.86, 2.35),
    "chicago":   (41.88, -87.63),
    "miami":     (25.76, -80.19),
    "singapore": (1.35, 103.82),
    "beijing":   (39.90, 116.40),
    "new-york":  (40.71, -74.01),
    "los-angeles": (34.05, -118.24),
    "sydney":    (-33.87, 151.21),
    "mumbai":    (19.08, 72.88),
    "delhi":     (28.61, 77.21),
}

# ============================================================
# 温度解析
# ============================================================

def parse_temp_range(question: str) -> Optional[tuple[float, float, str]]:
    """解析市场问题中的温度区间"""
    q = question.strip()

    # "X°C or below" / "X°F or below"
    m = re.search(r'(-?\d+)\s*°\s*([FC])\s+(?:or\s+)?(?:lower|below)', q, re.I)
    if m:
        return (float('-inf'), float(m.group(1)), m.group(2).upper())

    # "X°C or higher" / "X°F or higher" / "or above"
    m = re.search(r'(-?\d+)\s*°\s*([FC])\s+(?:or\s+)?(?:higher|above)', q, re.I)
    if m:
        return (float(m.group(1)), float('inf'), m.group(2).upper())

    # "between X-Y°F" / "between X and Y°F"  (°F 范围通常是2度一格)
    m = re.search(r'between\s+(-?\d+)\s*[-–and]+\s*(-?\d+)\s*°\s*([FC])', q, re.I)
    if m:
        return (float(m.group(1)), float(m.group(2)), m.group(3).upper())

    # "X-Y°C" 直接范围
    m = re.search(r'(-?\d+)\s*[-–to]+\s*(-?\d+)\s*°\s*([FC])', q, re.I)
    if m:
        return (float(m.group(1)), float(m.group(2)), m.group(3).upper())

    # 精确温度: "be X°C on"
    m = re.search(r'be\s+(-?\d+)\s*°\s*([FC])\s+on', q, re.I)
    if m:
        return (float(m.group(1)), float(m.group(1)), m.group(2).upper())

    # "X°F or below" 末尾格式
    m = re.search(r'(-?\d+)\s*°\s*([FC])\s+or\s+(?:lower|below)', q, re.I)
    if m:
        return (float('-inf'), float(m.group(1)), m.group(2).upper())

    return None


def parse_city_from_slug(slug: str) -> Optional[tuple[str, float, float]]:
    """从 slug 中提取城市"""
    slug_lower = slug.lower()
    # 精确匹配（长名优先）
    for city in sorted(CITIES.keys(), key=len, reverse=True):
        if city in slug_lower:
            lat, lon = CITIES[city]
            return (city, lat, lon)
    return None


def parse_date_from_slug(slug: str) -> Optional[str]:
    """从 slug 提取日期"""
    months = {
        'january': '01', 'february': '02', 'march': '03', 'april': '04',
        'may': '05', 'june': '06', 'july': '07', 'august': '08',
        'september': '09', 'october': '10', 'november': '11', 'december': '12',
    }
    for month_name, month_num in months.items():
        m = re.search(rf'{month_name}-(\d{{1,2}})-(\d{{4}})', slug, re.I)
        if m:
            return f"{m.group(2)}-{month_num}-{int(m.group(1)):02d}"
        m = re.search(rf'{month_name}-(\d{{1,2}})(?:\?|$)', slug, re.I)
        if m:
            year = datetime.now().year
            return f"{year}-{month_num}-{int(m.group(1)):02d}"
    return None


# ============================================================
# Open-Meteo
# ============================================================

class OpenMeteo:
    BASE = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, proxy=None):
        self._proxy = proxy
        self._session = None

    async def _get_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_forecast(self, lat, lon, unit="celsius", days=16):
        session = await self._get_session()
        params = {
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "hourly": "temperature_2m",
            "temperature_unit": unit,
            "timezone": "auto",
            "forecast_days": days,
        }
        try:
            async with session.get(self.BASE, params=params, proxy=self._proxy) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            logger.warning(f"Open-Meteo 请求失败: {e}")
        return None

    async def get_max_temp(self, lat, lon, date_str, unit="celsius"):
        """获取指定日期的最高温"""
        data = await self.get_forecast(lat, lon, unit)
        if not data:
            return None
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        maxs = daily.get("temperature_2m_max", [])
        mins = daily.get("temperature_2m_min", [])
        for i, d in enumerate(dates):
            if d == date_str:
                return {"max": maxs[i], "min": mins[i]}
        return None


# ============================================================
# 概率计算
# ============================================================

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def calc_probability(forecast_max, low, high, sigma):
    """计算温度落入 [low, high] 的概率"""
    if high == float('inf'):
        return 1.0 - norm_cdf((low - forecast_max) / sigma)
    elif low == float('-inf'):
        return norm_cdf((high + 0.5 - forecast_max) / sigma)
    elif low == high:
        # 精确温度
        p_h = norm_cdf((high + 0.5 - forecast_max) / sigma)
        p_l = norm_cdf((low - 0.5 - forecast_max) / sigma)
        return max(p_h - p_l, 0.0)
    else:
        p_h = norm_cdf((high + 0.5 - forecast_max) / sigma)
        p_l = norm_cdf((low - 0.5 - forecast_max) / sigma)
        return max(p_h - p_l, 0.0)


def get_sigma(unit, days_ahead):
    """预报标准差（基于预报天数和单位）"""
    # °C 的标准差
    if days_ahead <= 1:
        s = 1.5
    elif days_ahead <= 3:
        s = 2.5
    elif days_ahead <= 7:
        s = 3.5
    else:
        s = 5.0
    return s if unit == "C" else s * 1.8


# ============================================================
# 主逻辑
# ============================================================

class WeatherBot:
    def __init__(self):
        self.client = MarketClient()
        self.trader = Trader()
        self.meteo = OpenMeteo(proxy=PROXY)
        self._bets = {}  # 已下注记录 {condition_id: {...}}
        self._bet_count = 0
        self._total_cost = 0.0
        self._results = []  # 所有分析结果

    async def close(self):
        await self.meteo.close()
        await self.client.close()

    async def find_weather_events(self) -> list[dict]:
        """搜索所有活跃天气事件"""
        events = []
        today = datetime.now()

        # 搜未来1-7天的温度市场（跳过当天，当天市场已接近结算）
        for days in range(1, 8):
            target = today + timedelta(days=days)
            month = target.strftime("%B").lower()
            day = target.day
            year = target.year

            for city in CITIES:
                slug = f"highest-temperature-in-{city}-on-{month}-{day}-{year}"
                data = await self.client._get(
                    f"{GAMMA_HOST}/events", {"slug": slug}
                )
                if data and isinstance(data, list) and data:
                    ev = data[0]
                    markets = [m for m in ev.get("markets", []) if not m.get("closed")]
                    if markets:
                        events.append({
                            "title": ev.get("title", ""),
                            "slug": slug,
                            "city": city,
                            "date": f"{year}-{target.month:02d}-{day:02d}",
                            "markets": markets,
                        })
                await asyncio.sleep(0.05)  # 避免限流

        logger.info(f"找到 {len(events)} 个活跃天气事件")
        return events

    async def analyze_event(self, event: dict) -> list[dict]:
        """分析一个天气事件的所有子市场，返回有优势的信号"""
        city = event["city"]
        date_str = event["date"]
        lat, lon = CITIES[city]

        # 确定温度单位（从第一个子市场判断）
        sample_q = event["markets"][0].get("question", "")
        unit = "F" if "°F" in sample_q else "C"
        meteo_unit = "fahrenheit" if unit == "F" else "celsius"

        # 获取预报
        forecast = await self.meteo.get_max_temp(lat, lon, date_str, meteo_unit)
        if not forecast:
            logger.warning(f"  {city} {date_str} 预报获取失败")
            return []

        forecast_max = forecast["max"]
        days_ahead = max((datetime.strptime(date_str, "%Y-%m-%d") - datetime.now()).days, 0)
        sigma = get_sigma(unit, days_ahead)

        logger.info(f"\n{'='*60}")
        logger.info(f"  {city.upper()} {date_str} | 预报最高 {forecast_max:.1f}°{unit} | "
                     f"σ={sigma:.1f}°{unit} | {days_ahead}天后")
        logger.info(f"  {'子市场':<30} {'市场价':>7} {'预测':>7} {'优势':>7} {'信号':>6}")
        logger.info(f"  {'-'*65}")

        signals = []
        for m in event["markets"]:
            q = m.get("question", "")
            temp_range = parse_temp_range(q)
            if not temp_range:
                continue

            low, high, u = temp_range
            prices = m.get("outcomePrices", "[]")
            clob_ids = m.get("clobTokenIds", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)

            if not prices or not clob_ids:
                continue

            market_price = float(prices[0])
            predicted = calc_probability(forecast_max, low, high, sigma)
            edge = predicted - market_price

            # 标签
            if high == float('inf'):
                label = f">={low}°{u}"
            elif low == float('-inf'):
                label = f"<={high}°{u}"
            elif low == high:
                label = f"={low}°{u}"
            else:
                label = f"{low}-{high}°{u}"

            # 信号判定
            signal = ""
            sig_data = None

            # 过滤: 市场价太低(流动性差)或太高(已结算)的跳过
            if edge >= MIN_EDGE and 0.03 < market_price < 0.90:
                signal = "BUY Y"
                buy_price = min(market_price * 1.05, predicted - 0.03)
                buy_price = max(buy_price, 0.02)
                sig_data = {
                    "token_id": clob_ids[0],
                    "side": "BUY",
                    "price": round(buy_price, 4),
                    "size": BET_SIZE,
                    "market_name": f"[天气] {city} {date_str} {label}",
                    "reason": f"预报{forecast_max:.0f}°{u} 概率{predicted:.0%} vs 市场{market_price:.0%} 优势{edge:+.0%}",
                    "edge": edge,
                    "condition_id": m.get("conditionId", ""),
                    "city": city,
                    "target_date": date_str,
                    "condition": label,
                    "predicted": predicted,
                    "signal": "BUY Y",
                }
            elif edge <= -MIN_EDGE and 0.10 < market_price < 0.97 and len(clob_ids) > 1:
                signal = "BUY N"
                no_price = float(prices[1]) if len(prices) > 1 else 1 - market_price
                buy_price = min(no_price * 1.05, (1 - predicted) - 0.03)
                buy_price = max(buy_price, 0.02)
                sig_data = {
                    "token_id": clob_ids[1],
                    "side": "BUY",
                    "price": round(buy_price, 4),
                    "size": BET_SIZE,
                    "market_name": f"[天气] {city} {date_str} NOT {label}",
                    "reason": f"预报{forecast_max:.0f}°{u} 概率仅{predicted:.0%} 市场{market_price:.0%} 高估{-edge:.0%}",
                    "edge": abs(edge),
                    "condition_id": m.get("conditionId", ""),
                    "city": city,
                    "target_date": date_str,
                    "condition": label,
                    "predicted": predicted,
                    "signal": "BUY N",
                }

            logger.info(f"  {label:<30} {market_price:>6.1%} {predicted:>6.1%} {edge:>+6.1%} {signal:>6}")

            if sig_data:
                signals.append(sig_data)

            self._results.append({
                "city": city, "date": date_str, "label": label,
                "market_price": market_price, "predicted": predicted,
                "edge": edge, "signal": signal,
            })

        return signals

    async def place_bets(self, signals: list[dict]):
        """下注（按优势排序，取前N个）"""
        # 按优势从大到小排序
        signals.sort(key=lambda s: s["edge"], reverse=True)
        placed = 0

        for sig in signals:
            if placed >= MAX_BETS_PER_SCAN:
                break

            cid = sig["condition_id"]
            if cid in self._bets:
                logger.info(f"  跳过（已下注）: {sig['market_name']}")
                continue

            order_id = await self.trader.buy_async(
                sig["token_id"], sig["price"], sig["size"],
                sig["market_name"], order_type="FOK"
            )

            if order_id:
                self._bets[cid] = {
                    "order_id": order_id,
                    "market": sig["market_name"],
                    "price": sig["price"],
                    "size": sig["size"],
                    "edge": sig["edge"],
                    "reason": sig["reason"],
                    "time": datetime.now().strftime("%H:%M:%S"),
                }
                self._bet_count += 1
                self._total_cost += sig["size"]
                placed += 1

                logger.info(f"\n  >>> 下注 #{self._bet_count} <<<")
                logger.info(f"      {sig['market_name']}")
                logger.info(f"      {sig['reason']}")
                logger.info(f"      价格=${sig['price']:.4f} 金额=${sig['size']:.0f} 优势={sig['edge']:.1%}")
                logger.info(f"      orderID={order_id}")

                # 自动写入 tracker（实盘+模拟都记录）
                self._save_bet_to_tracker(sig, order_id)

        return placed

    def _save_bet_to_tracker(self, sig: dict, order_id: str):
        """将下注记录追加到 weather_bets_tracker.json"""
        tracker_path = os.path.join(os.path.dirname(__file__), "logs", "weather_bets_tracker.json")
        try:
            data = []
            if os.path.exists(tracker_path):
                with open(tracker_path, "r") as f:
                    data = json.load(f)

            today = datetime.now().strftime("%Y-%m-%d")
            target_date = sig.get("target_date", sig.get("date", ""))

            # 找到今天的记录或创建新的
            today_entry = None
            for entry in data:
                if entry.get("bet_date", "").startswith(today):
                    today_entry = entry
                    break
            if not today_entry:
                today_entry = {"bet_date": today, "target_date": target_date, "bets": []}
                data.append(today_entry)

            today_entry["bets"].append({
                "city": sig.get("city", ""),
                "date": target_date,
                "condition": sig.get("label", sig.get("condition", "")),
                "side": "YES" if "BUY Y" in sig.get("signal", sig.get("reason", "")) else "NO",
                "question": sig.get("market_name", ""),
                "market_price": sig.get("price", 0),
                "my_probability": sig.get("predicted", 0),
                "bet_price": sig.get("price", 0),
                "edge": sig.get("edge", 0),
                "amount": sig.get("size", 0),
                "order_id": order_id,
                "dry_run": config.DRY_RUN,
                "timestamp": datetime.now().isoformat(),
            })

            with open(tracker_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"写入 tracker 失败: {e}")

    async def run_once(self):
        """单次扫描 + 分析 + 下注"""
        logger.info(f"\n{'#'*60}")
        logger.info(f"天气机器人扫描 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"{'#'*60}")

        # 1. 搜索天气市场
        events = await self.find_weather_events()
        if not events:
            logger.info("未找到活跃天气市场")
            return

        # 2. 分析每个事件
        all_signals = []
        for ev in events:
            sigs = await self.analyze_event(ev)
            all_signals.extend(sigs)
            await asyncio.sleep(0.2)

        # 3. 下注
        if all_signals:
            logger.info(f"\n{'='*60}")
            logger.info(f"发现 {len(all_signals)} 个交易机会 (优势>={MIN_EDGE:.0%})")
            placed = await self.place_bets(all_signals)
            logger.info(f"本次下注 {placed} 笔")
        else:
            logger.info(f"\n无交易机会（所有优势 < {MIN_EDGE:.0%}）")

        # 4. 汇总报告
        self._print_summary()

    def _print_summary(self):
        """打印汇总报告"""
        logger.info(f"\n{'='*60}")
        logger.info(f"汇总报告")
        logger.info(f"{'='*60}")
        logger.info(f"  累计下注: {self._bet_count} 笔 | 总金额: ${self._total_cost:.0f}")
        logger.info(f"  DRY_RUN: {config.DRY_RUN}")

        if self._bets:
            logger.info(f"\n  当前下注:")
            for cid, bet in self._bets.items():
                logger.info(f"    [{bet['time']}] {bet['market']} "
                             f"${bet['size']:.0f}@{bet['price']:.3f} 优势={bet['edge']:.1%}")

        # 按城市汇总有优势的机会
        if self._results:
            opps = [r for r in self._results if r["signal"]]
            if opps:
                logger.info(f"\n  有优势的机会 ({len(opps)}个):")
                for r in sorted(opps, key=lambda x: abs(x["edge"]), reverse=True):
                    logger.info(f"    {r['city']:>12} {r['date']} {r['label']:<15} "
                                 f"市场={r['market_price']:.1%} 预测={r['predicted']:.1%} "
                                 f"优势={r['edge']:+.1%} {r['signal']}")

    async def run(self):
        """持续运行"""
        logger.info(f"天气下注机器人启动")
        logger.info(f"  最小优势: {MIN_EDGE:.0%}")
        logger.info(f"  单笔金额: ${BET_SIZE:.0f}")
        logger.info(f"  最大下注/轮: {MAX_BETS_PER_SCAN}")
        logger.info(f"  扫描间隔: {SCAN_INTERVAL}s")
        logger.info(f"  DRY_RUN: {config.DRY_RUN}")
        logger.info(f"  代理: {PROXY}")

        try:
            while True:
                try:
                    await self.run_once()
                except Exception as e:
                    logger.error(f"扫描异常: {e}", exc_info=True)

                logger.info(f"\n下次扫描: {SCAN_INTERVAL}s 后")
                await asyncio.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            logger.info("天气机器人停止")
        finally:
            await self.close()


async def main():
    bot = WeatherBot()
    # 先执行一次立即扫描，然后循环
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
