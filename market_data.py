"""
market_data.py — Tüm Bybit API çağrıları
OHLCV, RSI, funding rate, open interest, ticker.
İndikatör hesaplamaları feature_engine.py'de yapılıyor;
bu modül sadece ham veri çekiyor.

⚙️  MOD SEÇİMİ (.env veya Railway Variables):
    BYBIT_API_KEY    = ...
    BYBIT_SECRET_KEY = ...
    BYBIT_MODE       = real     → Gerçek Bybit (varsayılan)
                     = demo     → Bybit Demo Trading (api-demo.bybit.com, simüle)
                     = testnet  → Bybit Testnet (ayrı platform, test parası)

📌  DEMO NOT: Demo key'i bybit.com → üst menü → Demo Trading → API Management'tan alınmalı.
    Gerçek hesap key'i ile demo endpoint'e bağlanılamaz, tam tersi de geçersiz.
    Demo: 50.000 USDT simüle bakiye, gerçek piyasa fiyatları.

    Bybit sembol formatı: BTC/USDT:USDT (linear perpetual)
"""

import os
import ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

EXCHANGE_TIMEOUT_MS = 10_000  # 10 sn

# ---- Mod: "real" | "demo" | "testnet" ----
BYBIT_MODE = os.getenv("BYBIT_MODE", "real").lower()


def get_exchange() -> ccxt.Exchange:
    api_key = os.getenv("BYBIT_API_KEY", "")
    secret  = os.getenv("BYBIT_SECRET_KEY", "")

    # Güvenli debug logu — key'in uzunluğunu ve ilk/son 3 karakterini göster
    print(f"🔑 Bybit modu: {BYBIT_MODE.upper()} | "
          f"key:{api_key[:3]}...{api_key[-3:]}({len(api_key)}kr) "
          f"secret:{len(secret)}kr")

    config = {
        'apiKey':          api_key,
        'secret':          secret,
        'enableRateLimit': True,
        'timeout':         EXCHANGE_TIMEOUT_MS,
        'options': {
            'defaultType': 'linear',   # USDT-margined perpetual
        },
    }

    if BYBIT_MODE == "demo":
        config['options']['demo'] = True
    elif BYBIT_MODE == "testnet":
        config['options']['testnet'] = True
    # "real" → ek flag yok

    return ccxt.bybit(config)


# ---- Singleton exchange ----
_exchange: ccxt.Exchange | None = None


def exchange() -> ccxt.Exchange:
    global _exchange
    if _exchange is None:
        _exchange = get_exchange()
    return _exchange


def reset_exchange():
    """Exchange singleton'ı sıfırla — key değişince çağır."""
    global _exchange
    _exchange = None
    print("🔄 Exchange bağlantısı sıfırlandı.")


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------

def fetch_ohlcv(symbol: str, timeframe: str = '15m', limit: int = 210) -> pd.DataFrame | None:
    try:
        bars = exchange().fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 40:
            return None
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = df.astype({'open': float, 'high': float, 'low': float,
                        'close': float, 'volume': float})
        return df
    except Exception as e:
        print(f"⚠️ fetch_ohlcv [{symbol} {timeframe}]: {e}")
        return None


# ---------------------------------------------------------------------------
# TICKER
# ---------------------------------------------------------------------------

def fetch_ticker(symbol: str) -> dict | None:
    try:
        return exchange().fetch_ticker(symbol)
    except Exception as e:
        print(f"⚠️ fetch_ticker [{symbol}]: {e}")
        return None


def fetch_tickers() -> dict:
    try:
        return exchange().fetch_tickers()
    except Exception as e:
        print(f"⚠️ fetch_tickers: {e}")
        return {}


# ---------------------------------------------------------------------------
# FUNDING RATE & OPEN INTEREST
# ---------------------------------------------------------------------------

def fetch_funding_rate(symbol: str) -> float | None:
    try:
        data = exchange().fetch_funding_rate(symbol)
        rate = data.get('fundingRate')
        return float(rate) * 100 if rate is not None else None
    except Exception:
        return None


def fetch_open_interest_change(symbol: str) -> float | None:
    try:
        history = exchange().fetch_open_interest_history(symbol, '1h', limit=25)
        if history and len(history) >= 24:
            oi_now = float(history[-1].get('openInterestAmount', 0) or
                           history[-1].get('openInterest', 0))
            oi_24h = float(history[-25].get('openInterestAmount', 0) or
                           history[-25].get('openInterest', 0))
            if oi_24h > 0:
                return round((oi_now - oi_24h) / oi_24h * 100, 2)
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ORDERS / TRADES
# ---------------------------------------------------------------------------

def fetch_my_trades(symbol: str, limit: int = 5) -> list:
    try:
        return exchange().fetch_my_trades(symbol, limit=limit)
    except Exception as e:
        print(f"⚠️ fetch_my_trades [{symbol}]: {e}")
        return []


def fetch_open_orders(symbol: str) -> list:
    try:
        return exchange().fetch_open_orders(symbol)
    except Exception as e:
        print(f"⚠️ fetch_open_orders [{symbol}]: {e}")
        return []


def cancel_order(order_id: str, symbol: str) -> bool:
    try:
        exchange().cancel_order(order_id, symbol)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# BALANCE / POSITIONS
# ---------------------------------------------------------------------------

def fetch_balance() -> dict:
    try:
        return exchange().fetch_balance()
    except Exception as e:
        print(f"⚠️ fetch_balance: {e}")
        return {}


def fetch_positions() -> list:
    try:
        return [p for p in exchange().fetch_positions()
                if float(p.get('contracts', 0)) > 0]
    except Exception as e:
        print(f"⚠️ fetch_positions: {e}")
        return []


# ---------------------------------------------------------------------------
# MARKETS
# ---------------------------------------------------------------------------

_markets_cache: dict | None = None


def get_markets() -> dict:
    global _markets_cache
    if _markets_cache is None:
        _markets_cache = exchange().load_markets()
    return _markets_cache