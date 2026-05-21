---
name: orb-strategy
description: Core ORB strategy rules and parameters for SPY options trading
metadata:
  type: project
---

# ORB Strategy — Core Rules

## Concept
Opening Range Breakout: the first 15 minutes of market trading define a support/resistance range. A confirmed close beyond that range signals momentum in that direction.

## Parameters
| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Range window | 9:30–9:45 ET | First 15 min = opening range |
| Breakout window | 9:45–10:30 ET | 45-min window for entry |
| Time stop | 11:00 AM ET | Avoid lunch chop |
| Min range size | 0.3% | Too tight = no directional bias |
| Profit target | +80% | Options move fast; bank gains early |
| Stop loss | -40% | Risk no more than half the target |
| Max budget | $300 | Position sizing cap per session |
| Strike offset | 10 points OTM | Capture directional move without premium bleed |

## Signal Confirmation
- **Body close** rule: the entire open-to-close body must be beyond the ORB level
- Wicks alone do NOT confirm breakout — they are noise
- Skip the first candle at 9:45 (stale data risk)

## Option Selection
- Nearest weekly expiry (Friday), within 7 days
- If no valid contract, try next expiry
- Validate via snapshot API before placing order

## Why These Numbers
- +80% target: SPY weekly options near the money can move 100%+ on strong breakouts; 80% is achievable while leaving room before the move fades
- -40% stop: keeps loss less than half the expected gain (favorable risk:reward)
- 10-point OTM: delta ~0.25-0.40, enough leverage without being lottery tickets
- $300 budget: small enough to paper-trade safely, large enough to see real P&L movement
