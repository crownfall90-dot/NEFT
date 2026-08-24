"""Спецификация инструмента из терминала.

Форекс и индексы считаются по-разному: у EURUSD контракт 100 000 и точка
0.00001, у NAS100 контракт 1.0 и точка 0.01. Захардкоженные константы дают
здесь бессмысленные объёмы и риск.
"""
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from neft.core.config import ROOT

CACHE = ROOT / "data" / "symbols.json"
SESSION_SPREADS = ROOT / "data" / "session_spreads.json"

_spread_cache: dict | None = None


def _session_spreads() -> dict:
    global _spread_cache
    if _spread_cache is None:
        _spread_cache = (json.loads(SESSION_SPREADS.read_text(encoding="utf-8"))
                         if SESSION_SPREADS.exists() else {})
    return _spread_cache


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    point: float
    digits: int
    contract_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    spread: int

    @property
    def default_spread(self) -> float:
        """Спред для баров, где история его не записала.

        Берём медиану РЕАЛЬНЫХ записей за торговое окно, а не снимок из
        терминала: снятый ночью спред в разы шире дневного и делает любую
        стратегию убыточной искусственно.
        """
        table = _session_spreads()
        if self.name in table:
            return float(table[self.name])
        return float(self.spread) if self.spread else 2.0

    @property
    def pip(self) -> float:
        """Пункт котировки: для 5/3-значного форекса это 10 point, иначе point."""
        return self.point * 10 if self.digits in (3, 5) else self.point


def load(symbol: str, refresh: bool = False) -> SymbolSpec:
    cache = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    if symbol in cache and not refresh:
        return SymbolSpec(**cache[symbol])

    import MetaTrader5 as mt5
    owned = mt5.terminal_info() is None
    if owned and not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    mt5.symbol_select(symbol, True)
    i = mt5.symbol_info(symbol)
    if owned:
        mt5.shutdown()
    if i is None:
        raise RuntimeError(f"Символ {symbol} не найден")

    spec = SymbolSpec(
        name=i.name, point=i.point, digits=i.digits,
        contract_size=i.trade_contract_size, volume_min=i.volume_min,
        volume_step=i.volume_step, volume_max=i.volume_max, spread=i.spread,
    )
    CACHE.parent.mkdir(exist_ok=True)
    cache[symbol] = asdict(spec)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return spec
