"""
main.py — Bot orkestratörü (v18 — tam refactor)

Sorumluluklar:
  ✅ Risk kontrolleri (bakiye, pozisyon limiti, marjin)
  ✅ Emir icrası (execution_engine)
  ✅ Ana döngü + watchdog + micro tracking
  ✅ Telegram raporlama

YASAKLI:
  ❌ Skor yeniden hesaplama
  ❌ Sinyal yorumlama
  ❌ Gizli fallback override

Strateji kararları → strategy_engine.py
Risk kontrolleri   → risk_manager.py
Emir icraatı       → execution_engine.py
Trade logu         → trade_logger.py
Durum              → state_manager.py
"""

import os
import sys
import re
import time
import json
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from google import genai
from dotenv import load_dotenv

# ---- Modüller ----
import market_data as md
import strategy_engine as se
import risk_manager as rm
import execution_engine as ee
import trade_logger as tl
from state_manager import get_state, update_heartbeat, record_trade_close

# FIXED_MIN_USDT tek kaynak: risk_manager.py
from risk_manager import FIXED_MIN_USDT

load_dotenv()

# ---------------------------------------------------------------------------
# BAĞLANTI
# ---------------------------------------------------------------------------
try:
    current_ip = requests.get('https://api.ipify.org', timeout=10).text
    print(f"\n🌐 =========================================")
    print(f"🚀 BYBIT'E BU IP ILE GIDIYORUM: {current_ip}")
    print(f"🌐 =========================================\n")
except Exception as e:
    print(f"⚠️ IP adresi alinamadi: {e}")

try:
    from news_collector import fetch_latest_news
except ImportError:
    def fetch_latest_news(): return ""

# ---------------------------------------------------------------------------
# PARAMETRELER
# ---------------------------------------------------------------------------
LEVERAGE               = 5
MAX_POSITIONS          = 4
MAX_POSITIONS_LONG_DIR = 2
MAX_POSITIONS_SHORT_DIR= 2
TARGET_TP_RATIO        = 0.020
STOP_LOSS_RATIO        = 0.010
TRAILING_BE_BUFFER        = 0.010
TRAILING_STOP_TRIGGER_PCT = 0.030  # %2 → %3: daha az tetiklensin (veri: trailing suçsuz, ama BE zararlı)
TRAILING_ACTIVE           = True
TRAILING_BE_ENABLED       = False  # BE mekanizması kapalı: fiyat geri dönünce büyük zararla çıkıyordu
CHECK_INTERVAL         = 45
MICRO_CHECK_INTERVAL   = 10
DATA_FETCH_TIMEOUT     = 120
WATCHDOG_TIMEOUT       = 300
RESTART_ON_FREEZE_SEC  = 600
DAILY_LOSS_LIMIT_PCT   = 0.11
MIN_PNL_TO_PROTECT     = 0.10
MIN_PNL_TO_CLOSE       = -0.10
MICRO_EXIT_RSI_LONG    = 38
MICRO_EXIT_PRICE_DROP  = 0.003
MICRO_EXIT_MIN_AGE_SEC = 90
MAX_HOLD_PROFIT_SEC    = 5400
MAX_HOLD_LOSS_SEC      = 480     # 15dk → 8dk: 3dk+ zararlı tradelerin %80'i kaybediyor
MIN_MOMENTUM_EXIT_AGE_SEC = 1200
MIN_SCORE_THRESHOLD    = 8
MIN_SCORE_SHORT        = 8
TRADING_BLACKOUT_HOURS = {7, 8}
FUNDING_HOURS_UTC3     = {3, 11, 19}
STAR_COINS             = {"SIREN", "BASED", "PIPPIN", "ENA", "SOON"}

API_KEY        = os.getenv("GEMINI_API_KEY")
TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")

client = genai.Client(api_key=API_KEY)

# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message,
                                 "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram: {e}")

# ---------------------------------------------------------------------------
# GÜVENLİ VERİ ÇEKME
# ---------------------------------------------------------------------------

_data_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="data_fetch")
_state = get_state()


def fetch_market_data_safe(recently_closed_coins: set = None) -> dict:
    """Timeout korumalı piyasa verisi çekme. Hata/timeout → cached dict."""
    if recently_closed_coins is None:
        recently_closed_coins = set()

    def _do_fetch():
        return se.fetch_binance_data(recently_closed_coins)

    future = _data_executor.submit(_do_fetch)
    try:
        result = future.result(timeout=DATA_FETCH_TIMEOUT)
        _state.last_successful_market_data = result
        _state.last_data_fetch_time = time.time()
        return result
    except FuturesTimeoutError:
        age = int(time.time() - _state.last_data_fetch_time)
        warn = (f"⚠️ <b>VERİ ÇEKME TIMEOUT</b>\n"
                f"{DATA_FETCH_TIMEOUT}s içinde tamamlanmadı.\n"
                f"Son başarılı veri: {age}s önce — cache kullanılıyor.")
        print(warn)
        send_telegram(warn)
        future.cancel()
        cached = _state.last_successful_market_data
        return cached if cached else {"btc_direction": "NÖTR", "candidates": [], "error": "timeout"}
    except Exception as e:
        print(f"⚠️ fetch_market_data_safe hatası: {e}")
        cached = _state.last_successful_market_data
        return cached if cached else {"btc_direction": "NÖTR", "candidates": [], "error": str(e)}

# ---------------------------------------------------------------------------
# WATCHDOG
# ---------------------------------------------------------------------------

_watchdog_running  = True
_micro_loop_running = True


def watchdog_loop():
    global _watchdog_running
    print(f"🐕 Watchdog başladı ({WATCHDOG_TIMEOUT}s eşik)")
    time.sleep(WATCHDOG_TIMEOUT)
    while _watchdog_running:
        try:
            elapsed = time.time() - _state.last_heartbeat_time
            if elapsed > WATCHDOG_TIMEOUT:
                msg = (f"🚨 <b>DONMA TESPİT EDİLDİ</b>\n"
                       f"⏱️ {int(elapsed//60)}dk {int(elapsed%60)}s yanıt yok")
                send_telegram(msg)
                time.sleep(WATCHDOG_TIMEOUT)
                if time.time() - _state.last_heartbeat_time > RESTART_ON_FREEZE_SEC:
                    send_telegram("🔄 <b>WATCHDOG: YENİDEN BAŞLATILIYOR</b>")
                    time.sleep(3)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                time.sleep(30)
        except Exception as e:
            print(f"⚠️ Watchdog hatası: {e}")
            time.sleep(30)

# ---------------------------------------------------------------------------
# WALLET / POSITIONS
# ---------------------------------------------------------------------------

def get_wallet_status(active_positions: list = None) -> dict:
    try:
        balance   = md.exchange().fetch_balance()
        usdt      = balance.get('USDT', {})
        usdt_free = float(usdt.get('free', 0))
        usdt_tot  = float(usdt.get('total', usdt_free))
        total_unr = sum(float(p.get('unrealizedPnl', 0)) for p in (active_positions or []))
        net       = usdt_tot + total_unr
        return {"free": usdt_free, "net": net, "unrealized": total_unr}
    except Exception as e:
        send_telegram(f"🛑 <b>BAKİYE HATASI</b>\n{e}")
        return {"free": 0, "net": 0, "unrealized": 0}

# ---------------------------------------------------------------------------
# TP / SL SYNC
# ---------------------------------------------------------------------------

def sync_trade_times(active_positions: list):
    """
    Kapanan pozisyonları tespit et, log yaz, cool-down başlat.
    TP/SL tespiti → execution_engine.detect_trade_outcome() (Task 5).
    """
    active_symbols = {p['symbol'] for p in active_positions}

    for sym in list(_state.trade_times.keys()):
        if sym in active_symbols:
            continue

        opened_at = _state.trade_times.get(sym, 0)
        duration  = int(time.time() - opened_at) if opened_at else 0

        # Task 5: SADECE fetch_my_trades + open_orders; fiyat kıyaslaması YOK
        result = ee.detect_trade_outcome(sym)

        result_emoji  = "✅ TP" if result == "WIN" else ("🔴 SL" if result == "LOSS" else "❓ UNKNOWN")
        result_header = {"WIN": "💹 KAPANDI — KÂR", "LOSS": "💸 KAPANDI — ZARAR",
                         "UNKNOWN": "❓ KAPANDI — SONUÇ BİLİNMİYOR"}[result]

        send_telegram(
            f"{result_header}\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"🪙 <b>{sym.split('/')[0]}</b>  ·  {result_emoji}\n"
            f"⏱️ Süre  ~{duration//60} dk\n"
            f"<i>Exchange TP/SL ile kapattı</i>"
        )
        print(f"🔔 TP/SL: {sym} (~{duration//60}dk) [{result_emoji}]")

        entry_p = _state.trade_entry_prices.get(sym, 0)
        side_s  = "LONG" if _state.trade_sides.get(sym) == "BUY" else "SHORT"

        # Çıkış fiyatı — sadece log için
        exit_p = 0
        try:
            ticker = md.fetch_ticker(sym)
            exit_p = float(ticker.get('last', 0)) if ticker else 0
        except Exception:
            pass

        raw_pnl = ((exit_p - entry_p) if side_s == "LONG" else (entry_p - exit_p))

        # Aday verilerini state'den al (varsa)
        candidate_data = _state.last_successful_market_data.get("candidates", [])
        sym_base = sym.split('/')[0].upper()
        cand = next((c for c in candidate_data if str(c.get("base", "")).upper() == sym_base), None)
        ctx = _state.trade_contexts.get(sym, {})

        tl.write_trade_log(
            symbol=sym, side=side_s,
            entry_price=entry_p, exit_price=exit_p,
            pnl=raw_pnl,
            result=result,   # WIN | LOSS | UNKNOWN
            btc_direction=_state.last_btc_direction,
            duration_sec=duration,
            score=ctx.get('score', cand['score'] if cand else 0),
            rsi_15m=ctx.get('rsi_15m', cand['features'].get('rsi_15m') if cand else None),
            rsi_1h=ctx.get('rsi_1h', cand['features'].get('rsi_1h') if cand else None),
            rsi_4h=ctx.get('rsi_4h', cand['features'].get('rsi_4h') if cand else None),
            atr_ratio=ctx.get('atr_ratio', cand['features'].get('atr_ratio') if cand else None),
            vol_ratio=ctx.get('vol_ratio', cand['features'].get('vol_ratio') if cand else None),
            funding_rate=ctx.get('funding_rate', cand['meta'].get('funding_rate') if cand else None),
            trade_type=ctx.get('trade_type', 'NORMAL'),
            lock_reason=ctx.get('lock_reason', ''),
            cooldown_active=ctx.get('cooldown_active', False),
            timestamp_entry=time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(opened_at)) if opened_at else None,
        )

        is_profit = (result == "WIN")
        record_trade_close(sym, is_profit)
        rm.mark_coin_closed(sym, profit=is_profit, send_telegram_fn=send_telegram)

# ---------------------------------------------------------------------------
# TRAILING STOP
# ---------------------------------------------------------------------------

def apply_trailing_stops(active_positions: list):
    if not TRAILING_ACTIVE:
        return
    for pos in active_positions:
        sym  = pos['symbol']
        pnl  = float(pos.get('unrealizedPnl', 0))
        if pnl <= 0:
            continue
        entry_p  = _state.trade_entry_prices.get(sym)
        side_rec = _state.trade_sides.get(sym)
        if entry_p is None or side_rec is None:
            continue
        contracts = abs(float(pos.get('contracts', 0)))
        notional  = contracts * entry_p
        margin    = notional / LEVERAGE
        trigger_pnl = margin * TRAILING_STOP_TRIGGER_PCT
        if pnl < trigger_pnl:
            continue

        # BE mekanizması kapalı (TRAILING_BE_ENABLED=False)
        # Veri analizi: BE SL fiyat geri döndüğünde büyük zararla çıkartıyordu
        # Exchange'in kendi TP emri yeterli — burada sadece log
        if not TRAILING_BE_ENABLED:
            be_flag = f"be_log_{sym}"
            if not _state.coin_closed_profit.get(be_flag):
                _state.coin_closed_profit[be_flag] = True
                print(f"📈 TRAILING eşiği geçildi: {sym} PnL={pnl:+.2f}$ — BE kapalı, TP bekliyor")
            continue

        be_flag = f"be_{sym}"
        if _state.coin_closed_profit.get(be_flag):
            continue
        try:
            ticker        = md.fetch_ticker(sym)
            current_price = float(ticker['last'])
            is_long = (side_rec == 'BUY')
            if is_long and current_price <= entry_p:
                continue
            if not is_long and current_price >= entry_p:
                continue

            open_orders = md.exchange().fetch_open_orders(sym)
            for o in open_orders:
                if (o.get('type') or '').upper() in ('STOP_MARKET', 'STOP'):
                    try: md.exchange().cancel_order(o['id'], sym)
                    except: pass
                    break

            be_side  = 'SELL' if side_rec == 'BUY' else 'BUY'
            be_price = entry_p * (1 + TRAILING_BE_BUFFER if is_long else 1 - TRAILING_BE_BUFFER)
            be_price = float(md.exchange().price_to_precision(sym, be_price))
            md.exchange().create_order(
                symbol=sym, type='STOP_MARKET', side=be_side,
                amount=contracts, params={'stopPrice': be_price, 'reduceOnly': True}
            )
            _state.coin_closed_profit[be_flag] = True
            send_telegram(
                f"🔒 <b>BREAKEVEN KURULDU</b>\n"
                f"🪙 <b>{sym.split('/')[0]}</b>  ·  +{pnl:.2f}$\n"
                f"🛑 Yeni SL  <code>{be_price:.4f}</code>  (+%{TRAILING_BE_BUFFER*100:.1f})"
            )
        except Exception as e:
            if '-2021' in str(e):
                try:
                    cs = 'SELL' if side_rec == 'BUY' else 'BUY'
                    md.exchange().create_market_order(symbol=sym, side=cs, amount=contracts,
                                                     params={'reduceOnly': True})
                    record_trade_close(sym, pnl > 0)
                    send_telegram(f"⚡ <b>TRAILING KORUMA ÇIKIŞI</b>\n🪙 {sym.split('/')[0]}  ·  {pnl:+.2f}$")
                except Exception as ce:
                    print(f"🛑 Trailing kapama hatası ({sym}): {ce}")

# ---------------------------------------------------------------------------
# MOMENTUM EXIT + MICRO EXIT + TIMEOUT
# ---------------------------------------------------------------------------

def _fetch_rsi_15m(market_symbol: str) -> float | None:
    try:
        ohlcv = md.exchange().fetch_ohlcv(market_symbol, timeframe='15m', limit=16)
        if not ohlcv or len(ohlcv) < 15: return None
        closes = [float(c[4]) for c in ohlcv]
        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        ag, al = sum(gains)/14, sum(losses)/14
        return 100.0 if al == 0 else round(100-(100/(1+ag/al)), 1)
    except: return None


def _fetch_rsi_1h(market_symbol: str) -> float | None:
    try:
        ohlcv = md.exchange().fetch_ohlcv(market_symbol, timeframe='1h', limit=16)
        if not ohlcv or len(ohlcv) < 15: return None
        closes = [float(c[4]) for c in ohlcv]
        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        ag, al = sum(gains)/14, sum(losses)/14
        return 100.0 if al == 0 else round(100-(100/(1+ag/al)), 1)
    except: return None


def check_momentum_exit(active_positions: list) -> list:
    closed = []
    now    = time.time()
    for pos in active_positions:
        sym    = pos['symbol']
        opened = _state.trade_times.get(sym)
        if opened is None: continue
        if now - opened < MIN_MOMENTUM_EXIT_AGE_SEC: continue
        pnl = float(pos.get('unrealizedPnl', 0))
        if pnl >= 0: continue
        rsi = _fetch_rsi_1h(sym)
        if rsi is None: continue
        is_long = pos['side'].lower() == 'long'
        if (is_long and rsi < 40) or (not is_long and rsi > 60):
            msg = (f"⚡ <b>İVME KAYBI — ÇIKIŞ</b>\n"
                   f"🪙 <b>{sym.split('/')[0]}</b>\n"
                   f"📊 RSI {rsi:.1f} · {pnl:.2f}$")
            ee.close_position(pos, f"İvme kaybı (RSI={rsi:.1f})", profit=False,
                              send_telegram_fn=send_telegram)
            send_telegram(msg)
            closed.append(sym)
    return closed


def check_micro_exit(active_positions: list) -> list:
    closed = []
    now    = time.time()
    for pos in active_positions:
        sym    = pos['symbol']
        opened = _state.trade_times.get(sym)
        if opened is None: continue
        if now - opened < MICRO_EXIT_MIN_AGE_SEC: continue
        pnl = float(pos.get('unrealizedPnl', 0))
        if pnl >= 0: continue
        entry_p = _state.trade_entry_prices.get(sym)
        if entry_p is None: continue
        try:
            ticker = md.fetch_ticker(sym)
            curr_p = float(ticker['last'])
        except: continue
        is_long = pos['side'].lower() == 'long'
        drop_pct = ((entry_p - curr_p) / entry_p if is_long
                    else (curr_p - entry_p) / entry_p)
        if drop_pct < MICRO_EXIT_PRICE_DROP: continue
        rsi_15m = _fetch_rsi_15m(sym)
        if rsi_15m is None: continue
        trigger = (is_long and rsi_15m < MICRO_EXIT_RSI_LONG) or \
                  (not is_long and rsi_15m > (100 - MICRO_EXIT_RSI_LONG))
        if trigger:
            ee.close_position(pos, f"Mikro çıkış 15m RSI={rsi_15m:.1f}", profit=False,
                              send_telegram_fn=send_telegram)
            send_telegram(
                f"🚨 <b>MİKRO ÇIKIŞ</b>\n{sym.split('/')[0]}\n"
                f"15m RSI={rsi_15m:.1f} · {pnl:+.2f}$")
            closed.append(sym)
    return closed


def check_timed_out_positions(active_positions: list) -> list:
    closed = []
    now    = time.time()
    for pos in active_positions:
        sym    = pos['symbol']
        opened = _state.trade_times.get(sym)
        if opened is None: continue
        elapsed  = now - opened
        pnl      = float(pos.get('unrealizedPnl', 0))
        max_hold = MAX_HOLD_PROFIT_SEC if pnl > 0 else MAX_HOLD_LOSS_SEC
        if elapsed > max_hold:
            ee.close_position(pos, f"Zaman limiti ({int(elapsed/60)}dk)",
                             profit=(pnl >= 0), send_telegram_fn=send_telegram)
            send_telegram(
                f"⏰ <b>ZAMAN LİMİTİ</b>\n"
                f"🪙 {sym.split('/')[0]}  ·  {int(elapsed/60)}dk  ·  {pnl:+.2f}$")
            closed.append(sym)
    return closed

# ---------------------------------------------------------------------------
# MICRO TRACKING THREAD
# ---------------------------------------------------------------------------

def micro_tracking_loop():
    global _micro_loop_running
    print("🔬 Mikro-takip döngüsü başladı (10s)")
    while _micro_loop_running:
        try:
            active = md.fetch_positions()
            if not active:
                time.sleep(MICRO_CHECK_INTERVAL)
                continue
            apply_trailing_stops(active)
            check_micro_exit(active)
            check_timed_out_positions(active)
        except Exception as e:
            print(f"⚠️ Mikro döngü hatası: {e}")
        time.sleep(MICRO_CHECK_INTERVAL)

# ---------------------------------------------------------------------------
# AI KARAR MOTORU
# ---------------------------------------------------------------------------

def get_ai_decision(news: str, candidates: list, pos_desc: str,
                    active_positions: list, cooldown_coins: list,
                    locked_coins: list, btc_direction: str) -> str:
    """
    AI'ya structured candidates göndeririz; metin rapor değil.
    main.py skoru YORUMLAMAZ — sadece AI'ın çıktısını parse eder.
    """
    long_count  = sum(1 for p in active_positions if p['side'].lower() == 'long')
    short_count = len(active_positions) - long_count
    cooldown_str = ",".join(cooldown_coins) if cooldown_coins else "None"
    locked_str   = ",".join(locked_coins)   if locked_coins   else "None"
    rsi_long_lim = 76 if btc_direction == "GÜÇLÜ YUKARI" else 72

    loss_note = ""
    if _state.loss_streaks >= 3:
        loss_note = f"LOSS_STREAK:{_state.loss_streaks}. BTC yönüyle aynı yönde aç."

    # Candidates'i kısa metin formatına çevir (AI için)
    cand_lines = []
    for c in candidates[:14]:   # max 14 aday
        f = c['features']
        m = c['meta']
        side_tag = "🟢LONG" if c['side'] == "LONG" else "🔴SHORT"
        cand_lines.append(
            f"{side_tag} {c['base']} SKOR:{c['score']}/10 "
            f"1hRSI:{f.get('rsi_1h') or 'N/A'} "
            f"4hRSI:{f.get('rsi_4h') or 'N/A'} "
            f"15mRSI:{f.get('rsi_15m') or 'N/A'} "
            f"24s:{m['change_24h']:+.1f}% "
            f"EMA200:{m['ema200_label']} "
            f"MACD:{f.get('macd_hist') or 0:.5f} "
            f"Vol:{f.get('vol_ratio') or 0:.1f}x "
            f"ATR:{f.get('atr_ratio') or 0:.3f} "
            f"Funding:{m.get('funding_rate') or 'N/A'}"
        )
    cand_text = "\n".join(cand_lines) if cand_lines else "Aday yok"

    prompt = f"""Crypto futures scalping bot. ~58 USDT. Fee ~0.08%. SADECE YÜKSEK GÜVEN.
{LEVERAGE}x | TP:{TARGET_TP_RATIO*100:.1f}% | SL:{STOP_LOSS_RATIO*100:.1f}% | RR=1:3
Açık:{len(active_positions)}/{MAX_POSITIONS} | BTC:{btc_direction}
LONG={long_count}(max:{MAX_POSITIONS_LONG_DIR}) SHORT={short_count}(max:{MAX_POSITIONS_SHORT_DIR})
Cool:{cooldown_str} | Kilit:{locked_str}

=== LONG KURALLARI ===
L1. Skor MUTLAKA {MIN_SCORE_THRESHOLD}/10+. 'BLOKLAYACAK' yazıyorsa seçme.
L2. BTC GÜÇLÜ AŞAĞI → NO_LONG (bypass: skor9+ VE 1hRSI 42-65 VE 24s>+%4 → REDUCED_MARGIN:YES).
L3. 1hRSI>{rsi_long_lim} → NO_LONG. 1hRSI>68 ise skor 9+ zorunlu.
L4. 1hRSI<40 → NO_LONG. 24s<%2 → NO_LONG. 15mRSI>85 → NO_LONG.
L5. EMA200 ALTINDA ise skor 9+ zorunlu.
L6. YILDIZ COİN (SIREN/BASED/PIPPIN/ENA/SOON): skor 7+ yeterli.
L7. 07:00-08:59 → NO_LONG.

=== SHORT KURALLARI ===
S1. Skor MUTLAKA {MIN_SCORE_SHORT}/10+.
S2. BTC GÜÇLÜ YUKARI → NO_SHORT. BTC HAFİF YUKARI → skor -2 ama yasak değil.
S3. BTC GÜÇLÜ AŞAĞI → SHORT öncelikli.
S4. 1hRSI<25 → NO_SHORT. 1hRSI<65 → NO_SHORT. 24s>+%45 → NO_SHORT.
S5. 4hRSI<55 → NO_SHORT. 1hRSI>=90 → NO_SHORT (squeeze).
S6. SHORT → REDUCED_MARGIN:YES her zaman.
S7. İdeal: 1hRSI>70 + 4hRSI>65 + skor 8+.

=== GENEL ===
G1. Skor 4- açık poz → CLOSE.
G2. Aynı coin/zıt yön yok. PnL>+{MIN_PNL_TO_PROTECT}$ → HOLD.
G3. PnL<{MIN_PNL_TO_CLOSE}$ → CLOSE. Loop'ta max 1 TRADE.

POZ:{pos_desc}
{loss_note}
ADAYLAR:
{cand_text}
HABER:{news[:150] if news else 'N/A'}

ÇIKTI (sadece bu format):
ANALİZ:[max 2 cümle]
KARAR: ACTION:TRADE | COIN:[base] | SIDE:[LONG/SHORT] | REDUCED_MARGIN:[YES/NO]
KARAR: ACTION:CLOSE | COIN:[base] | REASON:[kısa]
KARAR: ACTION:WAIT"""

    try:
        response = client.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)
        _state.api_error_count = 0
        return response.text.strip()
    except Exception as e:
        _state.api_error_count += 1
        print(f"🛑 AI Hatası (#{_state.api_error_count}): {e}")
        if _state.api_error_count == 1:
            send_telegram("⚠️ <b>AI API HATASI</b>\nKarar WAIT olarak işlenecek.")
        return "ANALİZ: API Hatası | KARAR: ACTION:WAIT"

# ---------------------------------------------------------------------------
# KARAR AYRIŞTIRICISI
# ---------------------------------------------------------------------------

def parse_decisions(ai_text: str) -> list[dict]:
    results = []
    for line in ai_text.split('\n'):
        line = line.strip()
        if not line.upper().startswith("KARAR"):
            continue

        action_m = re.search(r'ACTION\s*:\s*(\w+)', line, re.IGNORECASE)
        if not action_m:
            continue

        action = action_m.group(1).upper()

        if action == "WAIT":
            results.append({"action": "WAIT"})
            continue

        coin_m = re.search(r'COIN\s*:\s*([A-Z0-9]+)', line, re.IGNORECASE)
        side_m = re.search(r'SIDE\s*:\s*(LONG|SHORT)', line, re.IGNORECASE)
        rm_m   = re.search(r'REDUCED_MARGIN\s*:\s*(YES|NO)', line, re.IGNORECASE)
        reason_m = re.search(r'REASON\s*:\s*(.+)', line, re.IGNORECASE)

        if action == "TRADE" and coin_m and side_m:
            results.append({
                "action": "TRADE",
                "coin": coin_m.group(1).upper(),
                "side": side_m.group(1).upper(),
                "reduce_margin": (rm_m.group(1).upper() == "YES") if rm_m else False,
            })

        elif action == "CLOSE" and coin_m:
            results.append({
                "action": "CLOSE",
                "coin": coin_m.group(1).upper(),
                "reason": reason_m.group(1).strip() if reason_m else "AI",
            })

    return results if results else [{"action": "WAIT"}]


# ---------------------------------------------------------------------------
# ANA DÖNGÜ
# ---------------------------------------------------------------------------

def main():
    global _micro_loop_running, _watchdog_running

    print("\n" + "="*57)
    print("🚀  SCALP BOT — v18 (TAM REFACTOR) BAŞLATILDI")
    print("="*57)

    # ---- İLK TELEGRAM: API çağrılarından ÖNCE gönder ----
    send_telegram(
        f"🚀 <b>BOT BAŞLADI — v18</b>\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"⏳ Bybit bağlantısı kuruluyor...\n"
        f"⚙️ {LEVERAGE}x  ·  TP {TARGET_TP_RATIO*100:.1f}%  ·  SL {STOP_LOSS_RATIO*100:.1f}%"
    )

    # ---- Bybit bağlantı testi ----
    try:
        initial_positions = md.fetch_positions()
        initial_status    = get_wallet_status(initial_positions)
        _state.start_balance = initial_status['net']

        send_telegram(
            f"✅ <b>BYBIT BAĞLANTISI KURULDU</b>\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"💰 Bakiye  <b>{_state.start_balance:.2f} USDT</b>  |  Açık: {len(initial_positions)}\n"
            f"🔬 <i>v18: Tek karar motoru · Structured log · UNKNOWN sonuç · Fallback kapalı</i>"
        )
    except Exception as e:
        _state.start_balance = 0
        initial_positions = []
        send_telegram(
            f"⚠️ <b>BYBIT BAĞLANTI HATASI</b>\n"
            f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"❌ {str(e)[:200]}\n"
            f"Bot çalışmaya devam edecek, yeniden deneyecek."
        )

    micro_thread = threading.Thread(target=micro_tracking_loop, daemon=True)
    micro_thread.start()
    watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True, name="watchdog")
    watchdog_thread.start()

    last_report_time        = time.time()
    last_daily_summary_date = ""
    _state.iteration_counter = 0

    while True:
        try:
            _state.iteration_counter += 1
            now = time.time()
            update_heartbeat()

            # Kara delik saatleri — UTC+3 (Railway UTC'de çalışır)
            import datetime as _dt
            current_hour = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=3)).hour
            if current_hour in TRADING_BLACKOUT_HOURS:
                active_positions = md.fetch_positions()
                if active_positions:
                    apply_trailing_stops(active_positions)
                    sync_trade_times(active_positions)
                else:
                    print(f"⏸️ KARA DELİK: {current_hour}:00")
                time.sleep(CHECK_INTERVAL)
                continue

            # Funding blackout
            fb_main, fb_main_msg = rm.is_funding_blackout()
            if fb_main:
                print(fb_main_msg)
                active_positions = md.fetch_positions()
                if active_positions:
                    apply_trailing_stops(active_positions)
                    sync_trade_times(active_positions)
                time.sleep(CHECK_INTERVAL)
                continue

            active_positions = md.fetch_positions()
            wallet           = get_wallet_status(active_positions)
            usdt_free        = wallet.get('free', 0)
            net_balance      = wallet.get('net', usdt_free)
            print(f"🧪 free:{usdt_free:.2f} net:{net_balance:.2f}")

            if rm.check_daily_loss(net_balance, send_telegram):
                time.sleep(CHECK_INTERVAL); continue
            if _state.loss_streaks >= 3 and time.time() < _state.losses_pause_until:
                time.sleep(CHECK_INTERVAL); continue

            sync_trade_times(active_positions)
            momentum_closed = check_momentum_exit(active_positions)
            if momentum_closed:
                active_positions = md.fetch_positions()
                wallet    = get_wallet_status(active_positions)
                usdt_free = wallet.get('free', 0)

            # ---- Veri çekme ----
            recently_closed = {cb for cb in _state.coin_closed_profit.keys()
                               if _state.coin_closed_profit.get(cb)}
            market_data = fetch_market_data_safe(recently_closed)
            _state.last_market_snapshot = str(market_data)

            if market_data.get("error"):
                print(f"⚠️ Piyasa verisi hatası: {market_data['error']}")

            btc_dir = market_data.get("btc_direction", "NÖTR")
            _state.last_btc_direction = btc_dir

            news = fetch_latest_news()

            cooldown_coins = [
                c for c, ts in _state.coin_last_closed.items()
                if now - ts < (rm.COIN_COOLDOWN_PROFIT if _state.coin_closed_profit.get(c)
                               else rm.COIN_COOLDOWN_LOSS)
            ]
            rm.reset_daily_coin_data_if_needed()
            locked_coins = list({
                c for c in list(_state.daily_coin_losses.keys()) + list(_state.daily_coin_trades.keys())
                if (_state.daily_coin_losses.get(c, 0) >= rm.MAX_DAILY_LOSSES_COIN or
                    _state.daily_coin_trades.get(c, 0) >= rm.MAX_DAILY_TRADES_COIN)
            })

            pos_summary = "None"
            if active_positions:
                lines = []
                for p in active_positions:
                    pnl    = float(p.get('unrealizedPnl', 0))
                    sym    = p['symbol']
                    opened = _state.trade_times.get(sym)
                    age    = int((now - opened) / 60) if opened else "?"
                    lines.append(f"• {sym}|{p['side'].upper()}|PnL:{pnl:+.2f}|{age}dk")
                pos_summary = "\n".join(lines)

            candidates = market_data.get("candidates", [])

            full_ai = get_ai_decision(
                news, candidates, pos_summary,
                active_positions, cooldown_coins, locked_coins,
                btc_direction=btc_dir
            )

            decisions     = parse_decisions(full_ai)
            analysis_text = "Analiz yapılamadı"
            for line in full_ai.split('\n'):
                if re.match(r"ANALİZ\s*:", line, re.IGNORECASE):
                    analysis_text = line.split(":", 1)[1].strip()
                    break

            print(f"\n--- DÖNGÜ #{_state.iteration_counter} | {time.strftime('%H:%M:%S')} ---")
            print(f"💰 Free:{usdt_free:.2f} Net:{net_balance:.2f} Poz:{len(active_positions)}/{MAX_POSITIONS}")
            print(f"📈 BTC:{btc_dir} | Cool:{cooldown_coins or 'Yok'} | Kilit:{locked_coins or 'Yok'}")
            print(f"🧠 AI: {analysis_text}")

            trade_done = False
            opened_set: set[str] = set()

            for decision in decisions:
                action = decision.get("action")

                if action == "CLOSE":
                    target = re.sub(r'[USDT/:].*$', '', decision["coin"].upper()).strip()
                    p2c    = next((p for p in active_positions if target in p['symbol']), None)
                    if not p2c:
                        print(f"⚠️ CLOSE: {target} yok.")
                        continue
                    pnl = float(p2c.get('unrealizedPnl', 0))
                    if pnl >= MIN_PNL_TO_PROTECT:
                        print(f"🛡️ KORUMA: {p2c['symbol']} ({pnl:+.2f})")
                        continue
                    if MIN_PNL_TO_CLOSE < pnl < 0:
                        print(f"🛡️ ERKEN KES: {p2c['symbol']} {pnl:+.2f}")
                        continue
                    res = ee.close_position(p2c, decision.get("reason", "AI"),
                                            profit=(pnl >= 0),
                                            send_telegram_fn=send_telegram)
                    send_telegram(f"❌ <b>İŞLEM KAPATILDI</b>\n{res}")
                    active_positions = md.fetch_positions()

                elif action == "TRADE":
                    if trade_done:
                        print("⚠️ 2. TRADE atlandı.")
                        continue
                    if len(active_positions) >= MAX_POSITIONS:
                        print("⚠️ Max poz.")
                        continue
                    if usdt_free < FIXED_MIN_USDT:
                        print("⚠️ Yetersiz bakiye.")
                        continue

                    coin          = decision["coin"]
                    side          = decision["side"]
                    reduce_margin = decision.get("reduce_margin", False)
                    trade_type    = decision.get("trade_type", "NORMAL")
                    clean = re.sub(r'[:/].*$', '', coin.upper()).replace('USDT', '')

                    if clean in opened_set: continue

                    # Eşleşen candidate'i bul (features için)
                    cand = next((c for c in candidates if c.get("base") == clean), None)

                    res = ee.execute_trade(
                        {'COIN': coin, 'SIDE': side},
                        usdt_free,
                        active_positions=active_positions,
                        reduce_margin=reduce_margin,
                        btc_direction=btc_dir,
                        send_telegram_fn=send_telegram,
                        trade_type=trade_type,
                        candidate=cand,
                    )

                    if "İŞLEM AÇILDI" in res:
                        send_telegram(res)
                        active_positions = md.fetch_positions()
                        wallet    = get_wallet_status(active_positions)
                        usdt_free = wallet.get('free', 0)
                        trade_done = True
                        opened_set.add(clean)
                    else:
                        print(f"⚠️ İşlem açılamadı: {res}")
                    time.sleep(2)

            # ---- Günlük özet ----
            current_hm = time.strftime('%H:%M')
            today_str  = time.strftime('%Y-%m-%d')
            if current_hm >= '23:55' and last_daily_summary_date != today_str:
                tl.log_daily_summary(send_telegram)
                last_daily_summary_date = today_str

            # ---- Periyodik rapor ----
            if now - last_report_time >= 600:
                net_pnl   = net_balance - (_state.start_balance or net_balance)
                data_age  = int(time.time() - _state.last_data_fetch_time) if _state.last_data_fetch_time else -1
                data_note = f"\n📡 VeriYaşı:{data_age}s" if data_age > DATA_FETCH_TIMEOUT else ""
                btc_arrow = {"GÜÇLÜ YUKARI": "▲▲", "HAFİF YUKARI": "▲",
                             "NÖTR": "●", "HAFİF AŞAĞI": "▼", "GÜÇLÜ AŞAĞI": "▼▼"}.get(btc_dir, "●")

                pos_lines = ""
                for p in active_positions:
                    p_pnl  = float(p.get('unrealizedPnl', 0))
                    p_sym  = p['symbol'].split('/')[0]
                    p_side = "▲" if p['side'].lower() == 'long' else "▼"
                    p_icon = "🟢" if p_pnl >= 0 else "🔴"
                    pos_lines += f"\n  {p_icon} {p_sym} {p_side}  {p_pnl:+.2f}$"
                if not pos_lines:
                    pos_lines = "\n  <i>Boş</i>"

                pnl_icon = "🟢" if net_pnl >= 0 else "🔴"
                send_telegram(
                    f"📊 <b>RAPOR</b>  ·  {time.strftime('%H:%M')}\n"
                    f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
                    f"💰 <b>{net_balance:.2f} USDT</b>  {pnl_icon} {net_pnl:+.2f}$\n"
                    f"📈 BTC  {btc_arrow} {btc_dir}\n"
                    f"📂 Pozisyon  {len(active_positions)}/{MAX_POSITIONS}{pos_lines}\n"
                    f"🏷️ {', '.join(cooldown_coins[:3]) if cooldown_coins else '—'}"
                    f"{data_note}\n"
                    f"🧠 <i>{analysis_text}</i>"
                )
                last_report_time = now

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            _micro_loop_running  = False
            _watchdog_running    = False
            tl.log_daily_summary(send_telegram)
            print("\n🛑 Bot durduruldu.")
            break
        except Exception as e:
            err = f"🛑 <b>ANA DÖNGÜ HATASI:</b>\n{str(e)}"
            print(err)
            send_telegram(err)
            time.sleep(30)


if __name__ == "__main__":
    main()