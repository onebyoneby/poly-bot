"""查询指定钱包的24小时PnL - 使用curl"""
import subprocess
import json
import time
import sys
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8")

DATA_API = "https://data-api.polymarket.com"

# T2 当前跟单钱包
T2_WALLETS = {
    "piggyery": "0x3f5ea0a8053e81ce2f59814118869322c35fe7db",
    "Wannac": "0xa8e089ade142c95538e06196e09c85681112ad50",
    "hugesportswhale": "0xb6aee7f281e13b23bd959127f6e2b3dccb4e4df8",
    "Anointed-Connect": "0x8f037a2e4fd49d11267f4ab874ab7ba745ac64d6",
    "ewelmealt": "0x07921379f7b31ef93da634b688b2fe36897db778",
}

# 已移除/屏蔽的钱包
REMOVED_WALLETS = {
    "ImJustKen": None,
    "weflyhigh": None,
    "432614799197": None,
    "DrPufferfish": None,
    "gmanas": None,
    "reachingthesky": None,
}


def curl_json(url):
    """用curl获取JSON"""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "10", url],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        print(f"  [ERR] {e}")
    return None


def get_leaderboard_day(username):
    """查询用户的DAY排行榜数据"""
    url = f"{DATA_API}/v1/leaderboard?timePeriod=DAY&username={urllib.parse.quote(username)}"
    data = curl_json(url)
    if data and len(data) > 0:
        return data[0]
    return None


def get_activity(address, limit=30):
    """获取用户最近交易"""
    url = f"{DATA_API}/activity?user={address}&limit={limit}&type=TRADE"
    data = curl_json(url)
    return data or []


def main():
    now = int(time.time())
    cutoff_24h = now - 86400
    print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')} (ts={now})")
    print(f"24h起始: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cutoff_24h))}")
    print()

    results = []
    all_wallets = list(T2_WALLETS.items()) + [(k, v) for k, v in REMOVED_WALLETS.items()]

    # 1) 查询排行榜DAY PnL
    print("正在查询排行榜 DAY PnL...")
    for name, addr in all_wallets:
        profile = get_leaderboard_day(name)
        pnl = float(profile["pnl"]) if profile and "pnl" in profile else None
        vol = float(profile["vol"]) if profile and "vol" in profile else None
        rank = profile.get("rank") if profile else None
        resolved_addr = addr or (profile.get("proxyWallet") if profile else None)

        # 查询活动
        trades_24h = 0
        total_recent = 0
        if resolved_addr:
            acts = get_activity(resolved_addr, limit=30)
            total_recent = len(acts)
            trades_24h = sum(1 for a in acts if int(a.get("timestamp", 0)) >= cutoff_24h)

        tier = "T2" if name in T2_WALLETS else "Removed"
        status = "OK" if profile else "NOT FOUND"
        print(f"  {tier:<8} {name:<20} PnL={f'${pnl:+,.2f}' if pnl is not None else 'N/A':<16} [{status}]")

        results.append({
            "name": name,
            "address": resolved_addr,
            "pnl": pnl,
            "volume": vol,
            "rank": rank,
            "tier": tier,
            "trades_24h": trades_24h,
            "total_recent": total_recent,
        })

    # 2) 打印汇总表格
    print()
    print("=" * 110)
    print(f"{'Polymarket 钱包 24h PnL 汇总':^110}")
    print("=" * 110)
    header = f"{'分类':<8} {'用户名':<20} {'地址':<16} {'DAY PnL':>14} {'日成交量':>14} {'24h交易':>8} {'排名':>10}"
    print(header)
    print("-" * 110)

    def print_row(r):
        addr_short = (r["address"][:6] + "..." + r["address"][-4:]) if r["address"] else "N/A"
        pnl_str = f"${r['pnl']:+,.2f}" if r["pnl"] is not None else "N/A"
        vol_str = f"${r['volume']:,.0f}" if r["volume"] is not None else "N/A"
        rank_str = f"#{r['rank']}" if r["rank"] else "N/A"
        act_str = str(r.get("trades_24h", "?"))

        if r["pnl"] is not None and r["pnl"] < 0:
            pnl_display = f"\033[91m{pnl_str:>14}\033[0m"
        elif r["pnl"] is not None and r["pnl"] > 0:
            pnl_display = f"\033[92m{pnl_str:>14}\033[0m"
        else:
            pnl_display = f"{pnl_str:>14}"

        print(f"{r['tier']:<8} {r['name']:<20} {addr_short:<16} {pnl_display} {vol_str:>14} {act_str:>8} {rank_str:>10}")

    # T2 先
    for r in results:
        if r["tier"] == "T2":
            print_row(r)
    print("-" * 110)
    # Removed
    for r in results:
        if r["tier"] == "Removed":
            print_row(r)
    print("=" * 110)

    # 汇总
    t2 = [r for r in results if r["tier"] == "T2" and r["pnl"] is not None]
    rm = [r for r in results if r["tier"] == "Removed" and r["pnl"] is not None]
    if t2:
        total = sum(r["pnl"] for r in t2)
        c = "\033[92m" if total >= 0 else "\033[91m"
        print(f"\nT2 当前钱包合计 DAY PnL: {c}${total:+,.2f}\033[0m  ({len(t2)}个)")
    if rm:
        total = sum(r["pnl"] for r in rm)
        c = "\033[92m" if total >= 0 else "\033[91m"
        print(f"已移除钱包合计 DAY PnL:   {c}${total:+,.2f}\033[0m  ({len(rm)}个)")

    # 排序
    all_with_pnl = [r for r in results if r["pnl"] is not None]
    all_with_pnl.sort(key=lambda x: x["pnl"])
    print(f"\n按PnL排序 (从差到好):")
    for r in all_with_pnl:
        marker = " [T2]" if r["tier"] == "T2" else " [已移除]"
        c = "\033[91m" if r["pnl"] < 0 else "\033[92m"
        print(f"  {c}${r['pnl']:+,.2f}\033[0m  {r['name']}{marker}")


if __name__ == "__main__":
    main()
