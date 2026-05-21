# ORB Scanner — Claude Reference

## Strategy: Opening Range Breakout (ORB)

### What It Trades
- **Instrument**: SPY weekly options (calls or puts)
- **Session**: US market hours, runs once per day

### Three-Phase Logic

#### Phase 1 — Range Definition (9:30–9:45 ET)
- Collects all 1-minute bars for SPY from 9:30 to 9:45
- Records the **highest high wick** across all bars as `ORB_HIGH`
- Records the **lowest low wick** across all bars as `ORB_LOW`
- Filters out sessions where the range is less than 0.3% of price (too tight = no edge)

#### Phase 2 — Breakout Watch (9:45–10:30 ET)
- Polls every 60 seconds for the latest 1-min bar
- Skips the first candle at 9:45 (may be incomplete ORB data)
- Breakout is confirmed only when the candle **body** (open-to-close) fully closes beyond the ORB level
  - Body fully above `ORB_HIGH` → CALL trade
  - Body fully below `ORB_LOW` → PUT trade
- Only one trade per session; stops scanning after entry

#### Phase 3 — Position Monitor (until 11:00 ET)
- Polls option position every 60 seconds via Alpaca snapshot API
- **Profit target**: +80% on the option → market sell
- **Stop loss**: -40% on the option → market sell
- **Time stop**: 11:00 AM ET → market sell regardless of P&L

### Options Selection
- Direction CALL: strike = round(spy_price + 10)
- Direction PUT: strike = round(spy_price - 10)
- Expiry: nearest Friday within 7 days (weekly)
- OCC symbol format: `SPY{YYMMDD}{C|P}{strike*1000 zero-padded 8 digits}`
- Validated via Alpaca `/v2/snapshots/options/{symbol}`
- Max budget: $300 per session
- Quantity: floor(300 / (mid_price * 100))

### Key Files
| File | Purpose |
|------|---------|
| `scripts/orb_scanner.py` | Main bot (run this) |
| `data/orb_positions.json` | Live position state |
| `journal/YYYY-MM-DD.md` | Daily trade journal |
| `.env` | API credentials (never commit) |

### Running
```bash
python3 scripts/orb_scanner.py
```

### API Endpoints Used
- `GET https://data.alpaca.markets/v2/stocks/SPY/bars` — historical bars (feed=iex)
- `GET https://data.alpaca.markets/v2/stocks/SPY/bars/latest` — latest bar
- `GET https://data.alpaca.markets/v2/stocks/SPY/quotes/latest` — current quote
- `GET https://data.alpaca.markets/v2/snapshots/options/{symbol}` — option snapshot
- `POST https://paper-api.alpaca.markets/v2/orders` — place order
- `DELETE https://paper-api.alpaca.markets/v2/orders/{id}` — cancel order

### Exit Rules (Priority Order)
1. +80% profit target
2. -40% stop loss
3. 11:00 AM ET time stop

### What NOT to Change
- Do not trade past 11:00 AM ET
- Do not take more than one trade per session
- Do not trade if ORB range < 0.3%
- Always use market orders for options (liquidity)
- Never exceed $300 budget
