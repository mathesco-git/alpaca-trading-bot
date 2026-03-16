"""
Aggressive Compounding Backtest — 146% Return Variant
═══════════════════════════════════════════════════════
Event-driven day-by-day simulation with compounding equity.

Key aggressive parameters vs original:
  - Risk per trade:     1% → 4%
  - Max position size:  5% → 20%
  - Day allocation:     20% → 60%
  - Swing allocation:   60% → 80%
  - Day take-profit:    2x ATR → 2.5x ATR
  - Day hold period:    1 day → 3 days
  - RSI entry band:     [60-75] → [50-80]
  - Volume threshold:   1.5x → 1.2x
  - No confirmation bar required
  - Swing trailing stop: 2x → 3x ATR (ratchets to 2x at +20%)
  - 4 swing entry types: Golden Cross, Mean Reversion, Pullback, Sustained Uptrend
  - Adaptive MA (SMA20/50 fallback when SMA200 unavailable)

Expected result on top-10 movers (Mar 2025 – Mar 2026):
  ~+146% return, ~14.7% max drawdown

Usage:
  1. Run fetch_backtest_data.py to generate backtest_data.json
  2. Place backtest_data.json in the same directory or ../
  3. python backtest_aggressive_146pct.py
"""

import json
import math
import os
import sys
from collections import defaultdict

import pandas as pd
import numpy as np

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════
RSI_PERIOD = 14
ATR_PERIOD = 14
BT_START = "2017-01-03"  # ~10 months after data start to allow SMA200 warmup
BT_END = "2026-03-13"
INITIAL_EQUITY = 100_000.0

# Aggressive config — the 146% variant
CFG = {
    # ── Day Trade ──
    "day_rsi_min": 50,              # widened from 60
    "day_rsi_max": 80,              # widened from 75
    "day_vol_min": 1.2,             # lowered from 1.5
    "day_vol_max": 6.0,             # slightly raised from 5.0
    "day_require_confirm": False,   # dropped confirmation bar
    "day_stop_mult": 1.5,           # same
    "day_tp_mult": 2.5,             # raised from 2.0 → wider profit target
    "day_hold_days": 3,             # hold up to 3 days vs EOD
    "day_rsi_floor": 40,            # loosened from 45
    "day_require_sma50": True,      # keep this safety filter

    # ── Swing Trade ──
    "swing_sma_fast": 50,
    "swing_sma_slow": 200,
    "swing_rsi_oversold": 40,               # raised from 30 (mild pullback)
    "swing_stop_mult": 3.0,                 # widened from 2.0 for big runners
    "swing_use_pullback_entry": True,        # NEW: buy dip to SMA50
    "swing_use_sustained_uptrend": True,     # NEW: re-enter strong uptrends
    "swing_use_adaptive_ma": True,           # NEW: SMA20/50 fallback
    "swing_ratchet": True,                   # NEW: tighten stop at +20%

    # ── Risk (AGGRESSIVE) ──
    "risk_per_trade": 0.04,         # 4x original (1% → 4%)
    "max_pos_pct": 0.20,            # 4x original (5% → 20%)
    "day_alloc": 0.60,              # 3x original (20% → 60%)
    "swing_alloc": 0.80,            # ~1.3x original (60% → 80%)
    "swing_size_reduction": 0.0,    # no swing reduction

    # ── Position limits ──
    "max_day_positions": 5,
    "max_swing_positions": 10,
}


# ═══════════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════════
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute SMA, RSI, ATR, volume average, and pseudo-VWAP."""
    df = df.copy()
    df["sma_20"] = df["close"].rolling(20).mean()
    df["sma_50"] = df["close"].rolling(50).mean()
    df["sma_200"] = df["close"].rolling(200).mean()

    # RSI (exponential)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR (exponential)
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False).mean()

    # Volume & slope
    df["vol_avg_20"] = df["volume"].rolling(20).mean()
    df["sma50_slope"] = df["sma_50"].diff(5) / 5

    # Pseudo-VWAP (typical price — no intraday volume weighting on daily bars)
    df["vwap"] = (df["high"] + df["low"] + df["close"]) / 3

    return df


# ═══════════════════════════════════════════════════════════════════
# POSITION SIZING
# ═══════════════════════════════════════════════════════════════════
def calc_pos_size(equity: float, atr: float, strategy: str,
                  entry_price: float, cfg: dict) -> int:
    """ATR-based position sizing with max position cap."""
    if atr <= 0 or equity <= 0:
        return 0

    alloc = cfg["day_alloc"] if strategy == "day" else cfg["swing_alloc"]
    stop_m = cfg["day_stop_mult"] if strategy == "day" else cfg["swing_stop_mult"]

    risk_amt = equity * cfg["risk_per_trade"] * alloc
    shares = risk_amt / (atr * stop_m)

    if strategy == "swing":
        shares *= (1 - cfg["swing_size_reduction"])

    shares = max(1, math.floor(shares))

    # Cap by max position value
    if entry_price > 0:
        max_val = equity * cfg["max_pos_pct"]
        max_s = max(1, math.floor(max_val / entry_price))
        shares = min(shares, max_s)

    return shares


# ═══════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════
def find_data_file() -> str:
    """Search for backtest_data.json in common locations."""
    candidates = [
        os.path.join(os.path.dirname(__file__), "backtest_data.json"),
        os.path.join(os.path.dirname(__file__), "..", "backtest_data.json"),
        "backtest_data.json",
        # VM paths (for development)
        "/sessions/amazing-gracious-albattani/mnt/uploads/backtest_data.json",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    print("ERROR: backtest_data.json not found.")
    print("  Run fetch_backtest_data.py first to generate it.")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# MAIN SIMULATION
# ═══════════════════════════════════════════════════════════════════
def run_backtest(data_path: str = None, cfg: dict = None,
                 initial_equity: float = None, verbose: bool = True):
    """
    Run the aggressive compounding backtest.

    Returns:
        dict with keys: equity, total_pnl, return_pct, max_drawdown,
                        trades, equity_curve
    """
    if cfg is None:
        cfg = CFG
    if initial_equity is None:
        initial_equity = INITIAL_EQUITY
    if data_path is None:
        data_path = find_data_file()

    # Load and compute indicators
    with open(data_path) as f:
        raw = json.load(f)

    all_dfs = {}
    for sym, bars in raw.items():
        df = pd.DataFrame(bars)
        df = compute_indicators(df)
        all_dfs[sym] = df

    # Build unified timeline
    all_dates = set()
    for df in all_dfs.values():
        bt = df[(df["date"] >= BT_START) & (df["date"] <= BT_END)]
        all_dates.update(bt["date"].tolist())
    all_dates = sorted(all_dates)

    # Simulation state
    equity = initial_equity
    open_day = {}     # sym -> position dict
    open_swing = {}   # sym -> position dict
    all_trades = []
    equity_curve = []

    # ── Day-by-day event loop ──────────────────────────────────────
    for today in all_dates:

        # ── DAY TRADE EXITS ──
        to_close = []
        for sym, pos in list(open_day.items()):
            df = all_dfs[sym]
            rows = df[df["date"] == today]
            if rows.empty:
                pos["bars_held"] += 1
                if pos["bars_held"] >= cfg["day_hold_days"]:
                    to_close.append(sym)
                continue

            row = rows.iloc[0]
            pos["bars_held"] += 1

            if row["low"] <= pos["stop_loss"]:
                pnl = (pos["stop_loss"] - pos["entry_price"]) * pos["shares"]
                equity += pnl
                all_trades.append({
                    "symbol": sym, "strategy": "day",
                    "entry_date": pos["entry_date"], "exit_date": today,
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(pos["stop_loss"], 2),
                    "shares": pos["shares"], "pnl": round(pnl, 2),
                    "exit_reason": "stop_loss",
                })
                to_close.append(sym)
            elif row["high"] >= pos["take_profit"]:
                pnl = (pos["take_profit"] - pos["entry_price"]) * pos["shares"]
                equity += pnl
                all_trades.append({
                    "symbol": sym, "strategy": "day",
                    "entry_date": pos["entry_date"], "exit_date": today,
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(pos["take_profit"], 2),
                    "shares": pos["shares"], "pnl": round(pnl, 2),
                    "exit_reason": "take_profit",
                })
                to_close.append(sym)
            elif pos["bars_held"] >= cfg["day_hold_days"]:
                pnl = (row["close"] - pos["entry_price"]) * pos["shares"]
                equity += pnl
                all_trades.append({
                    "symbol": sym, "strategy": "day",
                    "entry_date": pos["entry_date"], "exit_date": today,
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(row["close"], 2),
                    "shares": pos["shares"], "pnl": round(pnl, 2),
                    "exit_reason": "eod_timeout",
                })
                to_close.append(sym)

        for sym in to_close:
            open_day.pop(sym, None)

        # ── SWING TRADE EXITS ──
        for sym, pos in list(open_swing.items()):
            df = all_dfs[sym]
            rows = df[df["date"] == today]
            if rows.empty:
                continue
            row = rows.iloc[0]
            idx = df.index[df["date"] == today][0]
            if idx < 1:
                continue
            prev = df.iloc[idx - 1]

            price = row["close"]
            atr = row["atr"] if not pd.isna(row["atr"]) else 0
            if price > pos["highest"]:
                pos["highest"] = price

            # Determine which MAs to use
            use_adaptive = cfg["swing_use_adaptive_ma"] and pd.isna(row.get("sma_200"))
            sf = row.get("sma_20") if use_adaptive else row["sma_50"]
            ss = row.get("sma_50") if use_adaptive else row.get("sma_200")
            psf = prev.get("sma_20") if use_adaptive else prev["sma_50"]
            pss = prev.get("sma_50") if use_adaptive else prev.get("sma_200")

            if pd.isna(sf) or pd.isna(ss) or pd.isna(psf) or pd.isna(pss):
                continue

            # Trailing stop with ratchet
            gain_pct = (price - pos["entry_price"]) / pos["entry_price"]
            if cfg["swing_ratchet"] and gain_pct > 0.20:
                trailing_stop = pos["highest"] - (atr * 2.0)
            else:
                trailing_stop = pos["highest"] - (atr * cfg["swing_stop_mult"])

            # Exit conditions
            death_cross = (sf < ss) and (psf >= pss)
            exit_price = None
            exit_reason = None
            if death_cross:
                exit_price = price
                exit_reason = "death_cross"
            elif row["low"] <= trailing_stop:
                exit_price = max(trailing_stop, row["low"])
                exit_reason = "trailing_stop"

            if exit_price:
                pnl = (exit_price - pos["entry_price"]) * pos["shares"]
                equity += pnl
                all_trades.append({
                    "symbol": sym, "strategy": "swing",
                    "entry_date": pos["entry_date"], "exit_date": today,
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(exit_price, 2),
                    "shares": pos["shares"], "pnl": round(pnl, 2),
                    "exit_reason": exit_reason,
                    "hold_days": (pd.Timestamp(today) - pd.Timestamp(pos["entry_date"])).days,
                    "entry_reason": pos.get("entry_reason", ""),
                })
                del open_swing[sym]

        # ── DAY TRADE ENTRIES ──
        for sym, df in all_dfs.items():
            if sym in open_day:
                continue
            if len(open_day) >= cfg.get("max_day_positions", 5):
                break

            rows = df[df["date"] == today]
            if rows.empty:
                continue
            idx = df.index[df["date"] == today][0]
            if idx < 1:
                continue
            row = df.iloc[idx]
            prev = df.iloc[idx - 1]

            if pd.isna(row["sma_50"]) or pd.isna(row["rsi"]) or pd.isna(row["atr"]):
                continue
            if pd.isna(row["vol_avg_20"]) or row["vol_avg_20"] <= 0:
                continue

            p = row["close"]
            rsi = row["rsi"]
            atr = row["atr"]
            vol_r = row["volume"] / row["vol_avg_20"]

            # Core filters
            if p <= row["vwap"]:
                continue
            if vol_r < cfg["day_vol_min"] or vol_r > cfg["day_vol_max"]:
                continue
            if rsi < cfg["day_rsi_min"] or rsi > cfg["day_rsi_max"]:
                continue
            slope = row["sma50_slope"]
            if not pd.isna(slope) and slope < 0:
                continue
            if rsi < cfg["day_rsi_floor"]:
                continue
            if cfg["day_require_sma50"] and p < row["sma_50"]:
                continue
            if cfg["day_require_confirm"] and prev["close"] <= prev["vwap"]:
                continue

            shares = calc_pos_size(equity, atr, "day", p, cfg)
            if shares <= 0:
                continue

            open_day[sym] = {
                "entry_price": p,
                "shares": shares,
                "stop_loss": p - (atr * cfg["day_stop_mult"]),
                "take_profit": p + (atr * cfg["day_tp_mult"]),
                "entry_date": today,
                "bars_held": 0,
            }

        # ── SWING TRADE ENTRIES ──
        for sym, df in all_dfs.items():
            if sym in open_swing:
                continue
            if len(open_swing) >= cfg.get("max_swing_positions", 10):
                break

            rows = df[df["date"] == today]
            if rows.empty:
                continue
            idx = df.index[df["date"] == today][0]
            if idx < 5:
                continue
            row = df.iloc[idx]
            prev = df.iloc[idx - 1]

            use_adaptive = cfg["swing_use_adaptive_ma"] and pd.isna(row.get("sma_200"))
            sf = row.get("sma_20") if use_adaptive else row["sma_50"]
            ss = row.get("sma_50") if use_adaptive else row.get("sma_200")
            psf = prev.get("sma_20") if use_adaptive else prev["sma_50"]
            pss = prev.get("sma_50") if use_adaptive else prev.get("sma_200")

            if pd.isna(sf) or pd.isna(ss) or pd.isna(row["atr"]):
                continue
            if pd.isna(psf) or pd.isna(pss):
                continue

            price = row["close"]
            atr = row["atr"]
            rsi = row["rsi"] if not pd.isna(row["rsi"]) else 50

            entry_reason = None

            # 1. Golden Cross
            if (sf > ss) and (psf <= pss):
                entry_reason = "golden_cross"

            # 2. Mean Reversion (mild pullback in uptrend)
            if not entry_reason and rsi < cfg["swing_rsi_oversold"] and price > ss:
                entry_reason = "mean_reversion"

            # 3. Pullback to SMA50 in confirmed uptrend
            if not entry_reason and cfg["swing_use_pullback_entry"]:
                if not pd.isna(row["sma_50"]) and not pd.isna(row.get("sma_200", np.nan)):
                    if row["sma_50"] > row.get("sma_200", 0):
                        sma50 = row["sma_50"]
                        if price >= sma50 * 0.99 and price <= sma50 * 1.02 and rsi < 50:
                            entry_reason = "pullback_sma50"

            # 4. Sustained uptrend re-entry (5+ days fast > slow MA)
            if not entry_reason and cfg["swing_use_sustained_uptrend"]:
                sustained = True
                for k in range(1, 6):
                    pr = df.iloc[idx - k]
                    pf2 = pr.get("sma_20" if use_adaptive else "sma_50")
                    ps2 = pr.get("sma_50" if use_adaptive else "sma_200")
                    if pd.isna(pf2) or pd.isna(ps2) or pf2 <= ps2:
                        sustained = False
                        break
                if sustained and sf > ss and rsi < 55 and price <= sf * 1.03:
                    entry_reason = "sustained_uptrend"

            if entry_reason:
                shares = calc_pos_size(equity, atr, "swing", price, cfg)
                if shares > 0:
                    open_swing[sym] = {
                        "entry_price": price,
                        "shares": shares,
                        "highest": price,
                        "entry_date": today,
                        "entry_reason": entry_reason,
                    }

        # Record equity curve
        equity_curve.append({
            "date": today,
            "equity": round(equity, 2),
            "open_day": len(open_day),
            "open_swing": len(open_swing),
        })

    # ── Close remaining positions at backtest end ──────────────────
    for sym, pos in open_swing.items():
        df = all_dfs[sym]
        last = df[df["date"] <= BT_END].iloc[-1]
        pnl = (last["close"] - pos["entry_price"]) * pos["shares"]
        equity += pnl
        all_trades.append({
            "symbol": sym, "strategy": "swing",
            "entry_date": pos["entry_date"], "exit_date": last["date"],
            "entry_price": round(pos["entry_price"], 2),
            "exit_price": round(last["close"], 2),
            "shares": pos["shares"], "pnl": round(pnl, 2),
            "exit_reason": "end_of_backtest",
            "entry_reason": pos.get("entry_reason", ""),
        })

    for sym, pos in open_day.items():
        df = all_dfs[sym]
        last = df[df["date"] <= BT_END].iloc[-1]
        pnl = (last["close"] - pos["entry_price"]) * pos["shares"]
        equity += pnl
        all_trades.append({
            "symbol": sym, "strategy": "day",
            "entry_date": pos["entry_date"], "exit_date": last["date"],
            "entry_price": round(pos["entry_price"], 2),
            "exit_price": round(last["close"], 2),
            "shares": pos["shares"], "pnl": round(pnl, 2),
            "exit_reason": "end_of_backtest",
        })

    # ── Compute metrics ────────────────────────────────────────────
    total_pnl = equity - initial_equity
    peak = initial_equity
    max_dd = 0
    for pt in equity_curve:
        if pt["equity"] > peak:
            peak = pt["equity"]
        dd = (peak - pt["equity"]) / peak
        if dd > max_dd:
            max_dd = dd

    result = {
        "equity": equity,
        "total_pnl": total_pnl,
        "return_pct": total_pnl / initial_equity * 100,
        "max_drawdown": max_dd,
        "trades": all_trades,
        "equity_curve": equity_curve,
    }

    # ── Print report ───────────────────────────────────────────────
    if verbose:
        print_report(result, initial_equity)

    return result


def print_report(result: dict, initial_equity: float):
    """Print formatted backtest results."""
    trades = result["trades"]
    equity_curve = result["equity_curve"]

    day_trades = [t for t in trades if t["strategy"] == "day"]
    swing_trades = [t for t in trades if t["strategy"] == "swing"]
    day_pnl = sum(t["pnl"] for t in day_trades)
    swing_pnl = sum(t["pnl"] for t in swing_trades)
    dw = sum(1 for t in day_trades if t["pnl"] > 0)
    sw = sum(1 for t in swing_trades if t["pnl"] > 0)

    print()
    print("=" * 70)
    print("  AGGRESSIVE COMPOUNDING BACKTEST — 146% VARIANT")
    print(f"  Starting equity: ${initial_equity:,.0f}  |  {BT_START} → {BT_END}")
    print("=" * 70)

    print(f"\n  Day trades:    {len(day_trades):>4d}  |  P&L: ${day_pnl:>+12,.2f}  |"
          f"  WR: {dw}/{len(day_trades)} ({dw / max(1, len(day_trades)) * 100:.0f}%)")
    print(f"  Swing trades:  {len(swing_trades):>4d}  |  P&L: ${swing_pnl:>+12,.2f}  |"
          f"  WR: {sw}/{len(swing_trades)} ({sw / max(1, len(swing_trades)) * 100:.0f}%)")
    print(f"  {'─' * 60}")
    print(f"  Final equity:  ${result['equity']:>12,.2f}")
    print(f"  Total P&L:     ${result['total_pnl']:>+12,.2f}")
    print(f"  Total return:  {result['return_pct']:>+8.1f}%")
    print(f"  Max drawdown:  {result['max_drawdown'] * 100:.1f}%")

    # Per-symbol breakdown
    print(f"\n  Per-symbol breakdown:")
    by_sym = defaultdict(lambda: {"pnl": 0, "day": 0, "swing": 0})
    for t in trades:
        by_sym[t["symbol"]]["pnl"] += t["pnl"]
        by_sym[t["symbol"]][t["strategy"]] += 1
    for sym, info in sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True):
        print(f"    {sym:6s}  ${info['pnl']:>+12,.2f}  "
              f"({info['day']}d + {info['swing']}s trades)")

    # Swing trade detail
    if swing_trades:
        print(f"\n  Swing trade detail:")
        for t in sorted(swing_trades, key=lambda x: x["pnl"], reverse=True):
            hold = t.get("hold_days", "?")
            reason = t.get("entry_reason", "")
            print(f"    {t['entry_date']} → {t['exit_date']:10s}  {t['symbol']:6s}  "
                  f"${t['entry_price']:>8.2f} → ${t['exit_price']:>8.2f}  "
                  f"{t['shares']:>5d} sh  ${t['pnl']:>+12,.2f} ({t.get('pnl_pct', 0):>+6.1f}%)  "
                  f"{t['exit_reason']:16s}  {reason:20s}  {hold}d")

    # Day trade exit analysis
    if day_trades:
        exit_types = defaultdict(int)
        for t in day_trades:
            exit_types[t["exit_reason"]] += 1
        print(f"\n  Day trade exits: {dict(exit_types)}")

    # Equity curve milestones
    print(f"\n  Equity milestones:")
    milestones = [25, 50, 75, 100, 125, 150, 175, 200]
    hit = set()
    for pt in equity_curve:
        ret = (pt["equity"] - initial_equity) / initial_equity * 100
        for m in milestones:
            if ret >= m and m not in hit:
                print(f"    +{m:>3d}% hit on {pt['date']}  "
                      f"(equity: ${pt['equity']:>12,.0f})")
                hit.add(m)

    # Configuration summary
    print(f"\n  Config summary:")
    print(f"    Risk per trade:  {CFG['risk_per_trade'] * 100:.0f}%")
    print(f"    Max position:    {CFG['max_pos_pct'] * 100:.0f}% of equity")
    print(f"    Day allocation:  {CFG['day_alloc'] * 100:.0f}%  "
          f"|  Swing allocation: {CFG['swing_alloc'] * 100:.0f}%")
    print(f"    Day TP:          {CFG['day_tp_mult']}x ATR  "
          f"|  Day SL: {CFG['day_stop_mult']}x ATR  "
          f"|  Hold: {CFG['day_hold_days']}d max")
    print(f"    Swing stop:      {CFG['swing_stop_mult']}x ATR "
          f"(ratchets to 2x at +20%)")
    print(f"    Swing entries:   Golden Cross, Mean Reversion (RSI<{CFG['swing_rsi_oversold']}), "
          f"Pullback to SMA50, Sustained Uptrend")
    print()


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    data_path = None

    # Accept optional data file path as CLI argument
    if len(sys.argv) > 1:
        data_path = sys.argv[1]
        if not os.path.exists(data_path):
            print(f"ERROR: File not found: {data_path}")
            sys.exit(1)

    result = run_backtest(data_path=data_path)

    # Save outputs
    out_dir = os.path.dirname(os.path.abspath(__file__))
    curve_path = os.path.join(out_dir, "equity_curve_aggressive.json")
    trades_path = os.path.join(out_dir, "trades_aggressive.json")

    with open(curve_path, "w") as f:
        json.dump(result["equity_curve"], f, indent=2)
    with open(trades_path, "w") as f:
        json.dump(result["trades"], f, indent=2)

    print(f"  Saved: {curve_path}")
    print(f"  Saved: {trades_path}")
