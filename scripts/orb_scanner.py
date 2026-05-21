#!/usr/bin/env python3
"""
ORB Scanner - Opening Range Breakout Bot for SPY Options
Phases: Range Definition (9:30-9:45) → Breakout Watch (9:45-10:30) → Position Monitor (until 11:00)
"""

import os
import sys
import json
import time
import math
import requests
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# ── Load env ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

API_KEY    = os.environ["APCA_API_KEY_ID"]
API_SECRET = os.environ["APCA_API_SECRET_KEY"]
BASE_URL   = os.environ["APCA_BASE_URL"].rstrip("/")  # trading API
DATA_URL   = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "accept":              "application/json",
    "content-type":        "application/json",
}

ET = ZoneInfo("America/New_York")

POSITIONS_FILE = ROOT / "data" / "orb_positions.json"
JOURNAL_DIR    = ROOT / "journal"

MAX_BUDGET     = 300        # dollars
PROFIT_TARGET  = 0.80       # +80%
STOP_LOSS      = -0.40      # -40%
STRIKE_OFFSET  = 10         # points above/below for call/put
MIN_RANGE_PCT  = 0.003      # 0.3%

# ── ANSI colours ─────────────────────────────────────────────────────────────
R  = "\033[0m"
B  = "\033[1m"
GR = "\033[32m"
RD = "\033[31m"
YL = "\033[33m"
CY = "\033[36m"
MG = "\033[35m"
WH = "\033[97m"
DM = "\033[2m"

def now_et() -> datetime.datetime:
    return datetime.datetime.now(ET)

def ts() -> str:
    return now_et().strftime("%I:%M:%S %p")

def hdr(text: str) -> None:
    width = 60
    print(f"\n{B}{CY}{'═'*width}{R}")
    print(f"{B}{CY}  {text}{R}")
    print(f"{B}{CY}{'═'*width}{R}\n")

def info(msg: str) -> None:
    print(f"{DM}[{ts()}]{R} {msg}")

def success(msg: str) -> None:
    print(f"{B}{GR}✔  {msg}{R}")

def warn(msg: str) -> None:
    print(f"{YL}⚠  {msg}{R}")

def err(msg: str) -> None:
    print(f"{B}{RD}✘  {msg}{R}")

def bold(msg: str) -> None:
    print(f"{B}{WH}{msg}{R}")

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def get(url: str, params: dict = None) -> dict:
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def post(url: str, body: dict) -> dict:
    r = requests.post(url, headers=HEADERS, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

def delete(url: str) -> dict:
    r = requests.delete(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json() if r.text else {}

# ── Alpaca data: 1-min bars ───────────────────────────────────────────────────
def fetch_bars(symbol: str, start: str, end: str) -> list[dict]:
    """Return list of 1-min bars between start and end (RFC3339 strings)."""
    bars = []
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": "1Min",
        "start":     start,
        "end":       end,
        "feed":      "iex",
        "limit":     1000,
    }
    while True:
        data = get(url, params)
        bars.extend(data.get("bars") or [])
        token = data.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return bars

def fetch_latest_bar(symbol: str) -> dict | None:
    """Return the most recent completed 1-min bar."""
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars/latest"
    data = get(url, {"feed": "iex"})
    return data.get("bar")

def fetch_latest_quote(symbol: str) -> float | None:
    """Return current mid price for SPY."""
    url = f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest"
    data = get(url, {"feed": "iex"})
    q = data.get("quote", {})
    ap, bp = q.get("ap", 0), q.get("bp", 0)
    if ap and bp:
        return round((ap + bp) / 2, 2)
    return None

# ── Options helpers ───────────────────────────────────────────────────────────
def nearest_friday(from_date: datetime.date, max_days: int = 7) -> datetime.date | None:
    """Return the nearest Friday (weekly expiry) within max_days."""
    for delta in range(1, max_days + 2):
        d = from_date + datetime.timedelta(days=delta)
        if d.weekday() == 4:  # Friday
            return d
    return None

def occ_symbol(underlying: str, expiry: datetime.date, opt_type: str, strike: float) -> str:
    """Build OCC option symbol: e.g. SPY261219C00580000"""
    exp_str  = expiry.strftime("%y%m%d")
    type_ch  = "C" if opt_type == "call" else "P"
    strike_i = int(round(strike * 1000))
    return f"{underlying}{exp_str}{type_ch}{strike_i:08d}"

def get_option_snapshot(symbol: str) -> dict | None:
    """Fetch option snapshot via v1beta1 snapshots endpoint."""
    url = f"{DATA_URL}/v1beta1/options/snapshots"
    try:
        data = get(url, {"symbols": symbol})
        snaps = data.get("snapshots", {})
        return snaps.get(symbol)
    except Exception:
        return None

def find_valid_option(direction: str, spy_price: float) -> tuple[str, float] | tuple[None, None]:
    """
    Find a valid option contract and return (occ_symbol, mid_price).
    Tries nearest weekly expiry first, then next one.
    """
    today = now_et().date()
    for extra_weeks in range(3):
        expiry = nearest_friday(today, max_days=7 + extra_weeks * 7)
        if expiry is None:
            continue
        strike = spy_price + STRIKE_OFFSET if direction == "call" else spy_price - STRIKE_OFFSET
        strike = round(strike)  # round to whole dollar
        sym = occ_symbol("SPY", expiry, direction, float(strike))
        snap = get_option_snapshot(sym)
        if snap:
            # latestQuote from v1beta1 snapshot
            q  = snap.get("latestQuote", {})
            ap = q.get("ap", 0)
            bp = q.get("bp", 0)
            if ap and bp:
                mid = round((ap + bp) / 2, 2)
                if mid > 0:
                    return sym, mid
            # fall back to latest trade price
            lt = snap.get("latestTrade", {})
            px = lt.get("p", 0)
            if px > 0:
                return sym, px
    return None, None

# ── Position store ────────────────────────────────────────────────────────────
def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text())
    return {}

def save_positions(data: dict) -> None:
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(data, indent=2))

# ── Order execution ───────────────────────────────────────────────────────────
def place_market_order(symbol: str, qty: int, side: str = "buy") -> dict:
    body = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    }
    return post(f"{BASE_URL}/orders", body)

def close_position_by_symbol(symbol: str, qty: int) -> dict | None:
    try:
        return place_market_order(symbol, qty, side="sell")
    except Exception as e:
        warn(f"Close order failed: {e}")
        return None

def get_open_option_position(symbol: str) -> dict | None:
    """Return Alpaca position dict for the given option symbol, or None."""
    try:
        data = get(f"{BASE_URL}/positions/{requests.utils.quote(symbol)}")
        return data
    except Exception:
        return None

# ── Journal ───────────────────────────────────────────────────────────────────
def write_journal(date: datetime.date, session: dict) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{date.isoformat()}.md"
    orb_h  = session.get("orb_high",  "N/A")
    orb_l  = session.get("orb_low",   "N/A")
    orb_r  = session.get("orb_range", "N/A")
    orb_p  = session.get("orb_pct",   "N/A")
    bdir   = session.get("direction",  "N/A")
    e_sym  = session.get("entry_symbol", "N/A")
    e_px   = session.get("entry_price",  "N/A")
    e_qty  = session.get("entry_qty",    "N/A")
    x_px   = session.get("exit_price",   "N/A")
    x_reas = session.get("exit_reason",  "N/A")
    pnl_p  = session.get("pnl_pct",     "N/A")
    pnl_d  = session.get("pnl_dollar",  "N/A")

    md = f"""# ORB Session — {date.isoformat()}

## Opening Range
| Field | Value |
|-------|-------|
| ORB High | ${orb_h} |
| ORB Low  | ${orb_l} |
| Range    | ${orb_r} ({orb_p}%) |

## Trade
| Field | Value |
|-------|-------|
| Direction     | {bdir} |
| Symbol        | {e_sym} |
| Entry Price   | ${e_px} |
| Qty           | {e_qty} |
| Exit Price    | ${x_px} |
| Exit Reason   | {x_reas} |
| P&L (%)       | {pnl_p}% |
| P&L ($)       | ${pnl_d} |

## Notes
_Auto-generated by orb_scanner.py_
"""
    path.write_text(md)
    success(f"Journal written → {path}")

# ── PHASE 1: Range Definition ─────────────────────────────────────────────────
def phase_range_definition() -> tuple[float, float] | tuple[None, None]:
    hdr("PHASE 1 — OPENING RANGE DEFINITION  (9:30 → 9:45 ET)")

    target = now_et().replace(hour=9, minute=45, second=0, microsecond=0)

    # Countdown loop every 30 seconds
    while True:
        n = now_et()
        remaining = (target - n).total_seconds()
        if remaining <= 0:
            break
        mins, secs = divmod(int(remaining), 60)
        print(f"\r  {CY}⏱  Range closes in {B}{mins:02d}:{secs:02d}{R}{CY}  — watching SPY 1-min bars ...{R}  ", end="", flush=True)
        time.sleep(30)

    print()  # newline after countdown

    # Fetch bars 9:30-9:45
    date_str = now_et().date().isoformat()
    start    = f"{date_str}T09:30:00-04:00"
    end      = f"{date_str}T09:45:00-04:00"

    info("Fetching 9:30-9:45 bars for SPY ...")
    bars = fetch_bars("SPY", start, end)

    if not bars:
        err("No bars returned — market may be closed or data unavailable.")
        return None, None

    orb_high = max(b["h"] for b in bars)
    orb_low  = min(b["l"] for b in bars)
    last_close = bars[-1]["c"]
    rng      = orb_high - orb_low
    rng_pct  = rng / last_close * 100

    print()
    print(f"  {B}{'─'*54}{R}")
    print(f"  {B}{GR}  ORB HIGH  ${orb_high:.2f}{R}")
    print(f"  {B}{RD}  ORB LOW   ${orb_low:.2f}{R}")
    print(f"  {B}{WH}  RANGE     ${rng:.2f}  ({rng_pct:.2f}%){R}")
    print(f"  {B}{'─'*54}{R}\n")

    if rng_pct < MIN_RANGE_PCT * 100:
        warn(f"RANGE TOO NARROW ({rng_pct:.2f}%) — SKIPPING SESSION")
        return None, None

    success(f"ORB confirmed: HIGH ${orb_high:.2f} | LOW ${orb_low:.2f} | {rng_pct:.2f}%")
    return orb_high, orb_low

# ── PHASE 2: Breakout Watch ────────────────────────────────────────────────────
def phase_breakout_watch(orb_high: float, orb_low: float) -> tuple[str, float] | tuple[None, None]:
    hdr("PHASE 2 — BREAKOUT WATCH  (9:45 → 10:30 ET)")

    end_time  = now_et().replace(hour=10, minute=30, second=0, microsecond=0)
    first_bar = True

    while now_et() < end_time:
        time.sleep(60)
        n = now_et()

        bar = fetch_latest_bar("SPY")
        if not bar:
            warn("No bar data — retrying next minute")
            continue

        ts_str = n.strftime("%I:%M %p")
        o, c   = bar["o"], bar["c"]
        spy_px = c

        body_high = max(o, c)
        body_low  = min(o, c)

        # Skip first bar right at 9:45 (may be partial ORB bar)
        if first_bar:
            first_bar = False
            info(f"[{ts_str}] SPY ${spy_px:.2f} | Skipping first candle after range")
            continue

        # Determine status
        if body_low > orb_high:
            direction = "call"
            status_txt = f"{B}{GR}BREAKOUT UP ▲{R}"
        elif body_high < orb_low:
            direction = "put"
            status_txt = f"{B}{RD}BREAKOUT DOWN ▼{R}"
        else:
            direction = None
            status_txt = f"{DM}No breakout{R}"

        bar_c_col = GR if c >= o else RD
        print(
            f"  {DM}[{ts_str}]{R}  SPY {bar_c_col}${spy_px:.2f}{R}"
            f"  |  Body [{o:.2f}→{c:.2f}]"
            f"  |  {status_txt}"
            f"  |  ORB {GR}H${orb_high:.2f}{R} {RD}L${orb_low:.2f}{R}"
        )

        if direction:
            print()
            if direction == "call":
                bold(f"  ⚡  BREAKOUT UP DETECTED — Entering CALL")
            else:
                bold(f"  ⚡  BREAKOUT DOWN DETECTED — Entering PUT")
            print()
            return direction, spy_px

    warn("10:30 AM reached — no breakout detected. Session complete.")
    return None, None

# ── PHASE 3: Position Monitor ─────────────────────────────────────────────────
def phase_position_monitor(
    direction: str,
    spy_price: float,
    orb_high: float,
    orb_low: float,
) -> dict:
    hdr("PHASE 3 — POSITION ENTRY & MONITOR  (until 11:00 ET)")

    session = {
        "orb_high":  round(orb_high, 2),
        "orb_low":   round(orb_low, 2),
        "orb_range": round(orb_high - orb_low, 2),
        "orb_pct":   round((orb_high - orb_low) / spy_price * 100, 2),
        "direction": direction,
    }

    # ── Find option ───────────────────────────────────────────────────────────
    info(f"Searching for SPY {direction.upper()} option near ${spy_price:.2f} ...")
    opt_sym, opt_mid = find_valid_option(direction, spy_price)

    if opt_sym is None:
        err("Could not find a valid option contract — aborting trade.")
        session.update({"entry_symbol": "N/A", "exit_reason": "no contract found"})
        return session

    info(f"Found contract: {B}{opt_sym}{R} @ mid ${opt_mid:.2f}")

    # ── Calculate qty ─────────────────────────────────────────────────────────
    cost_per_contract = opt_mid * 100   # 1 contract = 100 shares
    qty = max(1, math.floor(MAX_BUDGET / cost_per_contract))
    total_cost = qty * cost_per_contract

    if total_cost > MAX_BUDGET and qty > 1:
        qty -= 1
        total_cost = qty * cost_per_contract

    info(f"Buying {B}{qty}{R} contract(s) — est. cost ${total_cost:.2f}")

    # ── Place order ───────────────────────────────────────────────────────────
    try:
        order = place_market_order(opt_sym, qty)
        order_id = order.get("id", "?")
        success(f"Order placed: {order_id}")
    except Exception as e:
        err(f"Order failed: {e}")
        session.update({"entry_symbol": opt_sym, "exit_reason": f"order error: {e}"})
        return session

    # Wait a moment for fill
    time.sleep(3)

    entry_price = opt_mid   # assume filled near mid; update from position if available
    session.update({
        "entry_symbol": opt_sym,
        "entry_price":  round(entry_price, 2),
        "entry_qty":    qty,
    })

    # Persist position
    positions = load_positions()
    positions[opt_sym] = {
        "direction":   direction,
        "entry_price": entry_price,
        "qty":         qty,
        "order_id":    order_id,
        "entered_at":  now_et().isoformat(),
    }
    save_positions(positions)

    print()
    print(f"  {B}{'─'*54}{R}")
    print(f"  {B}{CY}  POSITION OPENED{R}")
    print(f"  Symbol  : {B}{opt_sym}{R}")
    print(f"  Direction: {B}{direction.upper()}{R}")
    print(f"  Entry   : {B}${entry_price:.2f}{R}  x {qty} contracts")
    print(f"  Budget  : ${MAX_BUDGET}  |  Est. cost: ${total_cost:.2f}")
    print(f"  {B}{'─'*54}{R}\n")

    # ── Monitor loop ──────────────────────────────────────────────────────────
    time_stop = now_et().replace(hour=11, minute=0, second=0, microsecond=0)
    exit_reason = "time stop"
    exit_price  = entry_price

    while now_et() < time_stop:
        time.sleep(60)
        n      = now_et()
        ts_str = n.strftime("%I:%M %p")

        # Try to get current price from option snapshot
        snap = get_option_snapshot(opt_sym)
        current_price = None
        if snap:
            q  = snap.get("latestQuote", {})
            ap, bp = q.get("ap", 0), q.get("bp", 0)
            if ap and bp:
                current_price = round((ap + bp) / 2, 2)
            if current_price is None or current_price == 0:
                lt = snap.get("latestTrade", {})
                current_price = lt.get("p", entry_price)

        if current_price is None or current_price == 0:
            current_price = entry_price

        pnl_pct  = (current_price - entry_price) / entry_price
        pnl_dol  = (current_price - entry_price) * 100 * qty
        pnl_col  = GR if pnl_pct >= 0 else RD
        pnl_sign = "+" if pnl_pct >= 0 else ""

        # Current SPY
        spy_now = fetch_latest_quote("SPY") or 0

        print(
            f"  {DM}[{ts_str}]{R}  {B}{opt_sym}{R}"
            f"  |  Entry ${entry_price:.2f}"
            f"  |  Now {pnl_col}${current_price:.2f}{R}"
            f"  |  P&L {pnl_col}{pnl_sign}{pnl_pct*100:.1f}%{R}"
            f"  (${pnl_dol:+.0f})"
            f"  |  SPY ${spy_now:.2f}"
        )

        if pnl_pct >= PROFIT_TARGET:
            print()
            success(f"PROFIT TARGET HIT ({pnl_pct*100:.1f}%) — CLOSING")
            exit_reason = f"profit target +{pnl_pct*100:.1f}%"
            exit_price  = current_price
            break

        if pnl_pct <= STOP_LOSS:
            print()
            err(f"STOP LOSS HIT ({pnl_pct*100:.1f}%) — CLOSING")
            exit_reason = f"stop loss {pnl_pct*100:.1f}%"
            exit_price  = current_price
            break

    else:
        print()
        warn("TIME STOP — CLOSING ALL POSITIONS")

    # ── Close position ────────────────────────────────────────────────────────
    close_position_by_symbol(opt_sym, qty)
    success(f"Close order submitted for {opt_sym}")

    pnl_pct_final  = (exit_price - entry_price) / entry_price * 100
    pnl_dol_final  = (exit_price - entry_price) * 100 * qty
    pnl_col_final  = GR if pnl_pct_final >= 0 else RD

    print()
    print(f"  {B}{'─'*54}{R}")
    print(f"  {B}{WH}  SESSION COMPLETE{R}")
    print(f"  Symbol   : {opt_sym}")
    print(f"  Entry    : ${entry_price:.2f}")
    print(f"  Exit     : ${exit_price:.2f}")
    print(f"  Reason   : {exit_reason}")
    print(f"  P&L      : {pnl_col_final}{B}{pnl_pct_final:+.1f}%  (${pnl_dol_final:+.0f}){R}")
    print(f"  {B}{'─'*54}{R}\n")

    session.update({
        "exit_price":  round(exit_price, 2),
        "exit_reason": exit_reason,
        "pnl_pct":     round(pnl_pct_final, 2),
        "pnl_dollar":  round(pnl_dol_final, 2),
    })

    # Clean saved position
    positions = load_positions()
    positions.pop(opt_sym, None)
    save_positions(positions)

    return session

# ── Wait until time (ET) ──────────────────────────────────────────────────────
def wait_until(hour: int, minute: int, label: str) -> None:
    target = now_et().replace(hour=hour, minute=minute, second=0, microsecond=0)
    while True:
        remaining = (target - now_et()).total_seconds()
        if remaining <= 0:
            break
        mins, secs = divmod(int(remaining), 60)
        print(
            f"\r  {CY}⏳  Waiting for {label}  —  {B}{mins:02d}:{secs:02d}{R}{CY} remaining ...{R}  ",
            end="", flush=True
        )
        time.sleep(15)
    print()

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    print()
    print(f"{B}{MG}{'█'*60}{R}")
    print(f"{B}{MG}  ORB SCANNER — SPY Options  |  Opening Range Breakout{R}")
    print(f"{B}{MG}{'█'*60}{R}")
    print(f"  Strategy : 15-min ORB  |  Budget: ${MAX_BUDGET}  |  Target: +{PROFIT_TARGET*100:.0f}%  |  Stop: {STOP_LOSS*100:.0f}%")
    print(f"  Started  : {now_et().strftime('%Y-%m-%d %I:%M:%S %p ET')}")
    print()

    today = now_et().date()
    n     = now_et()

    # ── Pre-market wait ───────────────────────────────────────────────────────
    market_open = n.replace(hour=9, minute=30, second=0, microsecond=0)
    if n < market_open:
        wait_until(9, 30, "9:30 AM ET Market Open")

    # ── Phase 1: Range definition ─────────────────────────────────────────────
    n = now_et()
    range_end = n.replace(hour=9, minute=45, second=0, microsecond=0)
    if n < range_end:
        orb_high, orb_low = phase_range_definition()
    else:
        # Already past 9:45 — fetch completed bars
        warn("Already past 9:45 — fetching completed ORB bars now")
        date_str = today.isoformat()
        start    = f"{date_str}T09:30:00-04:00"
        end      = f"{date_str}T09:45:00-04:00"
        bars     = fetch_bars("SPY", start, end)
        if not bars:
            err("No ORB bars available. Exiting.")
            sys.exit(1)
        orb_high = max(b["h"] for b in bars)
        orb_low  = min(b["l"] for b in bars)
        last_c   = bars[-1]["c"]
        rng_pct  = (orb_high - orb_low) / last_c * 100
        info(f"ORB HIGH ${orb_high:.2f} | ORB LOW ${orb_low:.2f} | {rng_pct:.2f}%")
        if rng_pct < MIN_RANGE_PCT * 100:
            warn("RANGE TOO NARROW — SKIPPING SESSION")
            sys.exit(0)

    if orb_high is None:
        sys.exit(0)

    # ── Phase 2: Breakout watch ───────────────────────────────────────────────
    n = now_et()
    breakout_end = n.replace(hour=10, minute=30, second=0, microsecond=0)
    if n > breakout_end:
        warn("Already past 10:30 — breakout window closed. Exiting.")
        sys.exit(0)

    direction, spy_price = phase_breakout_watch(orb_high, orb_low)

    if direction is None:
        info("No breakout today. Session ended.")
        sys.exit(0)

    # ── Phase 3: Position monitor ─────────────────────────────────────────────
    session = phase_position_monitor(direction, spy_price, orb_high, orb_low)
    write_journal(today, session)

    print(f"\n{B}{MG}  ORB SESSION COMPLETE — See journal/{today.isoformat()}.md{R}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YL}  Interrupted by user. Exiting cleanly.{R}\n")
        sys.exit(0)
