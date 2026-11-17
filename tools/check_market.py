"""Run on the deployment host: python -m tools.check_market [688605]."""

import sys

from app.indicators import kdj
from app.market import MarketClient, PROVIDERS, normalize_symbol


symbol = normalize_symbol(sys.argv[1] if len(sys.argv) > 1 else "688605")
client = MarketClient()
success = 0
for provider in PROVIDERS:
    for period in ("day", "5", "15"):
        try:
            bars, name, source = client.fetch(symbol, period, provider)
            last = kdj(bars)[-1]
            print(f"OK   {source:10} {symbol} {period:3} {name} bars={len(bars)} latest={last['time']} K={last['K']:.2f} D={last['D']:.2f} J={last['J']:.2f}", flush=True)
            success += 1
        except (ValueError, IndexError) as exc:
            print(f"FAIL {provider:10} {symbol} {period:3} {exc}", flush=True)
sys.exit(0 if success == len(PROVIDERS) * 3 else 1)
