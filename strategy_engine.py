"""
strategy_engine.py — TEK karar motoru (Task 1 + Task 2 + Task 3)
signal generation, scoring, veto reasons → BURADAN gelir.
main.py bu sonuçları ASLA yeniden yorumlamaz.

fetch_market_data() structured dict döndürür (metin rapor değil).
"""

import datetime
from typing import Optional

import market_data as md
import feature_engine as fe

# ---------------------------------------------------------------------------
# SABITLER
# ---------------------------------------------------------------------------
MIN_VOLUME_USDT         = 2_000_000
TOP_BY_VOLUME_A         = 30
TOP_BY_MOVEMENT_B       = 30
TOP_FINAL               = 7
OHLCV_LIMIT_15M         = 210
RSI_1H_BULL_LIMIT       = 76
RSI_1H_NORMAL_LIMIT     = 72
RSI_1H_SHORT_ENTRY_MAX  = 72
RSI_1H_SHORT_ENTRY_MIN  = 65
MIN_ATR_RATIO           = 0.006
MIN_LONG_MOMENTUM_PCT   = 2.0
MAX_SHORT_MOMENTUM_PCT  = 45.0
MIN_1H_RANGE_PCT        = 0.8
MIN_VOL_SPIKE_ENTRY     = 1.5

BLACKLIST = {
    "PAXG", "XAU", "XAG", "XAUT", "AGLD",
    "TUSD", "USDP", "USDC", "BUSD", "DAI", "FRAX", "FDUSD",
    "BTC",  "ETH", "RAVE", "BNB",
    "SKYAI", "TST", "FARTCOIN", "PLAY", "EDGE", "ORDI",
    "HYPE", "AVAX", "TAO", "TSLA",
}

# ---------------------------------------------------------------------------
# SKOR MOTORU
# ---------------------------------------------------------------------------

def score_coin(
    *,
    rsi_15m:        Optional[float],
    rsi_1h:         Optional[float],
    rsi_4h:         Optional[float] = None,
    macd_hist:      Optional[float],
    macd_hist_prev: Optional[float],
    price:          float,
    ema20:          float,
    bb_upper:       Optional[float],
    bb_lower:       Optional[float],
    vol_ratio:      float,
    atr:            float,
    change_24h:     float,
    btc_is_bull:    bool,
    btc_direction:  str = "",
    recently_closed_profit: bool = False,
    side:           str = "LONG",
    ema200_label:   str = "",
    stoch_k:        Optional[float] = None,
    ema200_val:     Optional[float] = None,
) -> tuple[int, list[str]]:
    skor      = 0
    reasons   = []
    is_long   = (side.upper() == "LONG")
    rsi_cap   = 10

    # ---- EMA200 ----
    if is_long:
        if ema200_label == "ALTINDA❌":
            rsi_cap = 0
            reasons.append("EMA200 ALTINDA❌ — yapısal trend bozuk, LONG YASAK")
        elif ema200_label == "ÜSTÜNDE✅":
            skor += 1
            reasons.append("EMA200 ÜSTÜNDE✅")

    # ---- Stoch RSI (LONG) ----
    if is_long and stoch_k is not None:
        if stoch_k > 80:
            rsi_cap = min(rsi_cap, 8)
            reasons.append(f"StochRSI AŞIRI ALIM⚠️ K:{stoch_k:.0f} — giriş riskli")
        elif stoch_k < 20:
            skor += 2
            reasons.append(f"StochRSI AŞIRI SATIM✅ K:{stoch_k:.0f} — güçlü giriş")
        elif stoch_k < 40:
            skor += 1
            reasons.append(f"StochRSI Düşük K:{stoch_k:.0f}")

    # ---- 1h RSI ----
    if rsi_1h is not None:
        if is_long:
            limit = RSI_1H_BULL_LIMIT if btc_is_bull else RSI_1H_NORMAL_LIMIT
            if rsi_1h > limit:
                rsi_cap = 7
                reasons.append(f"1h RSI={rsi_1h:.1f} ⚠️ BLOKLAYACAK (>{limit})")
            elif rsi_1h > 70:
                reasons.append(f"1h RSI={rsi_1h:.1f} Aşırı Alım (limit altı)")
            elif rsi_1h < 35:
                reasons.append(f"1h RSI={rsi_1h:.1f} Aşırı Satım")
            else:
                reasons.append(f"1h RSI={rsi_1h:.1f} Nötr")
        else:
            if rsi_1h < 25:
                rsi_cap = 7
                reasons.append(f"1h RSI={rsi_1h:.1f} ⚠️ AŞIRI SATIM — SHORT RİSKLİ (<25)")
            elif rsi_1h < RSI_1H_SHORT_ENTRY_MIN:
                rsi_cap = min(rsi_cap, 6)
                reasons.append(f"1h RSI={rsi_1h:.1f} ⚠️ BLOKLAYACAK — RSI<{RSI_1H_SHORT_ENTRY_MIN} SHORT YASAK")
            elif rsi_1h > 75:
                skor += 3
                reasons.append(f"1h RSI={rsi_1h:.1f} Güçlü Aşırı Alım — SHORT için ✅✅")
            elif rsi_1h > 70:
                skor += 2
                reasons.append(f"1h RSI={rsi_1h:.1f} Aşırı Alım — SHORT için ✅")
            elif rsi_1h > 65:
                skor += 1
                reasons.append(f"1h RSI={rsi_1h:.1f} Yüksek — SHORT için olası")
            else:
                reasons.append(f"1h RSI={rsi_1h:.1f} Nötr")

    # ---- 24s Momentum filtresi ----
    if is_long:
        if change_24h < MIN_LONG_MOMENTUM_PCT:
            rsi_cap = min(rsi_cap, 7)
            reasons.append(f"24s=%{change_24h:.1f} ⚠️ Momentum yetersiz (<{MIN_LONG_MOMENTUM_PCT}%)")
        elif change_24h >= 40.0:
            rsi_cap = min(rsi_cap, 6)
            reasons.append(f"24s=+%{change_24h:.1f} ⚠️ AŞIRI KOŞ — LONG riski yüksek (>%40)")
    else:
        if change_24h >= MAX_SHORT_MOMENTUM_PCT:
            rsi_cap = min(rsi_cap, 5)
            reasons.append(f"24s=+%{change_24h:.1f} ⚠️ MOMENTUM BLOK — SHORT YASAK (>%{MAX_SHORT_MOMENTUM_PCT})")
        elif change_24h > 10.0:
            skor += 2
            reasons.append(f"24s=+%{change_24h:.1f} Güçlü — SHORT için düşüş yakın ✅")
        elif change_24h > 3.0:
            skor += 1
            reasons.append(f"24s=+%{change_24h:.1f} Orta — SHORT için uygun")
        elif change_24h < -3.0:
            rsi_cap = min(rsi_cap, 6)
            reasons.append(f"24s=%{change_24h:.1f} Negatif — SHORT için trend kayması riski")

    # ---- 15m RSI ----
    if rsi_15m is not None:
        if is_long:
            if rsi_15m > 85:
                rsi_cap = min(rsi_cap, 7)
                reasons.append(f"15m RSI={rsi_15m:.1f} ⚠️ MOMENTUM TÜKENME — skor max 7 (>85)")
            elif rsi_15m > 60:
                skor += 1
                reasons.append(f"15m RSI={rsi_15m:.1f} Yüksek — dikkatli giriş")
            elif rsi_15m >= 40:
                skor += 2
                reasons.append(f"15m RSI={rsi_15m:.1f} İdeal giriş bölgesi ✅")
            elif rsi_15m >= 30:
                skor += 2
                reasons.append(f"15m RSI={rsi_15m:.1f} Düşük")
            else:
                skor += 3
                reasons.append(f"15m RSI={rsi_15m:.1f} Aşırı Satım ✅")
        else:
            if rsi_15m > 75:
                skor += 3
                reasons.append(f"15m RSI={rsi_15m:.1f} Aşırı Alım — SHORT için ✅")
            elif rsi_15m > 60:
                skor += 2
                reasons.append(f"15m RSI={rsi_15m:.1f} Yüksek — SHORT için olası dönüş")
            elif rsi_15m < 35:
                skor += 3
                reasons.append(f"15m RSI={rsi_15m:.1f} Aşırı Satım — SHORT devam ✅")
            elif rsi_15m < 45:
                skor += 2
                reasons.append(f"15m RSI={rsi_15m:.1f} Düşük — SHORT devam")
            else:
                skor += 1
                reasons.append(f"15m RSI={rsi_15m:.1f} Nötr")

    # ---- MACD ----
    if macd_hist is not None:
        if is_long:
            if macd_hist > 0.0001:
                skor += 2
                reasons.append("MACD Pozitif ✅")
                if macd_hist_prev is not None and macd_hist_prev < 0:
                    skor += 1
                    reasons.append("MACD Pivot Geçiş ✅✅")
            elif macd_hist > -0.0001:
                skor += 1
                reasons.append("MACD Nötr")
            else:
                reasons.append("MACD Negatif ❌")
        else:
            if macd_hist < -0.0001:
                skor += 2
                reasons.append("MACD Negatif — SHORT için ✅")
                if macd_hist_prev is not None and macd_hist_prev > 0:
                    skor += 1
                    reasons.append("MACD Negatif Pivot — SHORT için ✅✅")
            elif macd_hist < 0.0001:
                skor += 1
                reasons.append("MACD Nötr")
            else:
                reasons.append("MACD Pozitif — SHORT aleyhine ❌")

    # ---- Bollinger Bantları ----
    if bb_lower and bb_upper:
        if is_long:
            if price <= bb_lower * 1.001:
                skor += 2
                reasons.append("BB Alt Banda Değdi ✅")
            elif price >= bb_upper * 0.999:
                reasons.append("BB Üst Bandında ⚠️ — LONG için giriş riskli")
            elif price > (bb_lower + bb_upper) / 2:
                skor += 1
                reasons.append("BB Üst Yarı")
        else:
            if price >= bb_upper * 0.999:
                skor += 2
                reasons.append("BB Üst Bandında — SHORT için ✅")
            elif price >= (bb_lower + bb_upper) / 2 * 1.05:
                skor += 1
                reasons.append("BB Üst Yarı — SHORT için nötr")
            elif price <= bb_lower * 1.001:
                reasons.append("BB Alt Bandında — SHORT aleyhine ❌")

    # ---- 24s momentum bonus (LONG) ----
    if is_long:
        if change_24h >= 10.0:
            skor += 2
            reasons.append(f"24s Güçlü Momentum +%{change_24h:.1f} ✅")
        elif change_24h >= 3.0:
            skor += 1
            reasons.append(f"24s Pozitif +%{change_24h:.1f}")

    # ---- Momentum Devam Bonusu ----
    if is_long and recently_closed_profit and change_24h >= 10.0:
        skor += 1
        reasons.append("Momentum Devam Bonusu ✅")

    # ---- Hacim ----
    if vol_ratio >= 2.0:
        skor += 1
        reasons.append(f"Hacim x{vol_ratio:.1f} ✅")

    # ---- SHORT özel: EMA200 mesafesi + BTC yön ----
    if not is_long:
        if ema200_val is not None and ema200_val > 0:
            pve = (price - ema200_val) / ema200_val * 100
            if pve > 5.0:
                skor += 1
                reasons.append(f"EMA200 +%{pve:.1f} — aşırı genişleme, SHORT için ✅")
        if btc_direction in ("GÜÇLÜ AŞAĞI", "HAFİF AŞAĞI"):
            skor += 1
            reasons.append(f"BTC {btc_direction} — SHORT için bonus ✅")
        elif btc_direction == "GÜÇLÜ YUKARI":
            skor -= 2
            reasons.append("BTC GÜÇLÜ YUKARI — SHORT için ceza ❌")
        elif btc_direction == "HAFİF YUKARI":
            skor -= 1
            reasons.append("BTC HAFİF YUKARI — SHORT için küçük ceza")

    # ---- 4h RSI katkısı (SHORT için) ----
    if not is_long and rsi_4h is not None:
        if rsi_4h > 72:
            skor = min(skor + 2, 10)
            reasons.append(f"4h RSI={rsi_4h} Aşırı Alım — SHORT için ✅✅")
        elif rsi_4h > 65:
            skor = min(skor + 1, 10)
            reasons.append(f"4h RSI={rsi_4h} Yüksek — SHORT için ✅")

    return min(max(skor, 0), rsi_cap, 10), reasons


# ---------------------------------------------------------------------------
# ANA VERİ FONKSİYONU — Bybit piyasa taraması
# ---------------------------------------------------------------------------

def fetch_market_data(recently_closed_coins: set = None) -> dict:
    """
    Bybit piyasa taraması → structured dict.

    Dönüş:
    {
      "btc_direction": str,
      "btc_price": float,
      "btc_change": float,
      "eth_change": float,
      "btc_funding": float | None,
      "candidates": [ { symbol, base, side, score, reasons, features, meta } ],
      "error": str | None
    }
    """
    if recently_closed_coins is None:
        recently_closed_coins = set()

    # Düşük likidite saatleri (UTC+3)
    _hour = datetime.datetime.now(datetime.timezone.utc).hour + 3
    if _hour >= 24: _hour -= 24
    IS_LOW_LIQ    = _hour in {0, 1, 2, 3, 4, 5, 6}
    min_1h_range  = 0.4 if IS_LOW_LIQ else MIN_1H_RANGE_PCT
    min_vol_spike = 1.0 if IS_LOW_LIQ else MIN_VOL_SPIKE_ENTRY

    try:
        btc_t     = md.fetch_ticker("BTC/USDT:USDT")
        eth_t     = md.fetch_ticker("ETH/USDT:USDT")
        btc_price = float(btc_t['last'])

        # Bybit 'percentage' → 24s değişim yüzdesi
        btc_change = float(btc_t.get('percentage') or btc_t.get('change') or 0)
        eth_change = float(eth_t.get('percentage') or eth_t.get('change') or 0) if eth_t else 0.0

        # BTC yön
        if   btc_change >  1.5: btc_direction, btc_is_bull = "GÜÇLÜ YUKARI", True
        elif btc_change >  0.3: btc_direction, btc_is_bull = "HAFİF YUKARI", False
        elif btc_change > -0.3: btc_direction, btc_is_bull = "NÖTR",         False
        elif btc_change > -1.5: btc_direction, btc_is_bull = "HAFİF AŞAĞI",  False
        else:                    btc_direction, btc_is_bull = "GÜÇLÜ AŞAĞI",  False

        btc_funding = md.fetch_funding_rate("BTC/USDT:USDT")

        tickers = md.fetch_tickers()
        altcoin_pairs = []
        for symbol, t in tickers.items():
            # Bybit linear perpetual formatı: XXX/USDT:USDT
            if '/USDT:USDT' not in symbol:
                continue
            base = symbol.split('/')[0]
            if base in BLACKLIST:
                continue
            volume = float(t.get('quoteVolume', 0) or 0)
            if volume >= MIN_VOLUME_USDT:
                altcoin_pairs.append(t)

        top_vol  = sorted(altcoin_pairs,
                          key=lambda x: float(x.get('quoteVolume', 0) or 0),
                          reverse=True)[:TOP_BY_VOLUME_A]
        top_move = sorted(altcoin_pairs,
                          key=lambda x: abs(float(x.get('percentage', 0) or 0)),
                          reverse=True)[:TOP_BY_MOVEMENT_B]
        combined       = {t['symbol']: t for t in top_vol + top_move}
        candidates_raw = list(combined.values())

        long_results:  list[dict] = []
        short_results: list[dict] = []

        for pair in candidates_raw:
            symbol = pair['symbol']
            base   = symbol.split('/')[0].split(':')[0]
            if not base.isascii():
                continue
            if any(bl in base for bl in BLACKLIST):
                continue

            try:
                df = md.fetch_ohlcv(symbol, timeframe='15m', limit=OHLCV_LIMIT_15M)
                if df is None:
                    continue

                feat = fe.compute_features(df)

                if feat['range_1h_pct'] < min_1h_range:
                    continue
                if feat['vol_spike_raw'] < min_vol_spike:
                    continue
                if feat['atr_ratio'] < MIN_ATR_RATIO:
                    continue

                close      = feat['close']
                change_24h = float(pair.get('percentage') or pair.get('change') or 0)
                funding_rate = md.fetch_funding_rate(symbol)
                rsi_15m_lbl  = fe.label_rsi(feat['rsi_15m'])
                recently_closed_profit = base in recently_closed_coins

                skor_long, reasons_long = score_coin(
                    rsi_15m=feat['rsi_15m'], rsi_1h=None,
                    macd_hist=feat['macd_hist'], macd_hist_prev=feat['macd_hist_prev'],
                    price=close, ema20=feat['ema20'],
                    bb_upper=feat['bb_upper'], bb_lower=feat['bb_lower'],
                    vol_ratio=feat['vol_ratio'], atr=feat['atr'],
                    change_24h=change_24h, btc_is_bull=btc_is_bull,
                    recently_closed_profit=recently_closed_profit,
                    side="LONG", ema200_label=feat['ema200_label'],
                    stoch_k=feat['stoch_k'], ema200_val=feat['ema200'],
                    btc_direction=btc_direction,
                )
                skor_short, reasons_short = score_coin(
                    rsi_15m=feat['rsi_15m'], rsi_1h=None,
                    macd_hist=feat['macd_hist'], macd_hist_prev=feat['macd_hist_prev'],
                    price=close, ema20=feat['ema20'],
                    bb_upper=feat['bb_upper'], bb_lower=feat['bb_lower'],
                    vol_ratio=feat['vol_ratio'], atr=feat['atr'],
                    change_24h=change_24h, btc_is_bull=btc_is_bull,
                    recently_closed_profit=False,
                    side="SHORT", ema200_label=feat['ema200_label'],
                    stoch_k=feat['stoch_k'], ema200_val=feat['ema200'],
                    btc_direction=btc_direction,
                )

                base_entry = {
                    "symbol": symbol.split(':')[0],
                    "base":   base,
                    "meta": {
                        "close":           close,
                        "change_24h":      change_24h,
                        "volume":          float(pair.get('quoteVolume', 0) or 0),
                        "funding_rate":    funding_rate,
                        "trend":           feat['trend'],
                        "ema200_label":    feat['ema200_label'],
                        "stoch_label":     feat['stoch_label'],
                        "vol_spike_label": feat['vol_spike_label'],
                        "body_label":      feat['body_label'],
                        "rsi_15m_lbl":     rsi_15m_lbl,
                        "support":         feat['support'],
                        "resistance":      feat['resistance'],
                    },
                    "features": {
                        "rsi_15m":  feat['rsi_15m'],
                        "rsi_1h":   None,
                        "rsi_4h":   None,
                        "atr_ratio": feat['atr_ratio'],
                        "vol_ratio": feat['vol_ratio'],
                        "ema200":   feat['ema200'],
                        "stoch_k":  feat['stoch_k'],
                        "macd_hist": feat['macd_hist'],
                        "price":    close,
                    },
                }

                long_entry  = {**base_entry, "score": skor_long,  "reasons": reasons_long,  "side": "LONG"}
                short_entry = {**base_entry, "score": skor_short, "reasons": reasons_short, "side": "SHORT"}

                long_results.append(long_entry)

                scenario_a = (change_24h > 5.0
                              and (feat['rsi_15m'] is None or feat['rsi_15m'] > 55)
                              and change_24h < MAX_SHORT_MOMENTUM_PCT)
                scenario_b = (change_24h is not None and change_24h < -5.0
                              and feat['rsi_15m'] is not None
                              and 45 < feat['rsi_15m'] < 65
                              and skor_short >= 6)
                if (scenario_a or scenario_b) and change_24h < MAX_SHORT_MOMENTUM_PCT:
                    short_results.append(short_entry)

            except Exception:
                continue

        # ---- Sıralama ----
        long_results  = sorted(long_results,  key=lambda x: x['score'], reverse=True)[:TOP_FINAL]
        short_results = sorted(short_results, key=lambda x: x['score'], reverse=True)[:TOP_FINAL]

        # ---- Çakışma engeli ----
        long_bases  = {r['base']: r['score'] for r in long_results}
        short_bases = {r['base']: r['score'] for r in short_results}
        conflicts   = set(long_bases.keys()) & set(short_bases.keys())
        for base in conflicts:
            if long_bases[base] >= short_bases[base]:
                short_results = [r for r in short_results if r['base'] != base]
                print(f"⚠️  ÇAKIŞMA: {base} LONG({long_bases[base]}) >= SHORT → SHORT çıkarıldı")
            else:
                long_results = [r for r in long_results if r['base'] != base]
                print(f"⚠️  ÇAKIŞMA: {base} SHORT({short_bases[base]}) > LONG → LONG çıkarıldı")

        # ---- Finalist: 1h + 4h RSI çek, skoru yeniden hesapla ----
        for result_list in [long_results, short_results]:
            for r in result_list:
                try:
                    sym_key = r['symbol']
                    if not sym_key.endswith(':USDT'):
                        sym_key = sym_key + ':USDT'

                    bars_1h = md.exchange().fetch_ohlcv(sym_key, timeframe='1h', limit=15)
                    rsi_1h  = fe.calc_rsi_from_bars(bars_1h)
                    r['features']['rsi_1h'] = rsi_1h
                    r['meta']['rsi_1h_lbl'] = fe.label_rsi(rsi_1h)

                    bars_4h = md.exchange().fetch_ohlcv(sym_key, timeframe='4h', limit=15)
                    rsi_4h  = fe.calc_rsi_from_bars(bars_4h)
                    r['features']['rsi_4h'] = rsi_4h
                    if rsi_4h is not None:
                        if rsi_4h > 70:   r['meta']['rsi_4h_label'] = f"AŞIRI ALIM⚠️({rsi_4h})"
                        elif rsi_4h < 30: r['meta']['rsi_4h_label'] = f"AŞIRI SATIM✅({rsi_4h})"
                        else:              r['meta']['rsi_4h_label'] = f"NÖTR({rsi_4h})"
                    else:
                        r['meta']['rsi_4h_label'] = "N/A"

                    feat_snap = r['features']
                    meta_snap = r['meta']
                    recently_closed_profit = r['base'] in (recently_closed_coins or set())
                    new_score, new_reasons = score_coin(
                        rsi_15m=feat_snap.get('rsi_15m'),
                        rsi_1h=rsi_1h,
                        rsi_4h=rsi_4h,
                        macd_hist=feat_snap.get('macd_hist'),
                        macd_hist_prev=None,
                        price=feat_snap.get('price', 0),
                        ema20=feat_snap.get('ema200', feat_snap.get('price', 0)),
                        bb_upper=None, bb_lower=None,
                        vol_ratio=feat_snap.get('vol_ratio', 1.0),
                        atr=0,
                        change_24h=meta_snap.get('change_24h', 0),
                        btc_is_bull=btc_is_bull,
                        btc_direction=btc_direction,
                        recently_closed_profit=recently_closed_profit,
                        side=r['side'],
                        ema200_label=meta_snap.get('ema200_label', ''),
                        stoch_k=feat_snap.get('stoch_k'),
                        ema200_val=feat_snap.get('ema200'),
                    )
                    r['score']   = new_score
                    r['reasons'] = new_reasons

                except Exception:
                    r['features']['rsi_1h']   = None
                    r['features']['rsi_4h']   = None
                    r['meta']['rsi_4h_label'] = "N/A"
                    r['meta']['rsi_1h_lbl']   = "N/A"

        MIN_SCORE_FINALIST = 8
        long_results  = sorted([r for r in long_results  if r['score'] >= MIN_SCORE_FINALIST],
                               key=lambda x: x['score'], reverse=True)
        short_results = sorted([r for r in short_results if r['score'] >= MIN_SCORE_FINALIST],
                               key=lambda x: x['score'], reverse=True)

        # ---- Debug özet ----
        print(f"\n{'='*50}")
        print(f"📊 ADAY ÖZET | BTC: {btc_direction} ({btc_change:+.2f}%)")
        print(f"🟢 LONG  : {len(long_results)} aday")
        for r in long_results:
            print(f"   {r['base']:10s} skor={r['score']}/10 | "
                  f"1hRSI={r['features']['rsi_1h'] or 'N/A'} | "
                  f"24s={r['meta']['change_24h']:+.1f}% | "
                  f"EMA200={r['meta']['ema200_label']}")
        print(f"🔴 SHORT : {len(short_results)} aday")
        for r in short_results:
            print(f"   {r['base']:10s} skor={r['score']}/10 | "
                  f"1hRSI={r['features']['rsi_1h'] or 'N/A'} | "
                  f"24s={r['meta']['change_24h']:+.1f}%")
        if not short_results:
            print("   ⚠️  SHORT listesi boş")
        print(f"{'='*50}\n")

        return {
            "btc_direction": btc_direction,
            "btc_price":    btc_price,
            "btc_change":   btc_change,
            "eth_change":   eth_change,
            "btc_funding":  btc_funding,
            "candidates":   long_results + short_results,
            "error":        None,
        }

    except Exception as e:
        return {"btc_direction": "NÖTR", "candidates": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Geriye dönük uyumluluk — eski isim çağrılırsa çalışsın
# ---------------------------------------------------------------------------
def fetch_binance_data(recently_closed_coins: set = None) -> dict:
    """Eski isim — fetch_market_data'ya yönlendir."""
    return fetch_market_data(recently_closed_coins)