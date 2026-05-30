# ORB Scanner — Bug Audit Report

**Audited file**: `scripts/orb_scanner.py`  
**Audit date**: 2026-05-30  
**Bugs found**: 12  
**Bugs fixed**: 12  
**Syntax check after fixes**: PASS  

---

## Bug Index

| # | Severity | Category | Short description |
|---|----------|----------|-------------------|
| 1 | CRITICAL | Timing | Hardcoded `-04:00` UTC offset breaks winter (EST) bar fetches |
| 2 | CRITICAL | P&L | `exit_price` never updated on time stop — 0% P&L shown |
| 3 | CRITICAL | Options | `find_valid_option` always retries same Friday expiry |
| 4 | CRITICAL | Orders | `max(1, ...)` buys 1 contract even when cost > $300 budget |
| 5 | HIGH | Crash | Unhandled `HTTPError` in Phase 2 first-tick bar fetch |
| 6 | HIGH | Crash | Unhandled `HTTPError` in Phase 2 breakout-detection loop |
| 7 | HIGH | Crash | Unhandled exception in `fetch_latest_quote` during Phase 3 |
| 8 | HIGH | Timing | Time stop fires up to 60 seconds late |
| 9 | HIGH | Crash | No emergency close on unhandled fatal exception |
| 10 | MEDIUM | Positions | No startup check for leftover positions from a crashed session |
| 11 | MEDIUM | Stale data | No bar-age check — same stale bar can trigger duplicate breakout |
| 12 | LOW | Data | `bar["c"]` direct access (KeyError risk) in first-tick skip block |

---

## Bug Details and Fixes

### Bug 1 — CRITICAL: Hardcoded `-04:00` UTC offset (DST)

**Category**: Timing / Stale data  
**Location**: `phase_range_definition()` lines 321–323; `fetch_orb_bars()` lines 663–665  
**Impact**: In winter (EST, UTC-5) the bot requests bars from 8:30–8:45 AM instead of 9:30–9:45 AM. No ORB data is available at that time, so every symbol shows "no data" and the session is skipped. Bot is effectively broken from November through March.

**Before**:
```python
date_str = now_et().date().isoformat()
start    = f"{date_str}T09:30:00-04:00"
end      = f"{date_str}T09:45:00-04:00"
```

**Fix**: Added `orb_window()` helper that builds ISO strings from timezone-aware datetimes, letting `ZoneInfo` emit the correct offset (`-05:00` in winter, `-04:00` in summer):
```python
def orb_window() -> tuple[str, str]:
    today    = now_et().date()
    start_dt = datetime.datetime(today.year, today.month, today.day, 9, 30, 0, tzinfo=ET)
    end_dt   = datetime.datetime(today.year, today.month, today.day, 9, 45, 0, tzinfo=ET)
    return start_dt.isoformat(), end_dt.isoformat()
```
Both callers updated to `start, end = orb_window()`.

---

### Bug 2 — CRITICAL: `exit_price` not updated on time stop

**Category**: P&L calculation / Profit target / Stop loss  
**Location**: `phase_position_monitor()`, monitor loop  
**Impact**: When the 11:00 AM time stop fires naturally (loop condition becomes False), `exit_price` is still set to `entry_price` (its initial value). The journal records 0% P&L and $0 P&L regardless of actual position. Also masked any gain or loss from the time-stop summary on screen.

**Before**:
```python
exit_price = entry_price   # set before loop, never updated on natural loop exit

while now_et() < time_stop:
    ...
else:
    warn("TIME STOP — CLOSING ALL POSITIONS")
# close_position runs here; exit_price is still entry_price
```

**Fix**: Introduced `last_price` to track the most recent valid mid price. The loop was restructured from `while condition` to `while True` with an explicit time-stop `break` that sets `exit_price = last_price`:
```python
last_price = entry_price

while True:
    time.sleep(60)
    n = now_et()
    if n >= time_stop:
        exit_price  = last_price   # <-- correct exit price for P&L
        exit_reason = "time stop"
        warn("TIME STOP — CLOSING ALL POSITIONS")
        break
    ...
    if mid > 0:
        current_price = mid
        last_price    = current_price   # keep last_price current each tick
```

---

### Bug 3 — CRITICAL: `find_valid_option` always retries the same expiry

**Category**: Options selection  
**Location**: `find_valid_option()`, lines 204–207  
**Impact**: The retry loop intended to try the nearest Friday, then the second Friday, then the third. Instead `nearest_friday(today, max_days=7 + extra * 7)` always returns the *same* nearest Friday regardless of `max_days` (it returns the first Friday found, which is always within 7 days). All three iterations hit the same contract. If that contract has no valid quote, the bot aborts the trade instead of trying the next weekly expiry.

**Before**:
```python
for extra in range(3):
    expiry = nearest_friday(today, max_days=7 + extra * 7)
```

**Fix**: Pass `today + timedelta(weeks=extra)` as the base date so each iteration walks forward by one week:
```python
for extra in range(3):
    expiry = nearest_friday(today + datetime.timedelta(weeks=extra))
```
Verified: `extra=0` → next Friday; `extra=1` → Friday of the following week; `extra=2` → Friday two weeks out.

---

### Bug 4 — CRITICAL: Budget overflow — `max(1, …)` forces over-budget purchase

**Category**: Order placement / Math  
**Location**: `phase_position_monitor()`, qty calculation  
**Impact**: When an option costs > $3.00/contract (lot cost > $300), `math.floor(300/cost_per)` returns 0. `max(1, 0)` overrides this to 1, placing an order that exceeds the $300 hard limit. For a $5 option this spends $500 — 67% over budget.

**Before**:
```python
qty = max(1, math.floor(MAX_BUDGET / cost_per))
if qty * cost_per > MAX_BUDGET and qty > 1:
    qty -= 1
```

**Fix**: Remove `max(1, …)`. When `qty == 0` the trade is aborted with an explanatory message:
```python
qty = math.floor(MAX_BUDGET / cost_per)
if qty == 0:
    err(f"Option at ${opt_mid:.2f}/contract (${cost_per:.0f}/lot) exceeds ${MAX_BUDGET} budget — aborting.")
    session.update({"entry_symbol": opt_sym, "exit_reason": "option too expensive for budget"})
    return session
```
The secondary over-budget guard was also removed — it was dead code (floor division can never exceed the numerator).

---

### Bug 5 — HIGH: Unhandled `HTTPError` in Phase 2 first-tick block

**Category**: Crash / Reconnect  
**Location**: `phase_breakout_watch()`, first-tick skip block  
**Impact**: `fetch_latest_bar()` calls `requests.get(...).raise_for_status()`. A non-200 API response raises `HTTPError`, which was not caught, crashing the bot during the 9:46 tick.

**Fix**: Wrapped in try/except; `bar` set to None on error:
```python
try:
    bar = fetch_latest_bar(sym)
except Exception as e:
    warn(f"  API error fetching {sym}: {e}")
    bar = None
```

---

### Bug 6 — HIGH: Unhandled `HTTPError` in Phase 2 breakout-detection loop

**Category**: Crash / Reconnect  
**Location**: `phase_breakout_watch()`, main per-symbol loop  
**Impact**: Same as Bug 5 but during every non-first tick. A single transient API error between 9:47 and 10:30 would crash the bot mid-session with an open (unmonitored) position if a trade had already been placed.

**Fix**: Added try/except with `continue` on error:
```python
try:
    bar = fetch_latest_bar(sym)
except Exception as e:
    warn(f"  API error fetching {sym}: {e} — skipping tick")
    continue
```

---

### Bug 7 — HIGH: Unhandled exception in `fetch_latest_quote` (Phase 3 monitor)

**Category**: Crash / Reconnect  
**Location**: `phase_position_monitor()`, monitor loop, `und_now` line  
**Impact**: `fetch_latest_quote()` calls `get()` which raises on API errors. This exception was not caught in the Phase 3 loop, crashing the bot while a position is open. The underlying price display (`und_now`) is cosmetic — an error here should not terminate monitoring.

**Before**:
```python
und_now = fetch_latest_quote(underlying) or 0
```

**Fix**:
```python
try:
    und_now = fetch_latest_quote(underlying) or 0
except Exception:
    und_now = 0
```

---

### Bug 8 — HIGH: Time stop fires up to 60 seconds late

**Category**: Timing  
**Location**: `phase_position_monitor()`, monitor loop  
**Impact**: The original `while now_et() < time_stop:` check happens *before* `time.sleep(60)`. If the loop enters at 10:59:55, the condition is True, the bot sleeps 60 seconds, wakes at 11:00:55, processes one more tick, then loops and finds the condition False. The time stop fires ~60 seconds after 11:00 AM. Options can trade past the hard close rule.

**Fix**: The loop restructure from Bug 2 also fixes this. Time is checked immediately *after* sleep, not before:
```python
while True:
    time.sleep(60)
    n = now_et()
    if n >= time_stop:   # check right after waking, not before sleeping
        ...
        break
```

---

### Bug 9 — HIGH: No emergency close on fatal exception

**Category**: Crash / Reconnect  
**Location**: `__main__` block  
**Impact**: Any unhandled exception (KeyError, ValueError, network failure outside a try block, etc.) would crash the bot while leaving an open options position unmonitored until expiry or manual intervention.

**Fix**: Added an `except Exception` handler that attempts to close all positions in `orb_positions.json` before exiting:
```python
except Exception as _exc:
    import traceback
    print(f"\n{B}{RD}  FATAL ERROR: {_exc}{R}")
    traceback.print_exc()
    try:
        _positions = load_positions()
        if _positions:
            warn("Emergency closing open positions after fatal error...")
            for _sym, _pos in _positions.items():
                close_position(_sym, _pos.get("qty", 1))
            save_positions({})
    except Exception as _ce:
        err(f"Emergency close failed: {_ce}")
    sys.exit(1)
```

---

### Bug 10 — MEDIUM: No startup check for leftover positions from a crashed session

**Category**: Position tracking  
**Location**: `main()`  
**Impact**: If the bot crashed during Phase 3 on a previous day (leaving a record in `orb_positions.json`), the next session would proceed without closing the stale position. If the bot crashed during today's Phase 3 and was restarted, it would re-enter Phase 1 and potentially place a second order, violating the one-trade-per-session rule.

**Fix**: Added `check_stale_positions()` called at startup:
- Positions from a *previous day*: auto-close via market sell order and remove from file.
- Positions from *today*: warn the user and `sys.exit(0)` — the user must manually verify and clear before restarting.

---

### Bug 11 — MEDIUM: No stale-bar detection — duplicate breakout risk

**Category**: Stale data  
**Location**: `phase_breakout_watch()`, breakout-detection loop  
**Impact**: `fetch_latest_bar()` returns the most recently completed bar. If the Alpaca IEX feed is slow or the network is congested, the same bar (from 2+ minutes ago) can be returned on two consecutive polls. The bot would detect the same breakout twice — but since it returns immediately on the first detection, this only matters if the first poll returns a stale bar from *before* Phase 2 started (e.g., the 9:44 ORB candle returned at 9:47 due to delay), producing a false signal.

**Fix**: After fetching a bar, check its timestamp. Skip bars older than 3 minutes:
```python
bar_ts = bar.get("t", "")
if bar_ts:
    try:
        bar_dt = datetime.datetime.fromisoformat(bar_ts.replace("Z", "+00:00"))
        age    = (datetime.datetime.now(datetime.timezone.utc) - bar_dt).total_seconds()
        if age > 180:
            warn(f"  {sym} bar is {int(age)}s old (stale) — skipping")
            continue
    except Exception:
        pass
```

---

### Bug 12 — LOW: `bar["c"]` direct dict access (KeyError risk)

**Category**: Crash  
**Location**: `phase_breakout_watch()`, first-tick skip block  
**Impact**: `bar["c"]` raises `KeyError` if the bar dict ever lacks a "c" key (malformed API response). Low probability but would crash the bot at 9:46.

**Before**:
```python
px = bar["c"] if bar else 0
```

**Fix**:
```python
px = bar.get("c", 0) if bar else 0
```

---

## Summary

All 12 bugs have been fixed and the script passes Python syntax compilation (`python3 -m py_compile`). The three most dangerous bugs were:

1. **The DST bug** — made the bot completely non-functional from November through March (wrong ORB bars fetched).
2. **The time-stop P&L bug** — every time-stopped session recorded 0% P&L in the journal regardless of actual outcome.
3. **The option expiry retry bug** — the bot silently retried the same contract three times instead of trying three different weekly expirations.
