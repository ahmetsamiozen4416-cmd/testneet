"""
execution_engine.py — Emir gönderme + TP/SL tespiti (Task 5 + Task 8)

TP/SL tespiti artık SADECE:
  1. fetch_my_trades()  — en güvenilir
  2. fetch_open_orders() — SL kaldıysa TP doldu
  3. "UNKNOWN" — fiyat karşılaştırma KALDIRILDI (Task 5)
"""

import re
import time
import pandas as pd
from typing import Optional

import market_data as md
import risk_manager as rm
from risk_manager import FIXED_MIN_USDT
from state_manager import get_state, _lock, record_trade_open, record_trade_close

# ---- Parametreler ----
LEVERAGE               = 5
RISK_PER_TRADE         = 0.09
RISK_REDUCED_RATIO     = 0.05
FIXED_MIN_NOTIONAL     = 25.0

# ---- Simüle edilmiş bakiye limiti ----
# None → gerçek bakiye kullanılır (canlıya geçince None yap)
# 150  → testnet'te sanki 150 USDT varmış gibi davranır
SIMULATED_BALANCE      = 150.0
TARGET_TP_RATIO        = 0.020
STOP_LOSS_RATIO        = 0.010
STOP_LOSS_HIGH_VOL     = 0.015
ATR_SL_MULTIPLIER      = 1.5
ATR_SL_MIN             = 0.008
ATR_SL_MAX             = 0.012   # %1.8 → %1.2: zarar asimetrisi düzeltme (veri kanıtı)
COIN_BLACKLIST = {
    "PAXG", "XAU", "XAG", "XAUT", "AGLD",
    "TUSD", "USDP", "USDC", "BUSD", "DAI", "FRAX", "FDUSD",
    "BTC",  "ETH",  "BNB", "RAVE",
    "SKYAI", "TST", "FARTCOIN", "PLAY", "EDGE", "ORDI",
    "HYPE", "AVAX", "TAO", "TSLA",
}

# ---------------------------------------------------------------------------
# SEMBOL ÇÖZÜMLEME
# ---------------------------------------------------------------------------

def resolve_market_symbol(coin_input: str) -> Optional[str]:
    markets = md.get_markets()
    raw = coin_input.strip().upper()
    cb  = re.sub(r'[:/].*$', '', raw).replace('USDT', '')
    if not cb:
        return None
    for sym in [f"{cb}/USDT:USDT", f"1000{cb}/USDT:USDT", f"{cb}USDT", f"{cb}/USDT"]:
        if sym in markets:
            return sym
    return None


# ---------------------------------------------------------------------------
# TP / SL TESPİTİ (Task 5)
# ---------------------------------------------------------------------------

def detect_trade_outcome(symbol: str) -> str:
    """
    Kapanan işlemin sonucunu tespit eder.
    Dönüş: "WIN" | "LOSS" | "UNKNOWN"

    Yöntem 1: fetch_my_trades — order type'a bak
    Yöntem 2: fetch_open_orders — SL hâlâ açıksa TP doldu
    Yöntem 3: UNKNOWN — fiyat karşılaştırması KULLANILMIYOR (Task 5)
    """
    # --- Yöntem 1: Son işlem kaydı ---
    try:
        trades = md.fetch_my_trades(symbol, limit=5)
        if trades:
            for t in reversed(trades):
                order_type = (t.get('type') or '').upper()
                info_type  = (t.get('info', {}).get('orderType') or
                              t.get('info', {}).get('type') or '').upper()
                if 'TAKE_PROFIT' in order_type or 'TAKE_PROFIT' in info_type:
                    print(f"✅ TP tespit edildi (trade kaydı): {symbol}")
                    return "WIN"
                if ('STOP_MARKET' in order_type or 'STOP_MARKET' in info_type or
                        'STOP' == order_type or 'STOP' == info_type):
                    print(f"🔴 SL tespit edildi (trade kaydı): {symbol}")
                    return "LOSS"
    except Exception as e:
        print(f"⚠️ fetch_my_trades ({symbol}): {e}")

    # --- Yöntem 2: Açık emirler ---
    try:
        open_orders = md.fetch_open_orders(symbol)
        order_types = [(o.get('type') or '').upper() for o in open_orders]
        has_sl_open = any('STOP_MARKET' in ot or ot == 'STOP' for ot in order_types)
        has_tp_open = any('TAKE_PROFIT' in ot for ot in order_types)
        if has_sl_open and not has_tp_open:
            print(f"✅ TP tespit edildi (açık emirler): {symbol} — SL bekliyor")
            # SL emrini iptal et
            for o in open_orders:
                if 'STOP' in (o.get('type') or '').upper():
                    md.cancel_order(o['id'], symbol)
            return "WIN"
    except Exception as e:
        print(f"⚠️ fetch_open_orders ({symbol}): {e}")

    # --- Yöntem 3: Bilinmiyor ---
    print(f"❓ TP/SL tespit edilemedi, UNKNOWN: {symbol}")
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# POZİSYON KAPATMA
# ---------------------------------------------------------------------------

def close_position(pos: dict, reason: str = "AI",
                   profit: Optional[bool] = None,
                   send_telegram_fn=None) -> str:
    state = get_state()
    try:
        symbol    = pos['symbol']
        side      = 'SELL' if pos['side'].lower() == 'long' else 'BUY'
        amount    = abs(float(pos['contracts']))
        pnl       = float(pos.get('unrealizedPnl', 0))
        is_profit = pnl >= 0 if profit is None else profit

        if not is_profit:
            state.loss_streaks = min(state.loss_streaks + 1, 3)
            rm.record_coin_loss(symbol, send_telegram_fn)
        else:
            state.loss_streaks = 0

        try:
            md.exchange().create_market_order(symbol=symbol, side=side, amount=amount,
                                              params={'reduceOnly': True})
        except Exception as ex:
            # Bybit: pozisyon zaten kapanmışsa hata yoksay
            if 'position' not in str(ex).lower() and '110025' not in str(ex):
                raise ex
            print(f"ℹ️ {symbol} zaten kapanmış.")

        record_trade_close(symbol, is_profit)

        result_line = f"✅ <b>+{pnl:.2f} USDT</b>" if is_profit else f"❌ <b>{pnl:.2f} USDT</b>"
        header      = "💹 KAPANDI — KÂR" if is_profit else "💸 KAPANDI — ZARAR"
        streak_bar  = "●" * state.loss_streaks + "○" * (3 - state.loss_streaks)
        msg = (
            f"{header}\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"🪙 <b>{symbol.split('/')[0]}</b>\n"
            f"💰 {result_line}\n"
            f"📌 {reason}\n"
            f"🔁 Seri  {streak_bar}  {state.loss_streaks}/3"
        )
        return msg
    except Exception as e:
        err = f"🛑 Kapatma [{pos.get('symbol','?')}]: {e}"
        if send_telegram_fn:
            send_telegram_fn(err)
        return err


# ---------------------------------------------------------------------------
# İŞLEM AÇMA
# ---------------------------------------------------------------------------

def execute_trade(
    signal_data: dict,
    usdt_free: float,
    active_positions: list = None,
    reduce_margin: bool = False,
    btc_direction: str = "NÖTR",
    send_telegram_fn=None,
    trade_type: str = "NORMAL",      # "NORMAL" | "FALLBACK"
    candidate: dict = None,          # strategy_engine'den gelen yapı (opsiyonel)
) -> str:
    """
    signal_data: {"COIN": str, "SIDE": str}
    Tüm filtreler burada; main.py yeniden skor/yorum yapmaz.
    """
    _dbg: list[str] = []
    rejection_reasons: list[str] = []

    def dbg_ok(msg):
        _dbg.append(f"  ✅ {msg}")

    def dbg_fail(msg):
        rejection_reasons.append(msg)
        _dbg.append(f"  🚫 {msg}")
        coin = signal_data.get('COIN', '?')
        side = signal_data.get('SIDE', '?')
        print(f"━━━ TRADE DEBUG: {coin} {side} | BTC:{btc_direction} ━━━")
        for line in _dbg:
            print(line)
        print(f"━━━ SONUÇ: AÇILAMADI → {msg} ━━━")
        # Task 10: structured rejection log
        rm.log_rejection(symbol=coin, reasons=rejection_reasons)

    state = get_state()
    active_positions = active_positions or []

    try:
        # Race condition koruması
        _req_coin = signal_data.get('COIN', '').upper().replace('USDT', '').replace('/','').replace(':','')
        _last_open = state.recently_opened_coins.get(_req_coin, 0)
        if time.time() - _last_open < 5:
            return f"⏱️ RACE-LOCK: {_req_coin} az önce açıldı"

        # Simüle edilmiş bakiye limiti (testnet için)
        if SIMULATED_BALANCE is not None:
            usdt_free = min(usdt_free, SIMULATED_BALANCE)

        if usdt_free < FIXED_MIN_USDT:
            dbg_fail(f"Yetersiz bakiye ({usdt_free:.2f} < {FIXED_MIN_USDT})")
            return f"⚠️ Yetersiz Bakiye ({usdt_free:.2f})."
        dbg_ok(f"Bakiye OK ({usdt_free:.2f} USDT)")

        market_symbol = resolve_market_symbol(signal_data['COIN'])
        if not market_symbol:
            dbg_fail(f"Parite bulunamadı: {signal_data['COIN']}")
            return f"🛑 Parite bulunamadı: {signal_data['COIN']}"
        dbg_ok(f"Parite: {market_symbol}")

        side_req  = signal_data['SIDE'].upper()
        coin_base = market_symbol.split('/')[0]

        if coin_base.upper() in COIN_BLACKLIST:
            dbg_fail(f"BLACKLIST: {coin_base}")
            return f"🚫 BLACKLIST: {coin_base}"
        dbg_ok("Blacklist geçildi")

        # Funding blackout
        fb, fb_msg = rm.is_funding_blackout()
        if fb:
            dbg_fail(f"FUNDING_BLACKOUT: {fb_msg}")
            return fb_msg

        # Funding rate
        fr, fr_msg = rm.check_funding_rate_before_open(market_symbol, side_req)
        if fr:
            dbg_fail(fr_msg)
            return fr_msg
        dbg_ok("Funding OK")

        # Notional
        markets     = md.get_markets()
        market_info = markets.get(market_symbol, {})
        min_notional = (market_info.get('limits', {}).get('cost', {}).get('min', 0)
                        or FIXED_MIN_NOTIONAL)
        entry_check = max(FIXED_MIN_USDT, usdt_free * RISK_PER_TRADE) * LEVERAGE
        if entry_check < min_notional:
            dbg_fail(f"Notional düşük ({entry_check:.1f} < {min_notional:.0f})")
            return f"🛑 Notional {entry_check:.1f} < {min_notional:.0f}."
        dbg_ok(f"Notional OK ({entry_check:.1f})")

        # BTC yön kontrolü
        btc_block = _is_direction_blocked(side_req, btc_direction)
        if btc_block and side_req in ('LONG', 'BUY') and btc_direction == 'GÜÇLÜ AŞAĞI':
            # Bypass koşulu — eşikler kasıtlı sıkı tutuldu (düşük winrate önlemi)
            try:
                rsi_b = _fetch_rsi_quick(market_symbol)
                chg_b = _get_24h_change(market_symbol)
                if (rsi_b is not None and 42 <= rsi_b <= 58        # eski: 65
                        and chg_b is not None and chg_b >= 8.0):   # eski: 4.0
                    reduce_margin = True
                    dbg_ok(f"BTC AŞAĞI bypass OK RSI={rsi_b:.1f} 24s=+{chg_b:.1f}%")
                else:
                    dbg_fail(f"BTC AŞAĞI → LONG YASAK bypass sağlanamadı (RSI={rsi_b}, 24s={chg_b})")
                    return f"🚫 {btc_block}"
            except Exception as e:
                dbg_fail(f"BTC AŞAĞI bypass hatası: {e}")
                return f"🚫 {btc_block}"
        elif btc_block:
            dbg_fail(f"YÖN BLOĞU: {btc_block}")
            return f"🚫 {btc_block}"
        else:
            dbg_ok(f"BTC yön OK ({btc_direction})")

        # Pozisyon kapasitesi
        max_same = (rm.MAX_POSITIONS_SHORT_DIR if side_req in ('SHORT', 'SELL')
                    else rm.MAX_POSITIONS_LONG_DIR)
        same_dir = sum(1 for p in active_positions
                       if (p['side'].lower() == 'long') == (side_req in ('LONG', 'BUY')))
        if same_dir >= max_same:
            dbg_fail(f"Kapasite dolu: {side_req} {same_dir}/{max_same}")
            return f"🚫 AYNI YÖN: {side_req} {same_dir}/{max_same}"
        dbg_ok(f"Kapasite OK ({side_req}: {same_dir}/{max_same})")

        # Daily lock
        locked, lock_reason = rm.is_coin_daily_locked(coin_base)
        if locked:
            dbg_fail(f"Günlük kilit: {lock_reason}")
            return f"🔴 {coin_base} {lock_reason}"
        dbg_ok("Günlük kilit YOK")

        # Cooldown
        cd_locked, cd_reason = rm.is_coin_in_cooldown(coin_base)
        if cd_locked:
            dbg_fail(f"COOLDOWN: {cd_reason}")
            return f"⏳ COOL-DOWN: {coin_base} {cd_reason}"
        dbg_ok("Cooldown YOK")

        # Volatility lock
        vol_locked, vol_reason = rm.is_coin_volatility_locked(coin_base)
        if vol_locked:
            dbg_fail(f"VOL-LOCK: {vol_reason}")
            return f"🌪️ VOL-LOCK: {coin_base} {vol_reason}"
        dbg_ok("Vol-lock YOK")

        # Zıt yön
        for p in active_positions:
            if coin_base.upper() not in p['symbol'].upper():
                continue
            p_is_long = p['side'].lower() == 'long'
            req_is_long = side_req in ('LONG', 'BUY')
            if p_is_long != req_is_long:
                dbg_fail(f"Zıt yön var: {market_symbol}")
                return f"🚫 {market_symbol} zıt yön var."

        # SHORT özel kontroller
        if side_req in ('SHORT', 'SELL'):
            blocked, block_reason = rm.is_momentum_blocked_for_short(
                market_symbol, coin_base, send_telegram_fn)
            if blocked:
                dbg_fail(block_reason)
                return f"🚫 MOMENTUM BLOK: {coin_base}"
            dbg_ok("Momentum OK")

            rsi_check = _fetch_rsi_quick(market_symbol)
            if rsi_check is not None:
                if rsi_check < rm.RSI_OVERSOLD:
                    dbg_fail(f"RSI AŞIRI SATIM SHORT ({rsi_check} < {rm.RSI_OVERSOLD})")
                    return f"🚫 RSI BLOK SHORT: {rsi_check}"
                if rsi_check >= rm.RSI_SHORT_EXTREME_BLOCK:
                    dbg_fail(f"RSI AŞIRI YÜKSEK SHORT ({rsi_check} >= {rm.RSI_SHORT_EXTREME_BLOCK})")
                    return f"🚫 RSI SQUEEZE BLOK: {rsi_check}"
                if rsi_check < rm.RSI_SHORT_ENTRY_MIN:
                    dbg_fail(f"RSI SHORT momentum yok ({rsi_check} < {rm.RSI_SHORT_ENTRY_MIN})")
                    return f"🚫 RSI SHORT BLOK: {rsi_check}"
            dbg_ok(f"RSI SHORT OK ({rsi_check})")

            # 4h RSI
            try:
                from market_data import exchange as _ex
                ohlcv_4h = _ex().fetch_ohlcv(market_symbol, timeframe='4h', limit=15)
                if ohlcv_4h and len(ohlcv_4h) >= 15:
                    from feature_engine import calc_rsi_from_bars
                    rsi_4h = calc_rsi_from_bars(ohlcv_4h)
                    thr_4h = 48 if btc_direction == "GÜÇLÜ AŞAĞI" else 55
                    if rsi_4h and rsi_4h < thr_4h:
                        dbg_fail(f"4h RSI düşük ({rsi_4h} < {thr_4h})")
                        return f"🚫 4h RSI BLOK SHORT: {rsi_4h}"
                    dbg_ok(f"4h RSI OK ({rsi_4h})")
            except Exception as e:
                dbg_ok(f"4h RSI atlandı ({e})")

        # LONG özel kontroller
        if side_req in ('LONG', 'BUY'):

            # ---- BTC HAFİF AŞAĞI özel kuralları (%15 WR veriden geldi) ----
            if btc_direction == "HAFİF AŞAĞI":
                # 1) Skor en az 9/10 olmalı
                cand_score = (candidate.get('score', 0) if candidate else 0)
                if cand_score < 9:
                    dbg_fail(f"BTC HAFİF AŞAĞI → LONG min skor 9 gerekli (skor={cand_score})")
                    return f"🚫 BTC HAFİF AŞAĞI: skor {cand_score}/10 < 9"
                dbg_ok(f"BTC HAFİF AŞAĞI skor OK ({cand_score}/10)")

                # 2) Yüksek volatilite coinler yasak (logdan: BLUR, PIEVERSE, ORDI en çok zarar)
                HIGH_VOL_BLACKLIST_HAFIF_ASAGI = {
                    "BLUR", "PIEVERSE", "ORDI", "HIGH", "BASED", "BOME",
                    "MERL", "MOVE", "DOGS", "SIREN", "GUN", "SOON",
                }
                if coin_base.upper() in HIGH_VOL_BLACKLIST_HAFIF_ASAGI:
                    dbg_fail(f"BTC HAFİF AŞAĞI → {coin_base} yüksek vol listesinde")
                    return f"🚫 BTC HAFİF AŞAĞI: {coin_base} vol listesinde"
                dbg_ok(f"BTC HAFİF AŞAĞI vol OK ({coin_base})")
            # ---------------------------------------------------------------

            # ---- EMA200 ALTINDA hard blok (veri: EMA altı coinler tutarsız, SIREN, ZEC hep kaybetti) ----
            if candidate:
                ema_label = (candidate.get('meta') or {}).get('ema200_label', '')
                if 'ALTINDA' in ema_label:
                    dbg_fail(f"EMA200 ALTINDA → LONG YASAK (yapısal trend bozuk)")
                    return f"🚫 EMA200 ALTINDA: {coin_base}"
                dbg_ok(f"EMA200 OK ({ema_label})")

            # ---- 24s %40+ LONG blok (veri: BOME %58, UAI %49, CHIP %41 hep kaybetti) ----
            chg = _get_24h_change(market_symbol)
            if chg is not None and chg >= 40.0:
                dbg_fail(f"24s AŞIRI KOŞ ({chg:.1f}% >= %40) → LONG YASAK")
                return f"🚫 24s AŞIRI KOŞ: {coin_base} %{chg:.1f}"
            dbg_ok(f"24s aşırı koş OK ({chg:.1f}% < %40)" if chg is not None else "24s OK")
            if chg is not None and chg < 2.0:
                dbg_fail(f"24s momentum düşük ({chg:.1f}% < %2)")
                return f"🚫 MOMENTUM: {coin_base} 24s %{chg:.1f} < %2"
            dbg_ok(f"24s momentum OK ({chg:+.1f}%)")

            rsi_val = _fetch_rsi_quick(market_symbol)
            rsi_limit = (rm.RSI_OVERBOUGHT_BULL if btc_direction == "GÜÇLÜ YUKARI"
                         else rm.RSI_OVERBOUGHT)
            if rsi_val is not None:
                if rsi_val < 40:
                    dbg_fail(f"1h RSI çok düşük ({rsi_val} < 40)")
                    return f"🚫 RSI DÜŞÜK BLOK: {rsi_val}"
                if rsi_val > rm.RSI_LONG_BLOCK_HARD:
                    dbg_fail(f"1h RSI HARD BLOK ({rsi_val} > {rm.RSI_LONG_BLOCK_HARD})")
                    return f"🚫 RSI HARD BLOK: {rsi_val}"
                if rsi_val > rsi_limit:
                    dbg_fail(f"1h RSI eşik aştı ({rsi_val} > {rsi_limit})")
                    return f"🚫 RSI BLOK: {rsi_val}"
            dbg_ok(f"1h RSI OK: {rsi_val}")

            chg_24 = _get_24h_change(market_symbol)
            if chg_24 is not None and abs(chg_24) < 0.5:
                dbg_fail(f"DONUK COİN 24s %{chg_24:.2f} < %0.5")
                return f"⛔ DONUK COİN: {market_symbol}"
        else:
            rsi_val = _fetch_rsi_quick(market_symbol)

        # ---- Volatility ----
        too_vol, vol_reason = rm.is_too_volatile(market_symbol, send_telegram_fn)
        if too_vol:
            dbg_fail(vol_reason)
            return f"🌪️ {vol_reason}: {coin_base}"
        dbg_ok("Volatility OK")

        chg_24h  = _get_24h_change(market_symbol)
        high_vol = chg_24h is not None and abs(chg_24h) >= 30.0

        # ---- ATR tabanlı dinamik SL ----
        sl_ratio = STOP_LOSS_RATIO
        try:
            ohlcv_sl = md.exchange().fetch_ohlcv(market_symbol, timeframe='15m', limit=20)
            if ohlcv_sl and len(ohlcv_sl) >= 14:
                df_sl = pd.DataFrame(ohlcv_sl, columns=['t','o','h','l','c','v'])
                hi = df_sl['h'].astype(float)
                lo = df_sl['l'].astype(float)
                cl = df_sl['c'].astype(float)
                tr = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
                atr_val    = tr.iloc[-14:].mean()
                entry_est  = float(cl.iloc[-1])
                if entry_est > 0:
                    atr_pct  = atr_val / entry_est
                    sl_ratio = max(ATR_SL_MIN, min(ATR_SL_MAX, atr_pct * ATR_SL_MULTIPLIER))
                    print(f"📐 ATR-SL: {coin_base} ATR%={atr_pct*100:.3f} → SL%={sl_ratio*100:.2f}")
        except Exception:
            sl_ratio = STOP_LOSS_HIGH_VOL if high_vol else STOP_LOSS_RATIO

        tp_ratio = max(TARGET_TP_RATIO, sl_ratio * 2.0)

        # ---- Marjin + leverage (Bybit) ----
        min_amount = market_info.get('limits', {}).get('amount', {}).get('min', 0)
        try:
            # Bybit: isolated margin modu
            md.exchange().set_margin_mode('isolated', market_symbol)
        except: pass
        try:
            md.exchange().set_leverage(LEVERAGE, market_symbol)
        except: pass

        actual_leverage = LEVERAGE
        try:
            pi = md.exchange().fetch_positions([market_symbol])
            actual_leverage = float(pi[0]['leverage']) if pi else LEVERAGE
        except: pass

        risk_ratio = RISK_REDUCED_RATIO if reduce_margin else RISK_PER_TRADE
        if high_vol:              risk_ratio *= 0.4
        if side_req in ('SHORT', 'SELL'): risk_ratio = min(risk_ratio, RISK_REDUCED_RATIO)

        entry_usdt = max(FIXED_MIN_USDT, usdt_free * risk_ratio)
        entry_usdt = min(entry_usdt, usdt_free * 0.85)
        if entry_usdt < FIXED_MIN_USDT:
            return f"⚠️ Marjin ({entry_usdt:.2f}) minimum altında."

        ticker      = md.fetch_ticker(market_symbol)
        entry_price = float(ticker['last'])
        mark_price  = float(ticker.get('mark') or ticker.get('last'))

        raw_amount  = (entry_usdt * actual_leverage) / entry_price
        amount_coin = float(md.exchange().amount_to_precision(market_symbol, raw_amount))

        if min_amount and amount_coin < min_amount:
            return f"🛑 Lot ({amount_coin}) < min ({min_amount})."
        if amount_coin <= 0:
            return "🛑 Miktar sıfır."

        side = 'BUY' if side_req in ['LONG', 'BUY'] else 'SELL'
        md.exchange().create_market_order(symbol=market_symbol, side=side, amount=amount_coin)

        trade_context = {
            'trade_type': trade_type,
            'score': candidate.get('score', 0) if candidate else 0,
            'rsi_15m': (candidate.get('features', {}) or {}).get('rsi_15m') if candidate else None,
            'rsi_1h': (candidate.get('features', {}) or {}).get('rsi_1h') if candidate else None,
            'rsi_4h': (candidate.get('features', {}) or {}).get('rsi_4h') if candidate else None,
            'atr_ratio': (candidate.get('features', {}) or {}).get('atr_ratio') if candidate else None,
            'vol_ratio': (candidate.get('features', {}) or {}).get('vol_ratio') if candidate else None,
            'funding_rate': (candidate.get('meta', {}) or {}).get('funding_rate') if candidate else None,
            'lock_reason': '',
            'cooldown_active': False,
        }
        record_trade_open(market_symbol, entry_price, side, context=trade_context)
        rm.record_coin_trade(market_symbol)

        # ---- TP (Bybit) ----
        tp_price = "?"
        try:
            tp_s = 'SELL' if side == 'BUY' else 'BUY'
            tp_m = (1 + tp_ratio + 0.001) if side == 'BUY' else (1 - tp_ratio - 0.001)
            tp_price = float(md.exchange().price_to_precision(market_symbol, mark_price * tp_m))
            md.exchange().create_order(
                symbol=market_symbol, type='limit', side=tp_s,
                amount=amount_coin, price=tp_price,
                params={'reduceOnly': True, 'triggerPrice': tp_price, 'orderType': 'TakeProfit'})
        except Exception as e:
            # Bybit fallback: takeProfit param ile market order
            try:
                tp_s = 'SELL' if side == 'BUY' else 'BUY'
                md.exchange().create_order(
                    symbol=market_symbol, type='market', side=tp_s,
                    amount=amount_coin,
                    params={'reduceOnly': True, 'takeProfit': tp_price})
            except Exception as e2:
                if send_telegram_fn:
                    send_telegram_fn(f"⚠️ TP Hatası ({market_symbol}): {e2}")

        # ---- SL (Bybit) ----
        sl_opened = False
        sl_price  = "?"
        try:
            sl_s = 'SELL' if side == 'BUY' else 'BUY'
            sl_m = (1 - sl_ratio - 0.001) if side == 'BUY' else (1 + sl_ratio + 0.001)
            sl_price = float(md.exchange().price_to_precision(market_symbol, mark_price * sl_m))
            md.exchange().create_order(
                symbol=market_symbol, type='market', side=sl_s,
                amount=amount_coin,
                params={'reduceOnly': True, 'triggerPrice': sl_price, 'orderType': 'StopLoss'})
            sl_opened = True
        except Exception as e:
            # Bybit fallback: stopLoss param
            try:
                sl_s = 'SELL' if side == 'BUY' else 'BUY'
                md.exchange().create_order(
                    symbol=market_symbol, type='market', side=sl_s,
                    amount=amount_coin,
                    params={'reduceOnly': True, 'stopLoss': sl_price})
                sl_opened = True
            except Exception as e2:
                if send_telegram_fn:
                    send_telegram_fn(f"⚠️ SL Hatası ({market_symbol}): {e2}")

        notes = ""
        if reduce_margin:          notes += " [%50 marjin]"
        if high_vol:               notes += f" [vol SL={sl_ratio*100:.0f}%]"
        if trade_type == "FALLBACK": notes += " [FALLBACK]"
        if side_req in ('SHORT', 'SELL'): notes += " [SHORT]"

        side_arrow = "▲ LONG" if side == 'BUY' else "▼ SHORT"
        sl_label   = f"{sl_price}" if sl_opened else "⚠️ AÇILAMADI"
        notes_str  = f"\n<i>{notes.strip()}</i>" if notes.strip() else ""
        atr_note   = f" [ATR-SL=%{sl_ratio*100:.1f}]" if sl_ratio != STOP_LOSS_RATIO else ""
        return (
            f"{'🟢' if side == 'BUY' else '🔴'} <b>İŞLEM AÇILDI</b> — {side_arrow}{notes_str}\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"🪙 <b>{market_symbol.split('/')[0]}</b>  ×{actual_leverage}  |  {entry_usdt:.2f} USDT\n"
            f"🎯 TP  <code>{tp_price}</code>\n"
            f"🛑 SL  <code>{sl_label}</code>{atr_note}\n"
            f"📊 RSI  <b>{rsi_val if rsi_val else 'N/A'}</b>\n"
            f"🏷️ {trade_type}"
        )

    except Exception as e:
        err = f"🛑 <b>SİSTEM HATASI:</b> {str(e)}"
        if send_telegram_fn:
            send_telegram_fn(err)
        return err


# ---------------------------------------------------------------------------
# Yardımcılar (execution_engine içi — API duplikasyonunu önler)
# ---------------------------------------------------------------------------

def _fetch_rsi_quick(market_symbol: str, timeframe: str = '1h', period: int = 14
                     ) -> Optional[float]:
    try:
        ohlcv = md.exchange().fetch_ohlcv(market_symbol, timeframe=timeframe, limit=period+1)
        if not ohlcv or len(ohlcv) < period+1:
            return None
        closes = [float(c[4]) for c in ohlcv]
        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        ag, al = sum(gains)/period, sum(losses)/period
        return 100.0 if al == 0 else round(100-(100/(1+ag/al)), 1)
    except Exception:
        return None


def _get_24h_change(market_symbol: str) -> Optional[float]:
    try:
        t = md.fetch_ticker(market_symbol)
        if not t:
            return None
        pct = t.get('percentage') or t.get('change')
        if pct is not None:
            return float(pct)
        if t.get('open') and t.get('last'):
            op, cl = float(t['open']), float(t['last'])
            return ((cl - op) / op * 100) if op > 0 else None
    except Exception:
        pass
    return None


def _is_direction_blocked(side: str, btc_direction: str) -> Optional[str]:
    if side.upper() in ('LONG', 'BUY') and btc_direction == "GÜÇLÜ AŞAĞI":
        return "BTC GÜÇLÜ AŞAĞI → LONG YASAK"
    if side.upper() in ('SHORT', 'SELL') and btc_direction == "GÜÇLÜ YUKARI":
        return f"BTC {btc_direction} → SHORT YASAK"
    return None