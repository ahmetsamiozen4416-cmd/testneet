"""
trade_logger.py — Genişletilmiş trade log (Task 4)
Her kapanan işlem için tüm alanlar CSV'ye yazılır.
"""

import csv
import os
import time
import pathlib
from typing import Optional

TRADE_LOG_FILE = str(pathlib.Path(__file__).parent / "trade_log.csv")

# ---- Tüm alanlar (Task 4 gereksinimleri) ----
HEADERS = [
    "timestamp_entry",
    "timestamp_exit",
    "symbol",
    "side",
    "score",
    "btc_direction",
    "rsi_15m",
    "rsi_1h",
    "rsi_4h",
    "atr_ratio",
    "vol_ratio",
    "funding_rate",
    "trade_type",        # NORMAL / FALLBACK
    "lock_reason",       # varsa kilitleme sebebi
    "cooldown_active",   # bool
    "entry_price",
    "exit_price",
    "pnl",
    "result",            # WIN / LOSS / UNKNOWN
    "duration_sec",
    "day",
]


def write_trade_log(
    *,
    symbol:            str,
    side:              str,
    entry_price:       float,
    exit_price:        float,
    pnl:               float,
    result:            str,          # "WIN" | "LOSS" | "UNKNOWN"
    btc_direction:     str = "",
    duration_sec:      int = 0,
    score:             float = 0,
    rsi_15m:           Optional[float] = None,
    rsi_1h:            Optional[float] = None,
    rsi_4h:            Optional[float] = None,
    atr_ratio:         Optional[float] = None,
    vol_ratio:         Optional[float] = None,
    funding_rate:      Optional[float] = None,
    trade_type:        str = "NORMAL",
    lock_reason:       str = "",
    cooldown_active:   bool = False,
    timestamp_entry:   Optional[str] = None,
):
    """Her kapanan işlemi trade_log.csv'ye yazar."""
    now_str    = time.strftime("%Y-%m-%d %H:%M:%S")
    entry_str  = timestamp_entry or now_str

    row = {
        "timestamp_entry":  entry_str,
        "timestamp_exit":   now_str,
        "symbol":           symbol,
        "side":             side,
        "score":            score,
        "btc_direction":    btc_direction,
        "rsi_15m":          round(rsi_15m, 1)    if rsi_15m    is not None else "",
        "rsi_1h":           round(rsi_1h, 1)     if rsi_1h     is not None else "",
        "rsi_4h":           round(rsi_4h, 1)     if rsi_4h     is not None else "",
        "atr_ratio":        round(atr_ratio, 5)  if atr_ratio  is not None else "",
        "vol_ratio":        round(vol_ratio, 2)  if vol_ratio  is not None else "",
        "funding_rate":     round(funding_rate, 5) if funding_rate is not None else "",
        "trade_type":       trade_type,
        "lock_reason":      lock_reason,
        "cooldown_active":  "1" if cooldown_active else "0",
        "entry_price":      round(entry_price, 8),
        "exit_price":       round(exit_price, 8),
        "pnl":              round(pnl, 4),
        "result":           result,
        "duration_sec":     duration_sec,
        "day":              time.strftime("%Y-%m-%d"),
    }

    file_exists = os.path.isfile(TRADE_LOG_FILE)
    try:
        with open(TRADE_LOG_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HEADERS)
            if not file_exists:
                w.writeheader()
            w.writerow(row)
        print(f"📝 Trade log: {symbol} {side} {result} {pnl:+.4f}$ [{trade_type}]")
    except Exception as e:
        print(f"⚠️ Trade log yazılamadı: {e}")


def log_daily_summary(send_telegram_fn=None):
    """Günlük özet — 23:59 veya bot durdurulduğunda çağrılır."""
    if not os.path.isfile(TRADE_LOG_FILE):
        return
    try:
        with open(TRADE_LOG_FILE, "r") as f:
            rows = list(csv.DictReader(f))
        today      = time.strftime("%Y-%m-%d")
        today_rows = [r for r in rows if r.get("day") == today]
        if not today_rows:
            return

        wins       = [r for r in today_rows if r["result"] == "WIN"]
        losses     = [r for r in today_rows if r["result"] == "LOSS"]
        unknowns   = [r for r in today_rows if r["result"] == "UNKNOWN"]
        total_pnl  = sum(float(r["pnl"]) for r in today_rows)
        wr         = len(wins) / len(today_rows) * 100 if today_rows else 0
        fallbacks  = [r for r in today_rows if r.get("trade_type") == "FALLBACK"]

        msg = (
            f"📅 <b>GÜNLÜK ÖZET — {today}</b>\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"📊 İşlem: {len(today_rows)}  |  WR: %{wr:.0f}\n"
            f"✅ Kazanan: {len(wins)}  |  ❌ Kaybeden: {len(losses)}  |  ❓ Bilinmeyen: {len(unknowns)}\n"
            f"💰 Net PnL: <b>{total_pnl:+.3f}$</b>\n"
        )
        if fallbacks:
            msg += f"🔄 Fallback işlemler: {len(fallbacks)}\n"
        if today_rows:
            best  = max(today_rows, key=lambda r: float(r["pnl"]))
            worst = min(today_rows, key=lambda r: float(r["pnl"]))
            msg += f"🏆 En iyi: {best['symbol']} {float(best['pnl']):+.3f}$\n"
            msg += f"💀 En kötü: {worst['symbol']} {float(worst['pnl']):+.3f}$"

        print(msg)
        if send_telegram_fn:
            send_telegram_fn(msg)
    except Exception as e:
        print(f"⚠️ Günlük özet hatası: {e}")