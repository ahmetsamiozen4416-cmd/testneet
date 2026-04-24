"""
state_manager.py — Merkezi durum yönetimi (Task 7)
Tüm global değişkenler buraya taşındı. Thread-safe erişim sağlanıyor.
"""

import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class BotState:
    # Açık pozisyon takibi
    open_positions: List[dict] = field(default_factory=list)   # ccxt pos objeleri

    # Trade zamanlaması: symbol → epoch
    trade_times: Dict[str, float] = field(default_factory=dict)
    trade_entry_prices: Dict[str, float] = field(default_factory=dict)
    trade_sides: Dict[str, str] = field(default_factory=dict)   # 'BUY' / 'SELL'
    trade_contexts: Dict[str, dict] = field(default_factory=dict)

    # Trade geçmişi / cool-down
    coin_last_closed: Dict[str, float] = field(default_factory=dict)
    coin_closed_profit: Dict[str, bool] = field(default_factory=dict)

    # Günlük coin takibi
    daily_coin_losses: Dict[str, int] = field(default_factory=dict)
    daily_coin_trades: Dict[str, int] = field(default_factory=dict)
    daily_coin_lock_date: Optional[str] = None
    daily_coin_lock_timestamps: Dict[str, float] = field(default_factory=dict)

    # Volatility lock: coin_base → timestamp
    coin_locks: Dict[str, float] = field(default_factory=dict)

    # Cooldown takibi (cool-down biten coinler)
    cooldowns: Dict[str, float] = field(default_factory=dict)

    # Art arda kayıp sayacı
    coin_consec_losses: Dict[str, int] = field(default_factory=dict)
    loss_streaks: int = 0
    losses_pause_until: float = 0

    # Race condition koruması
    recently_opened_coins: Dict[str, float] = field(default_factory=dict)

    # Genel bot durumu
    bot_paused: bool = False
    pause_until: float = 0
    start_balance: Optional[float] = None
    iteration_counter: int = 0

    # Hata / debug
    error_log: List[dict] = field(default_factory=list)
    last_market_snapshot: str = ""
    api_error_count: int = 0
    last_btc_direction: str = "NÖTR"

    # [FIX-FREEZE] Cache
    last_successful_market_data: dict = field(default_factory=dict)
    last_data_fetch_time: float = 0

    # Watchdog heartbeat
    last_heartbeat_time: float = field(default_factory=time.time)


# ---- Tek instance + lock ----
_lock = threading.Lock()
state = BotState()


def get_state() -> BotState:
    """Paylaşılan state'e erişim."""
    return state


def with_lock(fn):
    """Decorator: state değişikliklerini lock ile sarmalayarak thread-safe yapar."""
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return wrapper


@with_lock
def update_heartbeat():
    state.last_heartbeat_time = time.time()


@with_lock
def record_trade_open(symbol: str, entry_price: float, side_str: str, context: dict | None = None):
    """Yeni işlem açıldığında çağrılır."""
    state.trade_times[symbol] = time.time()
    state.trade_entry_prices[symbol] = entry_price
    state.trade_sides[symbol] = side_str
    state.trade_contexts[symbol] = dict(context or {})
    coin_base = symbol.split('/')[0].strip()
    state.recently_opened_coins[coin_base] = time.time()


@with_lock
def record_trade_close(symbol: str, profit: bool):
    """İşlem kapandığında çağrılır."""
    state.trade_times.pop(symbol, None)
    state.trade_entry_prices.pop(symbol, None)
    state.trade_sides.pop(symbol, None)
    state.trade_contexts.pop(symbol, None)

    cb = symbol.split('/')[0].strip()
    state.coin_last_closed[cb] = time.time()
    state.coin_closed_profit[cb] = profit

    if profit:
        state.coin_consec_losses[cb] = 0
        state.loss_streaks = 0
    else:
        state.coin_consec_losses[cb] = state.coin_consec_losses.get(cb, 0) + 1
        state.loss_streaks = min(state.loss_streaks + 1, 3)
