"""Нормализация таймфрейма и допуск CFD в /api/klines."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from neft.core.panel_guard import norm_tf, safe_klines


def test_norm_tf():
    assert norm_tf("M1") == "1m"
    assert norm_tf("m5") == "5m"
    assert norm_tf("15m") == "15m"
    assert norm_tf("H1") == "1h"
    assert norm_tf("4h") == "4h"
    assert norm_tf("D1") == "1d"
    assert norm_tf("bogus") == ""


def test_crypto_klines():
    assert safe_klines("ETH/USDT:USDT", "M5", "bybit") == ("ETHUSDT", "5m", "bybit")
    assert safe_klines("1000PEPEUSDT", "4h", "binance") == ("1000PEPEUSDT", "4h", "binance")
    assert safe_klines("LINKUSDT", "1d", "")[2] == "bybit"


def test_cfd_klines():
    assert safe_klines("NAS100", "5m", "mt5") == ("NAS100", "5m", "mt5")
    assert safe_klines("XAUUSD+", "H1", "bybit") == ("XAUUSD+", "1h", "mt5")
    assert safe_klines("EURUSD+", "M15", "mt5") == ("EURUSD+", "15m", "mt5")
    assert safe_klines("DJ30", "1d", "mt5") == ("DJ30", "1d", "mt5")
    assert safe_klines("../etc", "1m", "mt5") is None
    assert safe_klines("ETHUSDT", "1m", "mt5")[2] == "bybit"


def test_scanner_scope_gates_crypto():
    from neft.core.scanner import allow_new_entries, trade_markets
    sc = {"enabled": True, "scope": "cfd"}
    assert trade_markets("both", sc) == (False, True)
    assert trade_markets("crypto", sc) == (False, False)
    assert trade_markets("both", {"enabled": True, "scope": "crypto"}) == (True, False)
    assert trade_markets("both", {"enabled": False, "scope": "cfd"}) == (True, True)
    assert allow_new_entries(True, "cfd", "crypto", held=False, in_hot=True) is False
    assert allow_new_entries(True, "cfd", "crypto", held=True, in_hot=False) is True
    assert allow_new_entries(True, "cfd", "cfd", held=False, in_hot=True) is True
    assert allow_new_entries(True, "cfd", "cfd", held=False, in_hot=False) is False
    assert allow_new_entries(False, "cfd", "crypto", held=False, in_hot=False) is True


if __name__ == "__main__":
    test_norm_tf()
    test_crypto_klines()
    test_cfd_klines()
    test_scanner_scope_gates_crypto()
    print("ok")
