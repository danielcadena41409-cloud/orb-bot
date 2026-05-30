#!/usr/bin/env python3
"""
live_trader.py — ORB Scanner entry point

  Live mode:  python3 scripts/live_trader.py
  Dry-run:    python3 scripts/live_trader.py --test

--test exercises the full pipeline (real API calls, real option lookup,
simulated ticks) without ever placing a real order.
"""

import sys
import argparse
import math
import time
import datetime
from pathlib import Path

# Allow `import orb_scanner` from the same scripts/ directory
sys.path.insert(0, str(Path(__file__).parent))
import orb_scanner as orb


# ── Dry-run / test ─────────────────────────────────────────────────────────────
def run_test() -> None:
    """
    Full pipeline dry-run:
      1. Fetch real ORB bars for the most recent trading day with data
      2. Simulate a directional breakout from the real ORB levels
      3. Look up a real option contract (validates options API)
      4. Run 3 simulated price ticks: flat → +30% → +85% (hits +80% profit target)
      5. Write a journal entry marked [TEST]
      No orders are placed.
    """
    print()
    print(f"{orb.B}{orb.YL}{'▲' * 62}{orb.R}")
    print(f"{orb.B}{orb.YL}  ORB SCANNER — DRY RUN  |  --test  |  NO ORDERS PLACED{orb.R}")
    print(f"{orb.B}{orb.YL}{'▲' * 62}{orb.R}\n")

    # ── Regime ────────────────────────────────────────────────────────────────
    regime, min_range_pct = orb.load_regime()
    orb.print_regime(regime, min_range_pct)

    if regime == "HIGH_VOLATILITY":
        orb.warn("[TEST] HIGH_VOLATILITY regime — live session would be skipped here")
        return

    # ── Phase 1: ORB data — walk back up to 7 calendar days for data ──────────
    orb.hdr("PHASE 1 — ORB DATA  (most recent trading day)")
    today = orb.now_et().date()
    orbs: dict | None = None
    used_date: datetime.date | None = None

    for days_back in range(0, 7):
        candidate = today - datetime.timedelta(days=days_back)
        if candidate.weekday() >= 5:        # skip weekends
            continue
        start_dt = datetime.datetime(
            candidate.year, candidate.month, candidate.day, 9, 30, 0, tzinfo=orb.ET)
        end_dt = datetime.datetime(
            candidate.year, candidate.month, candidate.day, 9, 45, 0, tzinfo=orb.ET)
        result: dict = {}
        any_data = False
        for sym in orb.WATCHLIST:
            try:
                bars = orb.fetch_bars(sym, start_dt.isoformat(), end_dt.isoformat())
            except Exception as e:
                orb.warn(f"  API error fetching {sym} bars: {e}")
                bars = []
            if not bars:
                result[sym] = {"valid": False, "status": "no data"}
                continue
            any_data = True
            high   = max(b["h"] for b in bars)
            low    = min(b["l"] for b in bars)
            rng    = high - low
            rpct   = rng / bars[-1]["c"] * 100
            valid  = rpct >= min_range_pct * 100
            status = "✔ Valid" if valid else f"✘ Too narrow (<{min_range_pct * 100:.1f}%)"
            orb.info(f"  {sym}  H${high:.2f}  L${low:.2f}  Range {rpct:.2f}%  {status}")
            result[sym] = {
                "high": high, "low": low,
                "range": round(rng, 2), "pct": round(rpct, 2),
                "valid": valid, "status": status,
            }
        if any_data:
            orbs      = result
            used_date = candidate
            break

    if orbs is None or used_date is None:
        orb.err("[TEST] No bar data found in the last 7 days — check API credentials / connectivity")
        return

    if used_date != today:
        orb.warn(f"[TEST] Market not yet open today — using {used_date.isoformat()} ORB data")

    # Determine tradeable symbols; in test mode bypass the range filter if needed
    # so the full pipeline is always exercised as long as data exists.
    valid_syms = [s for s in orb.WATCHLIST if orbs.get(s, {}).get("valid")]
    if not valid_syms:
        orb.warn("[TEST] No symbol passed the range filter (ranges too narrow today)")
        orb.warn("[TEST] Bypassing filter to exercise Phase 3 pipeline ...")
        # Use first symbol that has any price data
        valid_syms = [s for s in orb.WATCHLIST if orbs.get(s, {}).get("high")]
        if not valid_syms:
            orb.err("[TEST] No price data at all — cannot continue")
            return
        for sym in valid_syms:
            orbs[sym]["valid"] = True

    # ── Phase 2: simulate breakout ─────────────────────────────────────────────
    orb.hdr("PHASE 2 — BREAKOUT  (simulated)")
    sym     = valid_syms[0]
    sim_dir = "put" if regime == "BEAR_TRENDING" else "call"
    if sim_dir == "call":
        sim_px = round(orbs[sym]["high"] + 0.05, 2)
    else:
        sim_px = round(orbs[sym]["low"] - 0.05, 2)

    for s in valid_syms:
        o = orbs[s]
        print(f"  {orb.B}{s}{orb.R}  ORB {orb.GR}H${o['high']:.2f}{orb.R} | {orb.RD}L${o['low']:.2f}{orb.R}")
    print()

    col = orb.GR if sim_dir == "call" else orb.RD
    orb.bold(
        f"  ⚡  [TEST] Simulating {sim_dir.upper()} breakout: "
        f"{orb.B}{sym}{orb.R} @ {col}${sim_px:.2f}{orb.R}"
    )
    print()

    # ── Phase 3: option lookup (real) + simulated ticks ──────────────────────
    orb.hdr("PHASE 3 — POSITION  (real option lookup, simulated ticks)")

    orb.info(f"[TEST] Looking up {sym} {sim_dir.upper()} option near ${sim_px:.2f} ...")
    try:
        opt_sym, opt_mid = orb.find_valid_option(sym, sim_dir, sim_px)
    except Exception as e:
        orb.err(f"[TEST] Option lookup failed: {e}")
        return

    if opt_sym is None:
        orb.err("[TEST] No valid option contract found — options API may be unavailable")
        return

    orb.info(f"[TEST] Found: {orb.B}{opt_sym}{orb.R} @ mid ${opt_mid:.2f}")

    qty = math.floor(orb.MAX_BUDGET / (opt_mid * 100))
    if qty == 0:
        orb.warn(
            f"[TEST] Option at ${opt_mid:.2f}/contract exceeds "
            f"${orb.MAX_BUDGET} budget — live session would abort here"
        )
        return

    total_cost = qty * opt_mid * 100
    orb.success(
        f"[TEST] Simulated BUY: {qty}×{opt_sym} @ ${opt_mid:.2f}  "
        f"(est. ${total_cost:.2f})  —  NO REAL ORDER PLACED"
    )
    print()
    print(f"  {orb.B}{'─' * 56}{orb.R}")
    print(f"  {orb.B}{orb.CY}  SIMULATED POSITION OPENED{orb.R}")
    print(f"  Underlying : {orb.B}{sym}{orb.R}")
    print(f"  Option     : {orb.B}{opt_sym}{orb.R}")
    print(f"  Direction  : {orb.B}{sim_dir.upper()}{orb.R}")
    print(f"  Entry      : {orb.B}${opt_mid:.2f}{orb.R}  x {qty} contracts")
    print(f"  Budget     : ${orb.MAX_BUDGET}  |  Est. cost: ${total_cost:.2f}")
    print(f"  {orb.B}{'─' * 56}{orb.R}\n")

    # Simulate 3 ticks: flat → +30% → +85%
    # +85% exceeds the +80% profit target, so tick 3 should trigger CLOSE
    entry       = opt_mid
    tick_mults  = [1.00, 1.30, 1.85]
    exit_price  = entry
    exit_reason = "time stop [TEST]"

    for i, mult in enumerate(tick_mults):
        time.sleep(0.4)
        cur = round(entry * mult, 2)
        pnl = (cur - entry) / entry
        col = orb.GR if pnl >= 0 else orb.RD
        sgn = "+" if pnl >= 0 else ""
        print(
            f"  {orb.DM}[tick {i + 1}/3]{orb.R}  {orb.B}{opt_sym}{orb.R}"
            f"  |  Entry ${entry:.2f}"
            f"  |  Now {col}${cur:.2f}{orb.R}"
            f"  |  P&L {col}{sgn}{pnl * 100:.1f}%{orb.R}"
            f"  {orb.DM}[simulated]{orb.R}"
        )
        if pnl >= orb.PROFIT_TARGET:
            print()
            orb.success(f"[TEST] PROFIT TARGET HIT ({pnl * 100:.1f}%) — would CLOSE position")
            exit_reason = f"profit target +{pnl * 100:.1f}% [TEST]"
            exit_price  = cur
            break
        if pnl <= orb.STOP_LOSS:
            print()
            orb.err(f"[TEST] STOP LOSS HIT ({pnl * 100:.1f}%) — would CLOSE position")
            exit_reason = f"stop loss {pnl * 100:.1f}% [TEST]"
            exit_price  = cur
            break
    else:
        orb.warn("[TEST] All simulated ticks done — time stop would fire")
        exit_price = round(entry * tick_mults[-1], 2)

    # ── Summary ───────────────────────────────────────────────────────────────
    pnl_pct_f = (exit_price - entry) / entry * 100
    pnl_dol_f = (exit_price - entry) * 100 * qty
    pnl_col   = orb.GR if pnl_pct_f >= 0 else orb.RD

    print()
    print(f"  {orb.B}{'─' * 56}{orb.R}")
    print(f"  {orb.B}{orb.WH}  DRY-RUN COMPLETE{orb.R}")
    print(f"  Symbol  : {opt_sym}")
    print(f"  Entry   : ${entry:.2f}  →  Exit: ${exit_price:.2f}  (simulated)")
    print(f"  Reason  : {exit_reason}")
    print(f"  P&L     : {pnl_col}{orb.B}{pnl_pct_f:+.1f}%  (${pnl_dol_f:+.0f}){orb.R}")
    print(f"  {orb.B}{'─' * 56}{orb.R}\n")
    print(f"  {orb.YL}{orb.B}⚠  DRY RUN — no real orders were placed.{orb.R}\n")

    # Write test journal entry so the output path is also validated
    session = {
        "regime":        regime,
        "traded_symbol": sym,
        "direction":     sim_dir,
        "orbs": {
            s: {k: v for k, v in orbs.get(s, {}).items() if k != "valid"}
            for s in orb.WATCHLIST
        },
        "entry_symbol":  opt_sym,
        "entry_price":   round(entry, 2),
        "entry_qty":     qty,
        "exit_price":    round(exit_price, 2),
        "exit_reason":   exit_reason,
        "pnl_pct":       round(pnl_pct_f, 2),
        "pnl_dollar":    round(pnl_dol_f, 2),
    }
    orb.write_journal(used_date, session)


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="live_trader.py",
        description="ORB Scanner — Opening Range Breakout for SPY + QQQ options",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Dry-run: real data + option lookup, simulated ticks, no orders placed",
    )
    args = parser.parse_args()

    if args.test:
        run_test()
    else:
        orb.main()


if __name__ == "__main__":
    main()
