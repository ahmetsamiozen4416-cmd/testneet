"""
market_data.py — Tüm Binance API çağrıları (Task 8)
OHLCV, RSI, funding rate, open interest, ticker.
İndikatör hesaplamaları feature_engine.py'de yapılıyor;
bu modül sadece ham veri çekiyor.

⚙️  TESTNET MODU: Binance Futures Testnet
    API Key → https://testnet.binancefuture.com
    .env dosyasına aynı değişken adlarını yaz:
        BINANCE_API_KEY=...
        BINANCE_SECRET_KEY=...
    Gerçeğe geçmek için TESTNET = False yap, URL bloğunu kaldır.
"""

import os
import ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

EXCHANGE_TIMEOUT_MS = 8_000  # 8 sn

# ---- Testnet bayrağı: False → gerçek Binance Futures ----
TESTNET = True

FUTURES_TESTNET_URLS = {
    'api': {
        'public':  'https://testnet.binancefuture.com',
        'private': 'https://testnet.binancefuture.com',
    },
    'fapiPublic':  'https://testnet.binancefuture.com/fapi/v1',
    'fapiPrivate': 'https://testnet.binancefuture.com/fapi/v1',
    'fapiPublicV2':  'https://testnet.binancefuture.com/fapi/v2',
    'fapiPrivateV2': 'https://testnet.binancefuture.com/fapi/v2',
}


def get_exchange() -> ccxt.Exchange:
    config = {
        'apiKey':          os.getenv("BINANCE_API_KEY"),
        'secret':          os.getenv("BINANCE_SECRET_KEY"),
        'enableRateLimit': True,
        'timeout':         EXCHANGE_TIMEOUT_MS,
        'options':         {'defaultType': 'future'},
    }
    if TESTNET:
        config['urls'] = FUTURES_TESTNET_URLS
    return ccxt.binance(config)


# ---- Singleton exchange ----
_exchange: ccxt.Exchange | None = None


def exchange() -> ccxt.Exchange:
    global _exchange
    if _exchange is None:
        _exchange = get_exchange()
    return _exchange


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------

def fetch_ohlcv(symbol: str, timeframe: str = '15m', limit: int = 210) -> pd.DataFrame | None:
    """Ham OHLCV bar listesi → DataFrame. None dönerse veri yetersiz."""
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
    """
    Yüzde cinsinden funding rate.
    Negatif = short baskı (LONG için olumlu).
    Pozitif = long baskı (SHORT için olumlu).
    Testnet'te desteklenir; hata durumunda None döner.
    """
    try:
        data = exchange().fetch_funding_rate(symbol)
        rate = data.get('fundingRate')
        return float(rate) * 100 if rate is not None else None
    except Exception:
        return None


def fetch_open_interest_change(symbol: str) -> float | None:
    """
    Son 24 saatlik OI değişimi (%).
    Testnet'te OI geçmişi sınırlı olabilir; veri yoksa None döner.
    """
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
# ORDERS / TRADES (TP-SL tespiti için)
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