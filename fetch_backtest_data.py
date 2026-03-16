"""
Fetch 10 years of historical OHLCV data for the top 100 movers from Yahoo Finance.
Produces a single JSON file that the backtest engine can consume.

Two-phase approach:
  Phase 1: Screen ~500 large-cap tickers to find the top 100 by % gain over
           the past 10 years (or longest available history).
  Phase 2: Download full 10-year daily OHLCV for those top 100.

Usage:
    pip install yfinance
    python fetch_backtest_data.py

Output: backtest_data.json in the same directory.
"""

import json
import os
import sys
import time
import datetime
import yfinance as yf

# ═══════════════════════════════════════════════════════════════════
# CANDIDATE UNIVERSE — ~500 large/mid-cap US equities
# ═══════════════════════════════════════════════════════════════════
# Broad cross-section: S&P 500 core + popular mid-caps + high-growth names.
# The screening phase will rank them by 10-year return and keep the top 100.
CANDIDATE_SYMBOLS = [
    # ── Mega-cap tech ──
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "TSM", "AVGO",
    "ORCL", "ADBE", "CRM", "AMD", "INTC", "QCOM", "TXN", "MU", "AMAT", "LRCX",
    "KLAC", "MRVL", "SNPS", "CDNS", "FTNT", "PANW", "CRWD", "ZS", "NET", "DDOG",
    "SNOW", "MDB", "PLTR", "NOW", "WDAY", "TEAM", "HUBS", "TTD", "SHOP", "SQ",
    "PYPL", "INTU", "ADSK", "ANSS", "NXPI", "ON", "MPWR", "MCHP", "ADI", "SWKS",

    # ── Semiconductors & hardware ──
    "ARM", "SMCI", "DELL", "HPQ", "HPE", "WDC", "STX", "SNDK", "LITE", "CIEN",
    "ANET", "KEYS", "TER", "ENTG", "ONTO", "COHR", "IIVI", "WOLF", "GFS", "UMC",

    # ── Software / SaaS / Cloud ──
    "MSFT", "AMZN", "GOOG", "IBM", "SAP", "VMW", "SPLK", "OKTA", "TWLO", "DOCU",
    "VEEV", "ZM", "ROKU", "SPOT", "U", "RBLX", "PATH", "CFLT", "MNDY", "BILL",
    "PCOR", "ESTC", "GTLB", "IOT", "AI", "BBAI", "SOUN", "ASAN", "FIVN", "TOST",

    # ── Fintech / Financial ──
    "V", "MA", "AXP", "GS", "JPM", "BAC", "MS", "WFC", "C", "SCHW",
    "BLK", "KKR", "APO", "COIN", "HOOD", "SOFI", "AFRM", "UPST", "LC", "NU",
    "FIS", "FISV", "GPN", "FI", "TOST", "FOUR", "DLO", "PAYO", "FLYW", "PSFE",

    # ── Healthcare / Biotech ──
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "SYK",
    "ISRG", "BSX", "MDT", "GILD", "AMGN", "BIIB", "REGN", "VRTX", "MRNA", "BNTX",
    "DXCM", "ALGN", "HOLX", "IDXX", "PODD", "INSP", "RXRX", "BEAM", "CRSP", "EDIT",
    "NTLA", "DNA", "IRTC", "HIMS", "DOCS", "TDOC", "VEEV", "CRL", "IQV", "EXAS",

    # ── Consumer / Retail / E-commerce ──
    "AMZN", "WMT", "COST", "HD", "LOW", "TGT", "TJX", "ROST", "DG", "DLTR",
    "NKE", "LULU", "DECK", "ON", "BIRD", "CROX", "BOOT", "MNST", "CELH", "KO",
    "PEP", "SBUX", "MCD", "CMG", "CAVA", "DPZ", "YUM", "QSR", "DASH", "UBER",
    "LYFT", "ABNB", "BKNG", "EXPE", "MAR", "HLT", "RCL", "CCL", "NCLH", "WYNN",

    # ── Industrials / Energy / Materials ──
    "CAT", "DE", "HON", "GE", "RTX", "LMT", "NOC", "BA", "GD", "TDG",
    "AXON", "VRSK", "TT", "EMR", "ETN", "ROK", "AME", "ITW", "PH", "DOV",
    "XOM", "CVX", "COP", "SLB", "HAL", "OXY", "EOG", "DVN", "MPC", "VLO",
    "FCX", "NEM", "GOLD", "BHP", "RIO", "VALE", "NUE", "STLD", "CLF", "AA",

    # ── EVs / Clean energy / Autos ──
    "TSLA", "RIVN", "LCID", "NIO", "XPEV", "LI", "F", "GM", "TM", "HMC",
    "ENPH", "SEDG", "FSLR", "RUN", "NOVA", "PLUG", "BE", "CHPT", "BLNK", "QS",

    # ── Real Estate / REITs ──
    "AMT", "CCI", "PLD", "EQIX", "DLR", "PSA", "SPG", "O", "WELL", "AVB",

    # ── Media / Entertainment / Communication ──
    "DIS", "NFLX", "CMCSA", "WBD", "PARA", "FOX", "LYV", "MTCH", "PINS", "SNAP",
    "RDIT", "DUOL", "TTWO", "EA", "ATVI", "MSFT", "NTES", "SE", "GRAB", "CPNG",

    # ── Crypto / Blockchain-adjacent ──
    "COIN", "MARA", "RIOT", "CLSK", "HUT", "MSTR", "BITF", "BTBT", "CIFR", "WULF",

    # ── Misc high-growth / popular ──
    "APP", "TMDX", "CELH", "CAVA", "DUOL", "AXON", "FICO", "VRSN", "GDDY", "GEN",
    "CYBR", "TENB", "RPD", "S", "QLYS", "VRNS", "CWAN", "SMAR", "APPF", "PCTY",
    "PAYC", "WK", "BRZE", "ALTR", "IONQ", "RGTI", "QUBT", "QBTS", "ARQQ", "ACHR",

    # ── Additional large-caps for breadth ──
    "BRK-B", "UNP", "ADP", "SPGI", "ICE", "MCO", "MSCI", "CPRT", "CTAS", "PAYX",
    "ODFL", "FAST", "WST", "POOL", "TECH", "BIO", "ILMN", "A", "WAT", "MTD",
]

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════
TOP_N = 100                # Keep top N movers
YEARS_OF_DATA = 10         # Fetch this many years
BATCH_PAUSE = 0.3          # Seconds between requests (be nice to Yahoo)
SCREEN_PERIOD = "10y"      # yfinance period for screening
OUTPUT_FILE = "backtest_data.json"

END_DATE = datetime.date.today()
START_DATE = END_DATE - datetime.timedelta(days=YEARS_OF_DATA * 365 + 60)  # ~10yr + buffer for SMA200


def dedupe_symbols(symbols: list) -> list:
    """Remove duplicate tickers while preserving order."""
    seen = set()
    result = []
    for s in symbols:
        s = s.strip().upper()
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def screen_top_movers(symbols: list, top_n: int = 100) -> list:
    """
    Phase 1: Quick screen — download 10y monthly data for all candidates,
    compute total return, and return the top N by % gain.
    """
    print("=" * 70)
    print(f"  PHASE 1: Screening {len(symbols)} candidates for top {top_n} movers")
    print("=" * 70)

    returns = []
    errors = 0

    for i, sym in enumerate(symbols):
        if (i + 1) % 50 == 0 or i == 0:
            print(f"\n  Progress: {i + 1}/{len(symbols)}...")

        try:
            ticker = yf.Ticker(sym)
            # Use monthly data for fast screening
            hist = ticker.history(period=SCREEN_PERIOD, interval="1mo")

            if hist.empty or len(hist) < 2:
                continue

            first_close = float(hist["Close"].iloc[0])
            last_close = float(hist["Close"].iloc[-1])

            if first_close <= 0:
                continue

            pct_return = (last_close - first_close) / first_close * 100
            years_available = len(hist) / 12

            returns.append({
                "symbol": sym,
                "return_pct": round(pct_return, 1),
                "first_close": round(first_close, 2),
                "last_close": round(last_close, 2),
                "months": len(hist),
                "years": round(years_available, 1),
            })

        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"    Warning: {sym} — {e}")

        time.sleep(BATCH_PAUSE)

    # Sort by return, descending
    returns.sort(key=lambda x: x["return_pct"], reverse=True)
    top = returns[:top_n]

    print(f"\n  Screened {len(returns)} valid symbols, {errors} errors")
    print(f"\n  Top {top_n} movers (10-year total return):")
    print(f"  {'Rank':>4s}  {'Symbol':6s}  {'Return':>10s}  {'From':>10s}  {'To':>10s}  {'Years':>5s}")
    print(f"  {'─' * 55}")

    for i, r in enumerate(top[:30]):  # Print top 30 for brevity
        print(f"  {i+1:>4d}  {r['symbol']:6s}  {r['return_pct']:>+9.1f}%  "
              f"${r['first_close']:>8.2f}  ${r['last_close']:>8.2f}  {r['years']:>5.1f}")

    if len(top) > 30:
        print(f"  ... and {len(top) - 30} more")

    print(f"\n  Return range: {top[0]['return_pct']:+.1f}% to {top[-1]['return_pct']:+.1f}%")

    return [r["symbol"] for r in top]


def fetch_daily_data(symbols: list) -> dict:
    """
    Phase 2: Download full daily OHLCV data for the selected symbols.
    """
    print()
    print("=" * 70)
    print(f"  PHASE 2: Fetching 10-year daily data for {len(symbols)} symbols")
    print(f"  Date range: {START_DATE} → {END_DATE}")
    print("=" * 70)

    all_data = {}
    errors = 0

    for i, sym in enumerate(symbols):
        pct_done = (i + 1) / len(symbols) * 100
        print(f"  [{i+1:>3d}/{len(symbols)}] {pct_done:5.1f}%  {sym:6s} ...", end=" ", flush=True)

        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(start=str(START_DATE), end=str(END_DATE), interval="1d")

            if df.empty:
                print("NO DATA — skipping")
                continue

            # Convert to serializable records
            records = []
            for idx, row in df.iterrows():
                records.append({
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": round(float(row["Open"]), 4),
                    "high": round(float(row["High"]), 4),
                    "low": round(float(row["Low"]), 4),
                    "close": round(float(row["Close"]), 4),
                    "volume": int(row["Volume"]),
                })

            all_data[sym] = records

            first_close = records[0]["close"]
            last_close = records[-1]["close"]
            pct = (last_close - first_close) / first_close * 100
            print(f"OK — {len(records):>5d} bars, "
                  f"${first_close:>8.2f} → ${last_close:>8.2f} ({pct:>+8.1f}%)")

        except Exception as e:
            errors += 1
            print(f"ERROR: {e}")

        time.sleep(BATCH_PAUSE)

    print(f"\n  Fetched {len(all_data)} symbols successfully, {errors} errors")
    return all_data


def main():
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║  MTH-TRADER — Historical Data Fetcher                              ║")
    print("║  10 years of daily OHLCV for the top 100 movers                    ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()

    # Dedupe candidates
    candidates = dedupe_symbols(CANDIDATE_SYMBOLS)
    print(f"  Unique candidate symbols: {len(candidates)}")

    # Phase 1: Screen for top movers
    top_symbols = screen_top_movers(candidates, top_n=TOP_N)

    # Phase 2: Fetch daily data
    all_data = fetch_daily_data(top_symbols)

    # Save to JSON
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_FILE)
    with open(output_path, "w") as f:
        json.dump(all_data, f, indent=2)

    total_bars = sum(len(v) for v in all_data.values())
    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)

    print()
    print("=" * 70)
    print(f"  DONE!")
    print(f"  Symbols:    {len(all_data)}")
    print(f"  Total bars: {total_bars:,}")
    print(f"  File size:  {file_size_mb:.1f} MB")
    print(f"  Saved to:   {output_path}")
    print("=" * 70)

    # Also save the symbol ranking for reference
    ranking_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "top100_symbols.json")
    with open(ranking_path, "w") as f:
        json.dump(top_symbols, f, indent=2)
    print(f"  Symbol list: {ranking_path}")


if __name__ == "__main__":
    main()
