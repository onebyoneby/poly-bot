import requests

PROXY = "http://192.168.0.155:7897"
proxies = {"http": PROXY, "https": PROXY}

BASE_URL = "https://data-api.polymarket.com/v1/leaderboard"

t1_wallets = [
    ("KaneAnalytics", "0x7e3a1f95c558f39a51ff334d789e3e039b553246"),
    ("abura2025", "0x8fba2c29715c41dd87e781c23373aa1e0549d08a"),
    ("JaJackson", "0xf195721ad850377c96cd634457c70cd9e8308057"),
    ("bobe2", "0xed107a85a4585a381e48c7f7ca4144909e7dd2e5"),
    ("Seasensez", "0x8379b0550ae0f303564f492fb82b11f285a7ed71"),
    ("Blessed-Sunshine", "0x59a0744db1f39ff3afccd175f80e6e8dfc239a09"),
    ("NoLyon", "0xfd2930a094c0a6900a2dc947e5db462d11ae77ae"),
    ("majorexploiter", "0x019782cab5d844f02bafb71f512758be78579f3c"),
]

results = []
for name, addr in t1_wallets:
    try:
        resp = requests.get(
            BASE_URL,
            params={"timePeriod": "DAY", "userName": name},
            proxies=proxies,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list) and len(data) > 0:
            entry = data[0]
            pnl = float(entry.get("pnl", 0))
            volume = float(entry.get("vol", 0))
            rank = entry.get("rank", "N/A")
            results.append((name, pnl, volume, rank))
        else:
            results.append((name, 0, 0, "N/A"))
    except Exception as e:
        results.append((name, 0, 0, f"ERR"))
        print(f"  Error for {name}: {e}")

# Sort by PnL descending
results.sort(key=lambda x: x[1], reverse=True)

print(f"\n{'Username':<22} {'DAY PnL':>12} {'Volume':>14} {'Rank':>8}")
print("-" * 60)
for name, pnl, volume, rank in results:
    print(f"{name:<22} {pnl:>+12.2f} {volume:>14.2f} {rank:>8}")
print()
