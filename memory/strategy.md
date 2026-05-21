---
name: orb-strategy
description: Core ORB strategy rules and parameters for SPY+QQQ options trading with regime filter
metadata:
  type: project
---

# ORB Strategy — Core Rules

## Concept
Opening Range Breakout: the first 15 minutes define a support/resistance range per symbol. A confirmed body-close beyond that range signals momentum. One trade per session — whichever of SPY or QQQ breaks out first.

## Parameters
| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Symbols | SPY, QQQ | Two liquid ETFs increase odds of a clean breakout session |
| Range window | 9:30–9:45 ET | First 15 min = opening range |
| Breakout window | 9:45–10:30 ET | 45-min window for entry |
| Time stop | 11:00 AM ET | Avoid lunch chop |
| Base min range | 0.3% | Too tight = no directional bias |
| SIDEWAYS min range | 0.5% | Stricter filter in choppy regimes |
| Profit target | +80% | Options move fast; bank gains early |
| Stop loss | -40% | Risk no more than half the target |
| Max budget | $300 | Position sizing cap per session |
| Strike offset | 10 points OTM | Capture directional move without premium bleed |
| Trades per session | 1 | Prevents over-trading and conflicting positions |

## Regime Integration
Reads `~/trading-agent/data/regime.json` (Markov model output) at startup.

| Regime | Action |
|--------|--------|
| BULL_TRENDING | min 0.3%, calls favored on ties |
| BEAR_TRENDING | min 0.3%, puts favored on ties |
| HIGH_VOLATILITY | skip entire session — no trade |
| SIDEWAYS | min 0.5%, SPY wins all ties |
| File missing | default to SIDEWAYS |

## Signal Confirmation
- **Body close** rule: entire open-to-close body must close beyond the ORB level
- Wicks alone do NOT confirm breakout
- Skip the first 1-min candle after 9:45 (stale data risk)
- Both symbols checked independently each tick

## Trade Priority (Phase 2 tie-breaking)
1. First symbol to break out (any direction) → wins immediately
2. Same-tick, same direction → SPY over QQQ
3. Same-tick, opposite directions:
   - BULL_TRENDING → take the call side
   - BEAR_TRENDING → take the put side
   - SIDEWAYS → take SPY (direction irrelevant to priority)

## Option Selection
- Underlying: whichever symbol triggered (SPY or QQQ)
- Strike: round(price ± 10) — consistent 10-point OTM offset
- Expiry: nearest weekly Friday
- Validated via Alpaca v1beta1 options snapshot API
- qty = floor(300 / (mid * 100)), minimum 1

## Why These Numbers
- +80% target: weekly options near-the-money can move 100%+ on strong breakouts
- -40% stop: keeps loss-to-target ratio at 1:2
- 10-point OTM: delta ~0.25-0.40 on SPY/QQQ, good leverage without lottery-ticket risk
- 0.5% SIDEWAYS filter: tight ranges in choppy markets produce immediate whipsaw reversals
- HIGH_VOLATILITY skip: wide bid/ask spreads + erratic price action make ORB unreliable
