#!/usr/bin/env python3
"""
ORB Scanner - Opening Range Breakout Bot for SPY + QQQ Options
Phases: Range Definition (9:30-9:45) → Breakout Watch (9:45-10:30) → Position Monitor (until 11:00)
Regime filter from ~/trading-agent/data/regime.json controls min range and directional bias.
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

# ── Load env ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

API_KEY    = os.environ["APCA_API_KEY_ID"]
API_SECRET = os.environ["APCA_API_SECRET_KEY"]
BASE_URL   = os.environ["APCA_BASE_URL"].rstrip("/")
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
REGIME_FILE    = Path.home() / "trading-agent" / "data" / "regime.json"

WATCHLIST      = ["SPY", "QQQ"]
MAX_BUDGET     = 300
PROFIT_TARGET  = 0.80
STOP_LOSS      = -0.40
STRIKE_OFFSET  = 10

# ── ANSI colours ──────────────────────────────────────────────────────────────
R  = "\033[0m"
B  = "\033[1m"
GR = "\033[32m"
RD = "\033[31m"
YL = "\033[33m"
CY = "\033[36m"
MG = "\033[35m"
WH = "\033[97m"
DM = "\033[2m"
BL = "\033[34m"

def now_et() -> datetime.datetime:
    return datetime.datetime.now(ET)

def ts() -> str:
    return now_et().strftime("%I:%M:%S %p")

def hdr(text: str) -> None:
    print(f"\n{B}{CY}{'═'*62}{R}")
    print(f"{B}{CY}  {text}{R}")
    print(f"{B}{CY}{'═'*62}{R}\n")

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

# ── Regime ────────────────────────────────────────────────────────────────────
REGIME_COLORS = {
    "BULL_TRENDING":  GR,
    "BEAR_TRENDING":  RD,
    "HIGH_VOLATILITY": YL,
    "SIDEWAYS":       CY,
}

def load_regime() -> tuple[str, float]:
    """
    Return (regime_name, min_range_pct).
    Defaults to SIDEWAYS if file missing or unreadable.
    """
    try:
        data    = json.loads(REGIME_FILE.read_text())
        regime  = data.get("current_regime", "SIDEWAYS")
    except Exception:
        regime  = "SIDEWAYS"

    min_range = {
        "BULL_TRENDING":   0.003,
        "BEAR_TRENDING":   0.003,
        "HIGH_VOLATILITY": 0.003,   # irrelevant — session is skipped
        "SIDEWAYS":        0.005,
    }.get(regime, 0.005)

    return regime, min_range

def print_regime(regime: str, min_range: float) -> None:
    col  = REGIME_COLORS.get(regime, WH)
    src  = "regime.json" if REGIME_FILE.exists() else "DEFAULT (file not found)"
    print(f"  {B}Regime  :{R} {col}{B}{regime}{R}  {DM}[{src}]{R}")
    print(f"  {B}Min range:{R} {min_range*100:.1f}%")
    if regime == "BULL_TRENDING":
        print(f"  {B}Bias    :{R} {GR}CALLS favored{R}  (tie → take call side)")
    elif regime == "BEAR_TRENDING":
        print(f"  {B}Bias    :{R} {RD}PUTS favored{R}   (tie → take put side)")
    elif regime == "HIGH_VOLATILITY":
        print(f"  {B}Bias    :{R} {YL}SKIP SESSION{R}   (high volatility — no trade)")
    else:
        print(f"  {B}Bias    :{R} {CY}NEUTRAL{R}         (stricter range filter)")
    print()

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def get(url: str, params: dict = None) -> dict:
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def post(url: str, body: dict) -> dict:
    r = requests.post(url, headers=HEADERS, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

# ── Alpaca data ───────────────────────────────────────────────────────────────
def fetch_bars(symbol: str, start: str, end: str) -> list[dict]:
    bars   = []
    url    = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    params = {"timeframe": "1Min", "start": start, "end": end, "feed": "iex", "limit": 1000}
    while True:
        data  = get(url, params)
        bars.extend(data.get("bars") or [])
        token = data.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return bars

def fetch_latest_bar(symbol: str) -> dict | None:
    data = get(f"{DATA_URL}/v2/stocks/{symbol}/bars/latest", {"feed": "iex"})
    return data.get("bar")

def fetch_latest_quote(symbol: str) -> float | None:
    data = get(f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest", {"feed": "iex"})
    q    = data.get("quote", {})
    ap, bp = q.get("ap", 0), q.get("bp", 0)
    if ap and bp:
        return round((ap + bp) / 2, 2)
    return None

# ── Options helpers ───────────────────────────────────────────────────────────
def nearest_friday(from_date: datetime.date, max_days: int = 7) -> datetime.date | None:
    for delta in range(1, max_days + 2):
        d = from_date + datetime.timedelta(days=delta)
        if d.weekday() == 4:
            return d
    return None

def occ_symbol(underlying: str, expiry: datetime.date, opt_type: str, strike: float) -> str:
    exp_str  = expiry.strftime("%y%m%d")
    type_ch  = "C" if opt_type == "call" else "P"
    strike_i = int(round(strike * 1000))
    return f"{underlying}{exp_str}{type_ch}{strike_i:08d}"

def get_option_snapshot(symbol: str) -> dict | None:
    try:
        data  = get(f"{DATA_URL}/v1beta1/options/snapshots", {"symbols": symbol})
        return data.get("snapshots", {}).get(symbol)
    except Exception:
        return None

def option_mid(snap: dict) -> float:
    q  = snap.get("latestQuote", {})
    ap = q.get("ap", 0)
    bp = q.get("bp", 0)
    if ap and bp:
        return round((ap + bp) / 2, 2)
    lt = snap.get("latestTrade", {})
    return lt.get("p", 0)

def find_valid_option(underlying: str, direction: str, price: float) -> tuple[str, float] | tuple[None, None]:
    today = now_et().date()
    for extra in range(3):
        expiry = nearest_friday(today, max_days=7 + extra * 7)
        if expiry is None:
            continue
        strike = round(price + STRIKE_OFFSET if direction == "call" else price - STRIKE_OFFSET)
        sym    = occ_symbol(underlying, expiry, direction, float(strike))
        snap   = get_option_snapshot(sym)
        if snap:
            mid = option_mid(snap)
            if mid > 0:
                return sym, mid
    return None, None

# ── Position store ────────────────────────────────────────────────────────────
def load_positions() -> dict:
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text())
    return {}

def save_positions(data: dict) -> None:
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(data, indent=2))

# ── Orders ────────────────────────────────────────────────────────────────────
def place_market_order(symbol: str, qty: int, side: str = "buy") -> dict:
    return post(f"{BASE_URL}/orders", {
        "symbol": symbol, "qty": str(qty),
        "side": side, "type": "market", "time_in_force": "day",
    })

def close_position(symbol: str, qty: int) -> None:
    try:
        place_market_order(symbol, qty, side="sell")
        success(f"Close order submitted for {symbol}")
    except Exception as e:
        warn(f"Close order failed for {symbol}: {e}")

# ── Journal ───────────────────────────────────────────────────────────────────
def write_journal(date: datetime.date, session: dict) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path   = JOURNAL_DIR / f"{date.isoformat()}.md"
    regime = session.get("regime", "N/A")

    # Build ORB table rows for all symbols
    orb_rows = ""
    for sym in WATCHLIST:
        o = session.get("orbs", {}).get(sym, {})
        h     = o.get("high",   "N/A")
        l     = o.get("low",    "N/A")
        rng   = o.get("range",  "N/A")
        pct   = o.get("pct",    "N/A")
        stat  = o.get("status", "N/A")
        h_str  = f"${h:.2f}"  if isinstance(h,   float) else str(h)
        l_str  = f"${l:.2f}"  if isinstance(l,   float) else str(l)
        rng_str = f"${rng:.2f}" if isinstance(rng, float) else str(rng)
        pct_str = f"{pct:.2f}%" if isinstance(pct, float) else str(pct)
        orb_rows += f"| {sym:<4} | {h_str:<8} | {l_str:<8} | {rng_str:<8} | {pct_str:<7} | {stat} |\n"

    traded_sym = session.get("traded_symbol", "N/A")
    bdir       = session.get("direction",     "N/A")
    e_sym      = session.get("entry_symbol",  "N/A")
    e_px       = session.get("entry_price",   "N/A")
    e_qty      = session.get("entry_qty",     "N/A")
    x_px       = session.get("exit_price",    "N/A")
    x_reas     = session.get("exit_reason",   "N/A")
    pnl_p      = session.get("pnl_pct",      "N/A")
    pnl_d      = session.get("pnl_dollar",   "N/A")

    e_px_str  = f"${e_px:.2f}"  if isinstance(e_px, float) else str(e_px)
    x_px_str  = f"${x_px:.2f}"  if isinstance(x_px, float) else str(x_px)
    pnl_p_str = f"{pnl_p:.2f}%" if isinstance(pnl_p, float) else str(pnl_p)
    pnl_d_str = f"${pnl_d:.2f}" if isinstance(pnl_d, float) else str(pnl_d)

    md = f"""# ORB Session — {date.isoformat()}

## Regime
**{regime}**

## Opening Ranges
| Symbol | High     | Low      | Range    | Pct     | Status |
|--------|----------|----------|----------|---------|--------|
{orb_rows}
## Trade
| Field         | Value |
|---------------|-------|
| Traded Symbol | {traded_sym} |
| Direction     | {bdir} |
| Option Symbol | {e_sym} |
| Entry Price   | {e_px_str} |
| Qty           | {e_qty} |
| Exit Price    | {x_px_str} |
| Exit Reason   | {x_reas} |
| P&L (%)       | {pnl_p_str} |
| P&L ($)       | {pnl_d_str} |

## Notes
_Auto-generated by orb_scanner.py_
"""
    path.write_text(md)
    success(f"Journal written → {path}")

# ── PHASE 1: Range Definition ─────────────────────────────────────────────────
def phase_range_definition(min_range_pct: float) -> dict[str, dict]:
    """
    Build ORB for all symbols in WATCHLIST.
    Returns dict: { "SPY": {"high": X, "low": X, "pct": X, "valid": bool}, ... }
    """
    hdr("PHASE 1 — OPENING RANGE DEFINITION  (9:30 → 9:45 ET)")

    target = now_et().replace(hour=9, minute=45, second=0, microsecond=0)
    while True:
        remaining = (target - now_et()).total_seconds()
        if remaining <= 0:
            break
        mins, secs = divmod(int(remaining), 60)
        syms = " + ".join(WATCHLIST)
        print(
            f"\r  {CY}⏱  Range closes in {B}{mins:02d}:{secs:02d}{R}"
            f"{CY}  — watching {syms} 1-min bars ...{R}  ",
            end="", flush=True
        )
        time.sleep(30)
    print()

    date_str = now_et().date().isoformat()
    start    = f"{date_str}T09:30:00-04:00"
    end      = f"{date_str}T09:45:00-04:00"

    results = {}
    print()
    print(f"  {B}{'─'*58}{R}")

    for sym in WATCHLIST:
        info(f"Fetching 9:30-9:45 bars for {sym} ...")
        bars = fetch_bars(sym, start, end)
        if not bars:
            warn(f"  {sym}: no bars — skipping")
            results[sym] = {"valid": False, "status": "no data"}
            continue

        high      = max(b["h"] for b in bars)
        low       = min(b["l"] for b in bars)
        last_c    = bars[-1]["c"]
        rng       = high - low
        rng_pct   = rng / last_c * 100
        valid     = rng_pct >= min_range_pct * 100
        status    = "✔ Valid" if valid else f"✘ Too narrow (<{min_range_pct*100:.1f}%)"

        col_h = GR
        col_l = RD
        col_r = WH if valid else YL

        print(
            f"  {B}{sym:<4}{R}"
            f"  {col_h}H${high:.2f}{R}"
            f"  {col_l}L${low:.2f}{R}"
            f"  {col_r}Range ${rng:.2f} ({rng_pct:.2f}%){R}"
            f"  {DM}{status}{R}"
        )

        results[sym] = {
            "high":   high,
            "low":    low,
            "range":  round(rng, 2),
            "pct":    round(rng_pct, 2),
            "valid":  valid,
            "status": status,
        }

    print(f"  {B}{'─'*58}{R}\n")
    return results

# ── PHASE 2: Breakout Watch ───────────────────────────────────────────────────
def phase_breakout_watch(
    orbs: dict[str, dict],
    regime: str,
) -> tuple[str, str, float] | tuple[None, None, None]:
    """
    Watch all valid-ORB symbols for a body-close breakout.
    Returns (symbol, direction, price) or (None, None, None).

    Priority rules:
      - First breakout detected wins (1 trade per session).
      - Same-tick tie: SPY over QQQ.
      - Same-tick opposite directions: regime bias decides direction,
        then whichever symbol broke in that direction.
    """
    hdr("PHASE 2 — BREAKOUT WATCH  (9:45 → 10:30 ET)")

    valid_syms = [s for s in WATCHLIST if orbs.get(s, {}).get("valid")]
    if not valid_syms:
        warn("No symbols passed the range filter — nothing to watch.")
        return None, None, None

    info(f"Watching: {', '.join(valid_syms)}")
    print()

    # Print per-symbol ORB reference
    for sym in valid_syms:
        o = orbs[sym]
        print(f"  {B}{sym}{R}  ORB {GR}H${o['high']:.2f}{R} | {RD}L${o['low']:.2f}{R}")
    print()

    end_time   = now_et().replace(hour=10, minute=30, second=0, microsecond=0)
    first_tick = True

    while now_et() < end_time:
        time.sleep(60)
        n      = now_et()
        ts_str = n.strftime("%I:%M %p")

        # Skip first tick (9:45 bar may be the ORB candle itself)
        if first_tick:
            first_tick = False
            for sym in valid_syms:
                bar = fetch_latest_bar(sym)
                px  = bar["c"] if bar else 0
                info(f"[{ts_str}] {sym} ${px:.2f} | Skipping first candle after range")
            continue

        # Collect breakouts this tick
        breakouts: list[tuple[str, str, float]] = []   # (symbol, direction, price)

        for sym in valid_syms:
            bar = fetch_latest_bar(sym)
            if not bar:
                continue
            o, c      = bar["o"], bar["c"]
            body_high = max(o, c)
            body_low  = min(o, c)
            orb_h     = orbs[sym]["high"]
            orb_l     = orbs[sym]["low"]
            bar_col   = GR if c >= o else RD

            if body_low > orb_h:
                direction  = "call"
                status_txt = f"{B}{GR}BREAKOUT UP ▲{R}"
            elif body_high < orb_l:
                direction  = "put"
                status_txt = f"{B}{RD}BREAKOUT DOWN ▼{R}"
            else:
                direction  = None
                status_txt = f"{DM}No breakout{R}"

            print(
                f"  {DM}[{ts_str}]{R}  {B}{sym}{R} {bar_col}${c:.2f}{R}"
                f"  [{o:.2f}→{c:.2f}]"
                f"  |  {status_txt}"
                f"  |  H${orb_h:.2f} L${orb_l:.2f}"
            )

            if direction:
                breakouts.append((sym, direction, c))

        if not breakouts:
            continue

        # ── Resolve which trade to take ───────────────────────────────────────
        print()
        if len(breakouts) == 1:
            chosen_sym, chosen_dir, chosen_px = breakouts[0]
        else:
            # Multiple breakouts this tick — apply priority
            calls = [(s, d, p) for s, d, p in breakouts if d == "call"]
            puts  = [(s, d, p) for s, d, p in breakouts if d == "put"]

            if len(calls) > 0 and len(puts) > 0:
                # Opposite directions — use regime bias
                if regime in ("BULL_TRENDING",):
                    pool = calls
                    bold(f"  {GR}Tie (opposite dirs) — BULL regime → taking CALL side{R}")
                elif regime in ("BEAR_TRENDING",):
                    pool = puts
                    bold(f"  {RD}Tie (opposite dirs) — BEAR regime → taking PUT side{R}")
                else:
                    # SIDEWAYS / default — SPY always wins
                    pool = breakouts
                    bold(f"  Tie (opposite dirs) — SIDEWAYS regime → taking SPY")
            else:
                # Same direction — take first SPY, else first QQQ
                pool = breakouts

            # Within the pool, prefer SPY
            spy_hits = [(s, d, p) for s, d, p in pool if s == "SPY"]
            chosen_sym, chosen_dir, chosen_px = spy_hits[0] if spy_hits else pool[0]

        col = GR if chosen_dir == "call" else RD
        bold(f"  ⚡  {chosen_sym} {col}BREAKOUT {'UP' if chosen_dir=='call' else 'DOWN'} DETECTED — Entering {chosen_dir.upper()}{R}")
        print()
        return chosen_sym, chosen_dir, chosen_px

    warn("10:30 AM reached — no breakout detected. Session complete.")
    return None, None, None

# ── PHASE 3: Position Monitor ─────────────────────────────────────────────────
def phase_position_monitor(
    underlying: str,
    direction: str,
    entry_px: float,
    orbs: dict[str, dict],
    regime: str,
) -> dict:
    hdr(f"PHASE 3 — POSITION ENTRY & MONITOR  (until 11:00 ET)")

    session: dict = {
        "regime":         regime,
        "traded_symbol":  underlying,
        "direction":      direction,
        "orbs":           {
            sym: {k: v for k, v in orbs.get(sym, {}).items() if k != "valid"}
            for sym in WATCHLIST
        },
    }

    # ── Find option ───────────────────────────────────────────────────────────
    info(f"Searching for {underlying} {direction.upper()} option near ${entry_px:.2f} ...")
    opt_sym, opt_mid = find_valid_option(underlying, direction, entry_px)

    if opt_sym is None:
        err("Could not find a valid option contract — aborting trade.")
        session.update({"entry_symbol": "N/A", "exit_reason": "no contract found"})
        return session

    info(f"Found: {B}{opt_sym}{R} @ mid ${opt_mid:.2f}")

    # ── Qty ───────────────────────────────────────────────────────────────────
    cost_per = opt_mid * 100
    qty      = max(1, math.floor(MAX_BUDGET / cost_per))
    if qty * cost_per > MAX_BUDGET and qty > 1:
        qty -= 1
    total_cost = qty * cost_per

    info(f"Buying {B}{qty}{R} contract(s) — est. cost ${total_cost:.2f}")

    # ── Place order ───────────────────────────────────────────────────────────
    try:
        order    = place_market_order(opt_sym, qty)
        order_id = order.get("id", "?")
        success(f"Order placed: {order_id}")
    except Exception as e:
        err(f"Order failed: {e}")
        session.update({"entry_symbol": opt_sym, "exit_reason": f"order error: {e}"})
        return session

    time.sleep(3)
    entry_price = opt_mid

    session.update({
        "entry_symbol": opt_sym,
        "entry_price":  round(entry_price, 2),
        "entry_qty":    qty,
    })

    positions = load_positions()
    positions[opt_sym] = {
        "underlying":  underlying,
        "direction":   direction,
        "entry_price": entry_price,
        "qty":         qty,
        "order_id":    order_id,
        "entered_at":  now_et().isoformat(),
    }
    save_positions(positions)

    print()
    print(f"  {B}{'─'*56}{R}")
    print(f"  {B}{CY}  POSITION OPENED{R}")
    print(f"  Underlying : {B}{underlying}{R}")
    print(f"  Option     : {B}{opt_sym}{R}")
    print(f"  Direction  : {B}{direction.upper()}{R}")
    print(f"  Entry      : {B}${entry_price:.2f}{R}  x {qty} contracts")
    print(f"  Budget     : ${MAX_BUDGET}  |  Cost: ${total_cost:.2f}")
    print(f"  {B}{'─'*56}{R}\n")

    # ── Monitor loop ──────────────────────────────────────────────────────────
    time_stop   = now_et().replace(hour=11, minute=0, second=0, microsecond=0)
    exit_reason = "time stop"
    exit_price  = entry_price

    while now_et() < time_stop:
        time.sleep(60)
        ts_str = now_et().strftime("%I:%M %p")

        snap          = get_option_snapshot(opt_sym)
        current_price = entry_price
        if snap:
            mid = option_mid(snap)
            if mid > 0:
                current_price = mid

        pnl_pct  = (current_price - entry_price) / entry_price
        pnl_dol  = (current_price - entry_price) * 100 * qty
        pnl_col  = GR if pnl_pct >= 0 else RD
        pnl_sign = "+" if pnl_pct >= 0 else ""
        und_now  = fetch_latest_quote(underlying) or 0

        print(
            f"  {DM}[{ts_str}]{R}  {B}{opt_sym}{R}"
            f"  |  Entry ${entry_price:.2f}"
            f"  |  Now {pnl_col}${current_price:.2f}{R}"
            f"  |  P&L {pnl_col}{pnl_sign}{pnl_pct*100:.1f}%{R} (${pnl_dol:+.0f})"
            f"  |  {underlying} ${und_now:.2f}"
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

    # ── Close ─────────────────────────────────────────────────────────────────
    close_position(opt_sym, qty)

    pnl_pct_f = (exit_price - entry_price) / entry_price * 100
    pnl_dol_f = (exit_price - entry_price) * 100 * qty
    pnl_col_f = GR if pnl_pct_f >= 0 else RD

    print()
    print(f"  {B}{'─'*56}{R}")
    print(f"  {B}{WH}  SESSION COMPLETE{R}")
    print(f"  Symbol  : {opt_sym}")
    print(f"  Entry   : ${entry_price:.2f}  →  Exit: ${exit_price:.2f}")
    print(f"  Reason  : {exit_reason}")
    print(f"  P&L     : {pnl_col_f}{B}{pnl_pct_f:+.1f}%  (${pnl_dol_f:+.0f}){R}")
    print(f"  {B}{'─'*56}{R}\n")

    session.update({
        "exit_price":  round(exit_price, 2),
        "exit_reason": exit_reason,
        "pnl_pct":     round(pnl_pct_f, 2),
        "pnl_dollar":  round(pnl_dol_f, 2),
    })

    positions = load_positions()
    positions.pop(opt_sym, None)
    save_positions(positions)

    return session

# ── Wait until ET time ────────────────────────────────────────────────────────
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

# ── ORB bars fetch helper (used when already past 9:45) ──────────────────────
def fetch_orb_bars(min_range_pct: float) -> dict[str, dict]:
    date_str = now_et().date().isoformat()
    start    = f"{date_str}T09:30:00-04:00"
    end      = f"{date_str}T09:45:00-04:00"
    results  = {}
    for sym in WATCHLIST:
        bars = fetch_bars(sym, start, end)
        if not bars:
            results[sym] = {"valid": False, "status": "no data"}
            continue
        high    = max(b["h"] for b in bars)
        low     = min(b["l"] for b in bars)
        last_c  = bars[-1]["c"]
        rng     = high - low
        rng_pct = rng / last_c * 100
        valid   = rng_pct >= min_range_pct * 100
        status  = "✔ Valid" if valid else f"✘ Too narrow (<{min_range_pct*100:.1f}%)"
        info(f"{sym}  ORB H${high:.2f} L${low:.2f}  {rng_pct:.2f}%  {status}")
        results[sym] = {
            "high": high, "low": low,
            "range": round(rng, 2), "pct": round(rng_pct, 2),
            "valid": valid, "status": status,
        }
    return results

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    print()
    print(f"{B}{MG}{'█'*62}{R}")
    print(f"{B}{MG}  ORB SCANNER — SPY + QQQ Options  |  Opening Range Breakout{R}")
    print(f"{B}{MG}{'█'*62}{R}")
    print(f"  Strategy : 15-min ORB  |  Budget: ${MAX_BUDGET}  |  Target: +{PROFIT_TARGET*100:.0f}%  |  Stop: {STOP_LOSS*100:.0f}%")
    print(f"  Symbols  : {', '.join(WATCHLIST)}")
    print(f"  Started  : {now_et().strftime('%Y-%m-%d %I:%M:%S %p ET')}")
    print()

    # ── Regime ────────────────────────────────────────────────────────────────
    regime, min_range_pct = load_regime()
    print_regime(regime, min_range_pct)

    if regime == "HIGH_VOLATILITY":
        warn("HIGH VOLATILITY REGIME — SESSION SKIPPED")
        sys.exit(0)

    today = now_et().date()
    n     = now_et()

    # ── Pre-market wait ───────────────────────────────────────────────────────
    if n < n.replace(hour=9, minute=30, second=0, microsecond=0):
        wait_until(9, 30, "9:30 AM ET Market Open")

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    n = now_et()
    if n < n.replace(hour=9, minute=45, second=0, microsecond=0):
        orbs = phase_range_definition(min_range_pct)
    else:
        warn("Already past 9:45 — fetching completed ORB bars now")
        orbs = fetch_orb_bars(min_range_pct)

    any_valid = any(v.get("valid") for v in orbs.values())
    if not any_valid:
        warn("No symbol passed the range filter — SKIPPING SESSION")
        session = {
            "regime": regime,
            "traded_symbol": "N/A",
            "direction": "N/A",
            "orbs": {sym: {k: v for k, v in orbs.get(sym, {}).items() if k != "valid"} for sym in WATCHLIST},
            "entry_symbol": "N/A",
            "entry_price": "N/A",
            "entry_qty": "N/A",
            "exit_price": "N/A",
            "exit_reason": "all ranges too narrow",
            "pnl_pct": 0,
            "pnl_dollar": 0,
        }
        write_journal(today, session)
        sys.exit(0)

    # ── Phase 2 ───────────────────────────────────────────────────────────────
    n = now_et()
    if n > n.replace(hour=10, minute=30, second=0, microsecond=0):
        warn("Already past 10:30 — breakout window closed. Exiting.")
        sys.exit(0)

    chosen_sym, direction, price = phase_breakout_watch(orbs, regime)

    if chosen_sym is None:
        info("No breakout today. Session ended.")
        session = {
            "regime": regime,
            "traded_symbol": "N/A",
            "direction": "N/A",
            "orbs": {sym: {k: v for k, v in orbs.get(sym, {}).items() if k != "valid"} for sym in WATCHLIST},
            "entry_symbol": "N/A",
            "entry_price": "N/A",
            "entry_qty": "N/A",
            "exit_price": "N/A",
            "exit_reason": "no breakout detected",
            "pnl_pct": 0,
            "pnl_dollar": 0,
        }
        write_journal(today, session)
        sys.exit(0)

    # ── Phase 3 ───────────────────────────────────────────────────────────────
    session = phase_position_monitor(chosen_sym, direction, price, orbs, regime)
    write_journal(today, session)

    print(f"\n{B}{MG}  ORB SESSION COMPLETE — journal/{today.isoformat()}.md{R}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YL}  Interrupted by user. Exiting cleanly.{R}\n")
        sys.exit(0)
