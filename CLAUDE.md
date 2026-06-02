# ORB Scanner — Claude Reference

## Strategy: Opening Range Breakout (ORB)

### What It Trades
- **Instruments**: SPY and QQQ weekly options (calls or puts)
- **Session**: US market hours, runs once per day, maximum 1 trade per session

### Regime Filter
Reads `~/trading-agent/data/regime.json` at startup before Phase 1.

| Regime | Min Range | Directional Bias |
|--------|-----------|-----------------|
| BULL_TRENDING | 0.3% | Favor calls — if tie on opposite dirs, take call |
| BEAR_TRENDING | 0.3% | Favor puts — if tie on opposite dirs, take put |
| HIGH_VOLATILITY | — | **Skip entire session** |
| SIDEWAYS | 0.3% | Neutral — SPY wins all ties |
| *(file missing)* | 0.5% | Defaults to SIDEWAYS rules |

### Three-Phase Logic

#### Phase 1 — Range Definition (9:30–9:45 ET)
- Fetches 1-min bars for **both SPY and QQQ** covering 9:30–9:45
- Records `high wick` and `low wick` across all bars as `ORB_HIGH` / `ORB_LOW` per symbol
- Each symbol filtered independently against `min_range_pct` from regime
- If a symbol fails the filter it is excluded from Phase 2 (but still shown in journal)
- Journal always shows both ORB ranges regardless of filter outcome

#### Phase 2 — Breakout Watch (9:45–10:30 ET)
- Polls every 60 seconds — fetches latest 1-min bar for each valid symbol
- Skips first candle at 9:45 for all symbols
- Breakout confirmed only when the candle **body** (open-to-close) fully closes beyond the ORB level
  - Body fully above `ORB_HIGH` → CALL
  - Body fully below `ORB_LOW` → PUT
- **One trade per session** — first breakout detected wins
- Tie-breaking (same tick, multiple breakouts):
  - Same direction → SPY over QQQ
  - Opposite directions → regime decides (BULL→call, BEAR→put, SIDEWAYS→SPY)

#### Phase 3 — Position Monitor (until 11:00 ET)
- Polls option position every 60 seconds via Alpaca v1beta1 snapshot
- **Profit target**: +80% → market sell
- **Stop loss**: -40% → market sell
- **Time stop**: 11:00 AM ET → market sell regardless

### Options Selection
- Underlying: whichever symbol triggered the breakout (SPY or QQQ)
- Strike: round(price ± 10) — 10 points OTM
- Expiry: nearest Friday within 7 days (weekly); tries next expiry if not found
- OCC symbol: `{underlying}{YYMMDD}{C|P}{strike*1000 zero-padded 8 digits}`
- Validated via `GET https://data.alpaca.markets/v1beta1/options/snapshots?symbols={sym}`
- Mid price = (ask + bid) / 2 from `latestQuote`
- Max budget: $300 | Qty = floor(300 / (mid × 100))

### Key Files
| File | Purpose |
|------|---------|
| `scripts/orb_scanner.py` | Main bot (run this) |
| `data/orb_positions.json` | Live position state (includes `underlying` field) |
| `journal/YYYY-MM-DD.md` | Daily journal — both ORBs + trade details |
| `.env` | API credentials (never commit) |
| `~/trading-agent/data/regime.json` | Markov regime input |

### Running
```bash
python3 scripts/orb_scanner.py
```

### API Endpoints Used
- `GET https://data.alpaca.markets/v2/stocks/{sym}/bars` — historical bars (feed=iex)
- `GET https://data.alpaca.markets/v2/stocks/{sym}/bars/latest` — latest 1-min bar
- `GET https://data.alpaca.markets/v2/stocks/{sym}/quotes/latest` — current quote
- `GET https://data.alpaca.markets/v1beta1/options/snapshots?symbols={sym}` — option snapshot
- `POST https://paper-api.alpaca.markets/v2/orders` — place order

### Exit Rules (Priority Order)
1. +80% profit target
2. -40% stop loss
3. 11:00 AM ET time stop

### Hard Rules — Never Change
- One trade per session maximum
- No trading past 11:00 AM ET
- Always market orders for options
- Never exceed $300 budget
- HIGH_VOLATILITY regime = no trade, clean exit
