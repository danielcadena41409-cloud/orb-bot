---
name: orb-lessons
description: Lessons learned from ORB bot development and live sessions
metadata:
  type: project
---

# ORB Bot — Lessons Learned

## Technical Lessons

### Alpaca Data API
- Use `feed=iex` for both bars and quotes — free tier default
- `bars/latest` returns the most recently *completed* 1-min bar, not a live tick
- Option snapshots are at `/v2/snapshots/options/{symbol}` on data.alpaca.markets
- Paper trading endpoint: `paper-api.alpaca.markets/v2` (separate from data URL)

### OCC Symbol Construction
- Format: `{underlying}{YYMMDD}{C|P}{strike * 1000, zero-padded to 8 digits}`
- Example: SPY expiring 2026-06-20, Call, strike $580 → `SPY260620C00580000`
- Strike is in units of $0.001 (multiply by 1000)

### Options Execution
- Always market order for options (limit orders can miss fills)
- Mid price = (ask + bid) / 2 from snapshot latestQuote
- qty = floor(budget / (mid * 100)); minimum 1 contract

### Phase Timing
- Phase 1 ends exactly at 9:45:00 ET
- Phase 2 skips the 9:45 bar (it may include pre-9:45 ticks)
- Phase 2 ends at 10:30:00 ET regardless
- Time stop is hard 11:00:00 ET

## Strategy Lessons

### What Works
- Filtering out narrow ranges (<0.3%) significantly reduces whipsaw trades
- Body-close confirmation (not wick) reduces false breakouts
- 11:00 AM time stop avoids lunch-hour mean reversion

### What to Watch For
- High-VIX days: options are more expensive, budget buys fewer contracts
- Earnings days: SPY less likely to trend; consider skipping
- FOMC days: extreme volatility can trigger both profit target AND stop quickly

## Future Improvements (Not Yet Implemented)
- Email notification via SendGrid when trade is entered or closed
- Pre-market gap filter (skip if SPY gaps >1% from prior close)
- Volume confirmation on breakout candle
- Track daily P&L in a running CSV
