"""
feature_engine.py — Tüm teknik indikatör hesaplamaları (Task 8 + Task 9)
Sadece DataFrame → features mantığı; API çağrısı YOK.
Aynı OHLCV verisi tekrar hesaplanmıyor (Task 9: cache/reuse).
"""

import pandas as pd
import pandas_ta as ta
from typing import Optional


# ---------------------------------------------------------------------------
# Ana indikatör hesaplama — tek noktadan çağrılır
# ---------------------------------------------------------------------------

def compute_features(df: pd.DataFrame) -> dict:
    """
    15m OHLCV DataFrame → tek bir features dict.
    Tüm indikatörler burada; data_collector veya başka modüller
    bu sonucu doğrudan kullanır (tekrar hesaplama YOK).
    """
    df = df.copy()

    # ---- RSI 14 (15m) ----
    df['RSI']    = ta.rsi(df['close'], length=14)

    # ---- EMA'lar ----
    df['EMA20']  = ta.ema(df['close'], length=20)
    df['EMA50']  = ta.ema(df['close'], length=50)
    df['EMA200'] = ta.ema(df['close'], length=200)

    # ---- ATR ----
    df['ATR']    = ta.atr(df['high'], df['low'], df['close'], length=14)

    # ---- Stochastic RSI ----
    stoch = ta.stochrsi(df['close'], length=14, rsi_length=14, k=3, d=3)
    if stoch is not None and not stoch.empty:
        df['StochK'] = stoch.iloc[:, 0]
        df['StochD'] = stoch.iloc[:, 1]
    else:
        df['StochK'] = None
        df['StochD'] = None

    # ---- MACD ----
    macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['MACD_hist'] = macd.iloc[:, 2] if (macd is not None and not macd.empty) else 0.0

    # ---- Bollinger Bands ----
    bb = ta.bbands(df['close'], length=20, std=2)
    if bb is not None and not bb.empty:
        df['BB_upper'] = bb.iloc[:, 0]
        df['BB_lower'] = bb.iloc[:, 2]
    else:
        df['BB_upper'] = None
        df['BB_lower'] = None

    # ---- Son barlardan özetler ----
    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    close = float(last['close'])

    ema20  = float(last['EMA20'])  if pd.notna(last['EMA20'])  else close
    ema50  = float(last['EMA50'])  if pd.notna(last['EMA50'])  else close
    ema200 = float(last['EMA200']) if pd.notna(last['EMA200']) else None
    atr    = float(last['ATR'])    if pd.notna(last['ATR'])    else 0.0

    atr_ratio = (atr / close) if close > 0 else 0.0

    rsi_15m        = float(last['RSI'])        if pd.notna(last['RSI'])        else None
    macd_hist      = float(last['MACD_hist'])  if pd.notna(last['MACD_hist'])  else None
    macd_hist_prev = float(prev['MACD_hist'])  if pd.notna(prev['MACD_hist'])  else None

    bb_upper = (float(last['BB_upper'])
                if last['BB_upper'] is not None and pd.notna(last['BB_upper']) else None)
    bb_lower = (float(last['BB_lower'])
                if last['BB_lower'] is not None and pd.notna(last['BB_lower']) else None)

    stoch_k = (float(last['StochK'])
               if last['StochK'] is not None and pd.notna(last['StochK']) else None)

    # ---- EMA200 etiketi ----
    if ema200:
        if close > ema200 * 1.002:   ema200_label = "ÜSTÜNDE✅"
        elif close < ema200 * 0.998: ema200_label = "ALTINDA❌"
        else:                         ema200_label = "SINIRDA⚠️"
    else:
        ema200_label = "N/A"

    # ---- Stoch etiketi ----
    if stoch_k is not None:
        if stoch_k > 80:   stoch_label = f"AŞIRI ALIM⚠️(K:{stoch_k:.0f})"
        elif stoch_k < 20: stoch_label = f"AŞIRI SATIM✅(K:{stoch_k:.0f})"
        else:               stoch_label = f"NÖTR(K:{stoch_k:.0f})"
    else:
        stoch_label = "N/A"

    # ---- Hacim ivmesi (son 1h vs 4h ortalama) ----
    vol_1h     = df['volume'].iloc[-4:].sum()
    vol_4h_avg = df['volume'].iloc[-16:].sum() / 4
    vol_spike  = round(vol_1h / vol_4h_avg, 2) if vol_4h_avg > 0 else 1.0
    if vol_spike >= 2.5:    vol_spike_label = f"GÜÇLÜ✅(x{vol_spike})"
    elif vol_spike >= 1.5:  vol_spike_label = f"ORTA(x{vol_spike})"
    else:                    vol_spike_label = f"ZAYIF❌(x{vol_spike})"

    avg_vol   = df['volume'].iloc[-21:-1].mean()
    vol_ratio = (float(last['volume']) / avg_vol) if avg_vol > 0 else 1.0

    # ---- Mum body oranı (son 3 mum) ----
    body_ratios = []
    for idx in [-1, -2, -3]:
        row = df.iloc[idx]
        body = abs(float(row['close']) - float(row['open']))
        wick = float(row['high']) - float(row['low'])
        body_ratios.append(body / wick if wick > 0 else 0)
    body_ratio = round(sum(body_ratios) / len(body_ratios), 2)
    if body_ratio >= 0.6:    body_label = f"GÜÇLÜ✅({body_ratio})"
    elif body_ratio >= 0.35: body_label = f"ORTA({body_ratio})"
    else:                    body_label = f"GÖLGE AĞIR❌({body_ratio})"

    # ---- Trend ----
    if   close > ema20 and ema20 > ema50: trend = "GÜÇLÜ YUKARI 🟢"
    elif close > ema20:                   trend = "YUKARI 🟡"
    elif close < ema20 and ema20 < ema50: trend = "GÜÇLÜ AŞAĞI 🔴"
    else:                                  trend = "AŞAĞI 🟠"

    # ---- Son 20 bar S/R ----
    support    = float(df['close'].iloc[-20:].min())
    resistance = float(df['close'].iloc[-20:].max())

    # ---- Son 1h range filtresi ----
    last_4 = df.iloc[-4:]
    high_1h = float(last_4['high'].max())
    low_1h  = float(last_4['low'].min())
    range_1h_pct = (high_1h - low_1h) / low_1h * 100 if low_1h > 0 else 0

    # ---- Hacim spike (ham, filtre için) ----
    vol_spike_raw = vol_spike

    return {
        "close":          close,
        "ema20":          ema20,
        "ema50":          ema50,
        "ema200":         ema200,
        "ema200_label":   ema200_label,
        "atr":            atr,
        "atr_ratio":      atr_ratio,
        "rsi_15m":        rsi_15m,
        "macd_hist":      macd_hist,
        "macd_hist_prev": macd_hist_prev,
        "bb_upper":       bb_upper,
        "bb_lower":       bb_lower,
        "stoch_k":        stoch_k,
        "stoch_label":    stoch_label,
        "vol_ratio":      vol_ratio,
        "vol_spike":      vol_spike,
        "vol_spike_label": vol_spike_label,
        "vol_spike_raw":  vol_spike_raw,
        "body_ratio":     body_ratio,
        "body_label":     body_label,
        "trend":          trend,
        "support":        support,
        "resistance":     resistance,
        "range_1h_pct":   range_1h_pct,
    }


# ---------------------------------------------------------------------------
# RSI helper — sadece ham bar listesinden hesapla (API çağrısı yapılmış bar listesi için)
# ---------------------------------------------------------------------------

def calc_rsi_from_bars(bars: list, period: int = 14) -> Optional[float]:
    """fetch_ohlcv sonucu [ts,o,h,l,c,v] listesinden RSI hesapla."""
    if not bars or len(bars) < period + 1:
        return None
    closes = [float(b[4]) for b in bars]
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains) / period, sum(losses) / period
    return 100.0 if al == 0 else round(100 - (100 / (1 + ag / al)), 1)


def label_rsi(rsi: Optional[float]) -> str:
    if rsi is None:     return "N/A"
    if rsi > 70:        return "AŞIRI ALIM"
    if rsi < 30:        return "AŞIRI SATIM"
    return "NÖTR"