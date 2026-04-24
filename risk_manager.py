"""
risk_manager.py — Tüm risk kontrolleri, kilitleme, cool-down (Task 6 + Task 10)
Her reddedilen trade için structured JSON logu.
"""

import time
from typing import Optional

import market_data as md
from state_manager import get_state, _lock

# ---- Parametreler (main.py ile senkron) ----
COIN_COOLDOWN_PROFIT          = 480
COIN_COOLDOWN_LOSS            = 1200
VOLATILITY_COIN_COOLDOWN      = 1800
MAX_VOLATILITY_PCT            = 0.05   # STAR_COINS muafiyeti kaldırıldı, eşik herkese eşit
VOLATILITY_TIMEFRAME          = '1h'
MAX_DAILY_LOSSES_COIN         = 1       # 2 → 1: ilk SL'den sonra o gün o coin yasak (veri: BASED -2.19 USDT)
MAX_DAILY_TRADES_COIN         = 5
MAX_DAILY_TRADES_STAR_COIN    = 10
MAX_CONSEC_LOSS_COIN          = 2
STAR_COINS                    = {"SIREN", "BASED", "PIPPIN", "ENA", "SOON"}
DAILY_LOSS_LIMIT_PCT          = 0.11
PAUSE_DURATION                = 3600
PAUSE_ON_3_LOSSES_SEC         = 3600
MOMENTUM_SHORT_BLOCK          = 45.0
RSI_OVERSOLD                  = 25
RSI_SHORT_ENTRY_MIN           = 65
RSI_SHORT_EXTREME_BLOCK       = 90.0
RSI_LONG_BLOCK_HARD           = 80     # 72 → 80: veri kanıtı — 65-72 arası kazananlar var (GTC=67,PROM=72)
RSI_LONG_IDEAL_MAX            = 68     # ideal giriş bölgesi üstü
RSI_OVERBOUGHT_BULL           = 76     # BTC GÜÇLÜ YUKARI'da üst eşik
RSI_OVERBOUGHT                = 72     # normal üst eşik (soft blok)
TRADING_BLACKOUT_HOURS        = {7, 8}
FUNDING_BLACKOUT_MINUTES      = 15
FUNDING_FEE_RATE_MAX          = 0.0010
FUNDING_HOURS_UTC3            = {3, 11, 19}
MIN_MOMENTUM_PCT_LONG         = 2.0
FIXED_MIN_USDT                = 7
MAX_POSITIONS                 = 4
MAX_POSITIONS_LONG_DIR        = 2
MAX_POSITIONS_SHORT_DIR       = 2


# ---------------------------------------------------------------------------
# STRUCTURED REJECTION LOG (Task 10)
# ---------------------------------------------------------------------------

def log_rejection(symbol: str, reasons: list[str]):
    """Her reddedilen trade için okunabilir log."""
    print({
        "symbol":   symbol,
        "rejected": True,
        "reason":   reasons,
    })


# ---------------------------------------------------------------------------
# COOL-DOWN
# ---------------------------------------------------------------------------

def is_coin_in_cooldown(coin_base: str) -> tuple[bool, str]:
    """(kilitli_mi, sebep)"""
    state = get_state()
    last     = state.coin_last_closed.get(coin_base)
    if last is None:
        return False, ""
    elapsed  = time.time() - last
    was_prof = state.coin_closed_profit.get(coin_base, False)
    cooldown = COIN_COOLDOWN_PROFIT if was_prof else COIN_COOLDOWN_LOSS
    if elapsed < cooldown:
        reason = f"COOLDOWN ({'kâr' if was_prof else 'zarar'}) {int((cooldown-elapsed)/60)}dk kaldı"
        print(f"⏳ COOL-DOWN: {coin_base} {reason}")
        return True, reason
    return False, ""


def mark_coin_closed(symbol: str, profit: bool, send_telegram_fn=None):
    state = get_state()
    with _lock:
        cb = symbol.split('/')[0].strip()
        state.coin_last_closed[cb]   = time.time()
        state.coin_closed_profit[cb] = profit
        cd = COIN_COOLDOWN_PROFIT if profit else COIN_COOLDOWN_LOSS
        print(f"🔒 Cool-down: {cb} ({cd//60}dk) [{'kâr' if profit else 'zarar'}]")

        if profit:
            state.coin_consec_losses[cb] = 0
        else:
            state.coin_consec_losses[cb] = state.coin_consec_losses.get(cb, 0) + 1
            consec = state.coin_consec_losses[cb]
            if consec >= MAX_CONSEC_LOSS_COIN:
                print(f"🚫 ART ARDA KAYIP: {cb} {consec}x SL → seans kilidi")
                state.daily_coin_losses[cb] = max(
                    state.daily_coin_losses.get(cb, 0), MAX_DAILY_LOSSES_COIN
                )
                if cb not in state.daily_coin_lock_timestamps:
                    state.daily_coin_lock_timestamps[cb] = time.time()
                if send_telegram_fn:
                    send_telegram_fn(
                        f"🚫 <b>ART ARDA KAYIP: {cb}</b>\n"
                        f"📛 {consec} üst üste SL — seans kilidi aktif"
                    )


# ---------------------------------------------------------------------------
# VOLATILITY LOCK
# ---------------------------------------------------------------------------

def is_coin_volatility_locked(coin_base: str) -> tuple[bool, str]:
    state = get_state()
    lock_start = state.coin_locks.get(coin_base)
    if lock_start is None:
        return False, ""
    elapsed = time.time() - lock_start
    if elapsed < VOLATILITY_COIN_COOLDOWN:
        reason = f"VOL-LOCK {int((VOLATILITY_COIN_COOLDOWN-elapsed)/60)}dk kaldı"
        print(f"🌪️ VOL-LOCK: {coin_base} {reason}")
        return True, reason
    with _lock:
        state.coin_locks.pop(coin_base, None)
    return False, ""


def mark_coin_volatility_locked(coin_base: str, send_telegram_fn=None):
    state = get_state()
    with _lock:
        state.coin_locks[coin_base] = time.time()
    print(f"🌪️ VOL-LOCK: {coin_base} {VOLATILITY_COIN_COOLDOWN//60}dk kilitlendi")
    if send_telegram_fn:
        send_telegram_fn(f"⚡ {coin_base} VOL-LOCK {VOLATILITY_COIN_COOLDOWN//60}dk")


def is_too_volatile(market_symbol: str, send_telegram_fn=None) -> tuple[bool, str]:
    coin_base = market_symbol.split('/')[0]
    locked, reason = is_coin_volatility_locked(coin_base)
    if locked:
        return True, reason
    try:
        ohlcv = md.exchange().fetch_ohlcv(market_symbol, timeframe=VOLATILITY_TIMEFRAME, limit=2)
        if not ohlcv or len(ohlcv) < 2:
            return False, ""
        op, cl = float(ohlcv[-2][1]), float(ohlcv[-2][4])
        if op <= 0:
            return False, ""
        v = abs(cl - op) / op
        # STAR_COINS artık volatilite muafiyeti almıyor — scalp stratejisiyle uyumsuz
        effective_limit = MAX_VOLATILITY_PCT
        if v >= effective_limit:
            last_lock = get_state().coin_locks.get(coin_base, 0)
            already_locked_recently = (time.time() - last_lock) < 3600
            reason = f"VOL-LOCK %{v*100:.1f} dalgalanma"
            if not already_locked_recently:
                mark_coin_volatility_locked(coin_base, send_telegram_fn)
            else:
                with _lock:
                    get_state().coin_locks[coin_base] = time.time()
            return True, reason
        return False, ""
    except Exception:
        return False, ""


# ---------------------------------------------------------------------------
# DAILY LOCK
# ---------------------------------------------------------------------------

def reset_daily_coin_data_if_needed():
    state = get_state()
    today = time.strftime("%Y-%m-%d")
    if state.daily_coin_lock_date != today:
        with _lock:
            state.daily_coin_losses    = {}
            state.daily_coin_trades    = {}
            state.coin_consec_losses   = {}
            state.daily_coin_lock_date = today


def is_coin_daily_locked(coin_base: str) -> tuple[bool, str]:
    reset_daily_coin_data_if_needed()
    state  = get_state()
    losses = state.daily_coin_losses.get(coin_base, 0)
    trades = state.daily_coin_trades.get(coin_base, 0)

    if losses >= MAX_DAILY_LOSSES_COIN:
        lock_start = state.daily_coin_lock_timestamps.get(coin_base)
        if lock_start and (time.time() - lock_start) >= 86400:
            with _lock:
                state.daily_coin_losses[coin_base] = 0
                state.daily_coin_lock_timestamps.pop(coin_base, None)
            print(f"🔓 {coin_base} 24s kilidi açıldı")
            return False, ""
        reason = f"DAILY-LOCK {losses} zarar (max {MAX_DAILY_LOSSES_COIN})"
        print(f"🔴 KAYIP KİLİT: {coin_base} — {reason}")
        return True, reason

    limit = MAX_DAILY_TRADES_STAR_COIN if coin_base in STAR_COINS else MAX_DAILY_TRADES_COIN
    if trades >= limit:
        reason = f"DAILY-TRADE-LOCK {trades}/{limit} işlem"
        print(f"🔴 İŞLEM KİLİT: {coin_base} — {reason}")
        return True, reason
    return False, ""


def record_coin_loss(symbol: str, send_telegram_fn=None):
    reset_daily_coin_data_if_needed()
    state = get_state()
    with _lock:
        cb = symbol.split('/')[0].strip()
        state.daily_coin_losses[cb] = state.daily_coin_losses.get(cb, 0) + 1
        losses = state.daily_coin_losses[cb]
        if losses >= MAX_DAILY_LOSSES_COIN:
            if cb not in state.daily_coin_lock_timestamps:
                state.daily_coin_lock_timestamps[cb] = time.time()
            if send_telegram_fn:
                send_telegram_fn(
                    f"🔒 <b>{cb} KİLİTLENDİ</b>\n"
                    f"📛 {losses} zarar · 24 saat"
                )


def record_coin_trade(symbol: str):
    reset_daily_coin_data_if_needed()
    state = get_state()
    with _lock:
        cb = symbol.split('/')[0].strip()
        state.daily_coin_trades[cb] = state.daily_coin_trades.get(cb, 0) + 1


# ---------------------------------------------------------------------------
# MOMENTUM BLOCK (SHORT)
# ---------------------------------------------------------------------------

def is_momentum_blocked_for_short(market_symbol: str, coin_base: str,
                                   send_telegram_fn=None) -> tuple[bool, str]:
    try:
        t   = md.fetch_ticker(market_symbol)
        chg = float(t.get('percentage', 0) or 0) if t else None
        if chg is not None and chg >= MOMENTUM_SHORT_BLOCK:
            reason = f"MOMENTUM_BLOCK 24s +%{chg:.1f} > %{MOMENTUM_SHORT_BLOCK}"
            if send_telegram_fn:
                send_telegram_fn(f"🚫 MOMENTUM BLOK: {coin_base} 24s +%{chg:.1f}")
            return True, reason
    except Exception:
        pass
    return False, ""


# ---------------------------------------------------------------------------
# FUNDING BLACKOUT
# ---------------------------------------------------------------------------

def is_funding_blackout() -> tuple[bool, str]:
    import datetime
    now_utc3 = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    h, m = now_utc3.hour, now_utc3.minute
    for fh in FUNDING_HOURS_UTC3:
        if h == (fh - 1) % 24 and m >= (60 - FUNDING_BLACKOUT_MINUTES):
            return True, f"FUNDING_BLACKOUT {fh:02d}:00'a {60-m}dk kaldı"
        if h == fh and m < FUNDING_BLACKOUT_MINUTES:
            return True, f"FUNDING_BLACKOUT {fh:02d}:00 geçti {m}dk"
    return False, ""


def check_funding_rate_before_open(market_symbol: str, side: str) -> tuple[bool, str]:
    try:
        funding = md.exchange().fetch_funding_rate(market_symbol)
        rate    = float(funding.get('fundingRate', 0) or 0)
        if abs(rate) < FUNDING_FEE_RATE_MAX:
            return False, ""
        is_long = side.upper() in ('LONG', 'BUY')
        if is_long and rate > FUNDING_FEE_RATE_MAX:
            return True, f"FUNDING_RATE_BLOCK rate=%{rate*100:.3f} → LONG ödeyecek"
        if not is_long and rate < -FUNDING_FEE_RATE_MAX:
            return True, f"FUNDING_RATE_BLOCK rate=%{rate*100:.3f} → SHORT ödeyecek"
    except Exception:
        pass
    return False, ""


# ---------------------------------------------------------------------------
# DAILY BALANCE LOCK
# ---------------------------------------------------------------------------

def check_daily_loss(net_balance: float, send_telegram_fn=None) -> bool:
    import json, os, pathlib
    DAILY_STATE_FILE = str(pathlib.Path(__file__).parent / "daily_state.json")
    state = get_state()

    def _load():
        try:
            if not os.path.isfile(DAILY_STATE_FILE): return None
            with open(DAILY_STATE_FILE) as f: s = json.load(f)
            return s if s.get("date") == time.strftime("%Y-%m-%d") else None
        except: return None

    def _save(b, d, p, pu):
        try:
            with open(DAILY_STATE_FILE, "w") as f:
                json.dump({"date": d, "start_balance": b, "paused": p, "pause_until": pu}, f)
        except: pass

    today = time.strftime("%Y-%m-%d")
    saved = _load()
    if saved:
        daily_sb   = saved["start_balance"]
        state.bot_paused  = saved.get("paused", False)
        state.pause_until = saved.get("pause_until", 0)
    else:
        daily_sb = net_balance
        state.bot_paused  = False
        state.pause_until = 0
        _save(net_balance, today, False, 0)

    if state.bot_paused:
        if time.time() < state.pause_until:
            return True
        else:
            state.bot_paused = False
            _save(daily_sb, today, False, 0)
            if send_telegram_fn:
                send_telegram_fn("▶️ <b>BOT DEVAM EDİYOR</b>")

    if daily_sb and daily_sb > 0:
        loss_pct = (daily_sb - net_balance) / daily_sb
        if loss_pct >= DAILY_LOSS_LIMIT_PCT:
            state.bot_paused  = True
            state.pause_until = time.time() + PAUSE_DURATION
            _save(daily_sb, today, True, state.pause_until)
            msg = (
                f"🚨 <b>GÜNLÜK LİMİT AŞILDI</b>\n"
                f"📉 Zarar %{loss_pct*100:.1f} (limit %{DAILY_LOSS_LIMIT_PCT*100:.0f})\n"
                f"⏸️ {int(PAUSE_DURATION/60)} dk mola"
            )
            print(msg)
            if send_telegram_fn:
                send_telegram_fn(msg)
            return True
    return False
