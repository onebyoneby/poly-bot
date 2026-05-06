#!/usr/bin/env python3
"""天气市场模拟下注结算脚本 — 对比实际天气，计算盈亏"""
import json, asyncio, aiohttp, re, os, sys
from datetime import datetime, timedelta

TRACKER = "/Users/qq/Projects/polymarket-bot/logs/weather_bets_tracker.json"
RESULTS = "/Users/qq/Projects/polymarket-bot/logs/weather_results.json"
PROXY = "http://192.168.0.155:7897"

CITIES_COORDS = {
    'tokyo': (35.6762, 139.6503, 'celsius'),
    'singapore': (1.3521, 103.8198, 'celsius'),
    'beijing': (39.9042, 116.4074, 'celsius'),
    'paris': (48.8566, 2.3522, 'celsius'),
    'chicago': (41.8781, -87.6298, 'fahrenheit'),
    'miami': (25.7617, -80.1918, 'fahrenheit'),
    'london': (51.5074, -0.1278, 'celsius'),
    'lucknow': (26.8467, 80.9462, 'celsius'),
    'los-angeles': (34.0522, -118.2437, 'fahrenheit'),
    'toronto': (43.6532, -79.3832, 'celsius'),
}


async def get_actual_temp(session, city, date_str):
    lat, lon, unit = CITIES_COORDS[city]
    url = (f"https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}"
           f"&daily=temperature_2m_max"
           f"&temperature_unit={unit}"
           f"&timezone=auto"
           f"&start_date={date_str}&end_date={date_str}")
    async with session.get(url, proxy=PROXY, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json()
        return data['daily']['temperature_2m_max'][0]


def check_condition(condition, actual_temp):
    actual_int = round(actual_temp)
    # =X
    m = re.match(r'=(\d+)', condition)
    if m:
        return actual_int == int(m.group(1))
    # <=X
    m = re.match(r'<=(\d+)', condition)
    if m:
        return actual_temp <= float(m.group(1))
    # >=X
    m = re.match(r'>=(\d+)', condition)
    if m:
        return actual_temp >= float(m.group(1))
    # X-Y range
    m = re.match(r'(\d+)-(\d+)', condition)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return lo <= actual_temp <= hi
    return None


async def settle():
    if not os.path.exists(TRACKER):
        print("无下注记录")
        return

    with open(TRACKER) as f:
        all_sessions = json.load(f)

    today = datetime.now().strftime('%Y-%m-%d')
    to_settle = [s for s in all_sessions if s['target_date'] <= today and s['status'] == 'PENDING']

    if not to_settle:
        print("无待结算注单")
        return

    async with aiohttp.ClientSession() as session:
        for sess in to_settle:
            target_date = sess['target_date']
            print(f"\n{'='*60}")
            print(f"结算 {target_date} 天气市场")
            print(f"{'='*60}")

            # Get actual temps
            actual_temps = {}
            cities_needed = set(b['city'] for b in sess['bets'])
            for city in cities_needed:
                try:
                    actual_temps[city] = await get_actual_temp(session, city, target_date)
                    print(f"  {city}: 实际最高温 {actual_temps[city]}°")
                except Exception as e:
                    print(f"  {city}: 获取失败 {e}")

            total_invested = 0
            total_payout = 0
            wins = 0
            losses = 0

            print(f"\n{'城市':>12} | {'条件':>10} | {'方向':>4} | {'价格':>6} | {'实际':>6} | {'结果':>6} | {'盈亏':>8}")
            print(f"{'─'*12} | {'─'*10} | {'─'*4} | {'─'*6} | {'─'*6} | {'─'*6} | {'─'*8}")

            for bet in sess['bets']:
                city = bet['city']
                actual = actual_temps.get(city)
                if actual is None:
                    continue

                cond_result = check_condition(bet['condition'], actual)
                if bet['side'] == 'YES':
                    won = cond_result
                else:
                    won = not cond_result

                amount = bet['amount']
                total_invested += amount

                if won:
                    payout = amount / bet['bet_price']
                    profit = payout - amount
                    total_payout += payout
                    wins += 1
                    result = "✅ 赢"
                else:
                    profit = -amount
                    losses += 1
                    result = "❌ 输"

                bet['actual_temp'] = actual
                bet['won'] = won
                bet['profit'] = profit

                print(f"{city:>12} | {bet['condition']:>10} | {bet['side']:>4} | ${bet['bet_price']:.3f} | {actual:>5.1f}° | {result} | ${profit:>+7.1f}")

            net = total_payout - total_invested
            sess['status'] = 'SETTLED'
            sess['actual_temps'] = actual_temps
            sess['result'] = {
                'wins': wins, 'losses': losses,
                'total_invested': total_invested,
                'total_payout': total_payout,
                'net_pnl': net,
                'roi': net / total_invested * 100 if total_invested > 0 else 0,
                'settled_at': datetime.now().isoformat(),
            }

            print(f"\n📊 {target_date} 结算: 投${total_invested:.0f} | 回${total_payout:.1f} | 净盈亏=${net:+.1f} | ROI={net/total_invested*100:+.1f}% | W{wins}/L{losses}")

    # Save updated tracker
    with open(TRACKER, 'w') as f:
        json.dump(all_sessions, f, indent=2, ensure_ascii=False)

    # Append to results history
    history = []
    if os.path.exists(RESULTS):
        with open(RESULTS) as f:
            history = json.load(f)
    for s in to_settle:
        if s.get('result'):
            history.append({
                'target_date': s['target_date'],
                **s['result'],
            })
    with open(RESULTS, 'w') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 结算完成，结果已保存")

if __name__ == "__main__":
    asyncio.run(settle())
