"""Матрица вариантов бота: что даёт прибыль на РЕАЛЬНЫХ ценах рынка.

Перебираем все осмысленные комбинации:
  * сигнал (несколько вариантов предсказания направления),
  * роль (тейкер по рынку / мейкер лимитником),
  * цена входа (из фактически наблюдавшегося стакана),
  * актив (BTC / ETH / BNB — у них разная ликвидность и спред),
  * горизонт (5 мин / 15 мин / сутки).

Каждый вариант оценивается по одному критерию: EV на сделку с учётом
реальной цены покупки и комиссии. Итог — CSV + печатная таблица.

    python scripts/updown_matrix.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "logs" / "book_snapshots.jsonl"
OUT_CSV = ROOT / "logs" / "strategy_matrix.csv"

REBATE = 0.25


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


def maker_credit(p: float) -> float:
    return taker_fee(p) * REBATE


def cost_of(price: float, role: str) -> float:
    """Полная стоимость контракта с учётом комиссии/ребейта."""
    if role == "taker":
        return price + taker_fee(price)
    return price - maker_credit(price)


def ev_per_dollar(win_rate: float, price: float, role: str) -> float:
    """Ожидаемая прибыль на $1 вложенный."""
    c = cost_of(price, role)
    if c <= 0:
        return 0.0
    return (win_rate / 100 * 1.0 - c) / c


def breakeven(price: float, role: str) -> float:
    return cost_of(price, role) * 100


def load_book() -> pd.DataFrame:
    rows = []
    with SNAP.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    d = pd.DataFrame(rows)
    d["t"] = pd.to_datetime(d.t)
    d["asset"] = d.slug.str.extract(r"^(bitcoin|ethereum|bnb)")[0]
    # горизонт окна из слага
    def horizon(s: str) -> str:
        s = s or ""
        if "-on-" in s or s.count("-") <= 5:
            return "1d"
        if "am-et" in s or "pm-et" in s:
            return "5m/1h"
        return "?"
    d["horizon"] = d.slug.map(horizon)
    return d


def market_prices(d: pd.DataFrame) -> pd.DataFrame:
    """Реальные цены покупки по активам: что наблюдалось в стакане."""
    rows = []
    for (asset,), g in d.groupby(["asset"], dropna=True):
        for side in ("Up", "Down"):
            ask = g[f"{side}_ask"].dropna()
            bid = g[f"{side}_bid"].dropna()
            if len(ask) < 10:
                continue
            # отбрасываем уже решённые рынки (цена у краёв)
            mid = ask[(ask > 0.15) & (ask < 0.85)]
            rows.append({
                "asset": asset, "side": side, "n": len(ask),
                "ask_min": ask.min(), "ask_p10": ask.quantile(.10),
                "ask_med": ask.median(),
                "ask_mid_med": mid.median() if len(mid) else np.nan,
                "bid_med": bid.median() if len(bid) else np.nan,
                "spread_med": (g[f"{side}_ask"] - g[f"{side}_bid"]).median(),
            })
    return pd.DataFrame(rows)


# Винрейты, полученные в исследовании (walk-forward, вне выборки).
SIGNALS = {
    "fade mom10 (базовый)": 52.3,
    "fade + walk-forward":  52.9,
    "fade + фильтры (in-sample)": 62.2,   # не переносится, для сравнения
    "fade + фильтры (out-of-sample)": 51.7,
    "трендовый (старый)": 48.9,
    "монетка": 50.0,
}


def main() -> None:
    if not SNAP.exists():
        print(f"нет {SNAP}")
        return
    d = load_book()
    mp = market_prices(d)

    print("=" * 100)
    print("РЕАЛЬНЫЕ ЦЕНЫ ИЗ СТАКАНА (что наблюдалось за время замеров)")
    print("=" * 100)
    print(mp.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    # Цены-кандидаты для входа: реально наблюдавшиеся уровни.
    price_points = {
        "лучший ask (p10)": None,     # заполним из данных
        "медианный ask": None,
        "мейкер на bid": None,
        "мейкер bid+1ц": None,
    }

    rows = []
    for asset in ("bitcoin", "ethereum", "bnb"):
        sub = mp[mp.asset == asset]
        if sub.empty:
            continue
        ask_p10 = float(sub.ask_p10.min())
        ask_med = float(sub.ask_mid_med.median()) if sub.ask_mid_med.notna().any() \
            else float(sub.ask_med.median())
        bid_med = float(sub.bid_med.median())

        variants = [
            ("тейкер, лучший ask (p10)", "taker", ask_p10),
            ("тейкер, медианный ask", "taker", ask_med),
            ("мейкер на медианном bid", "maker", bid_med),
            ("мейкер bid+1ц", "maker", round(bid_med + 0.01, 2)),
            ("мейкер агрессивный 0.50", "maker", 0.50),
            ("мейкер предел 0.52", "maker", 0.52),
        ]
        for sig, wr in SIGNALS.items():
            for label, role, price in variants:
                if not np.isfinite(price) or price <= 0 or price >= 1:
                    continue
                ev = ev_per_dollar(wr, price, role)
                be = breakeven(price, role)
                rows.append({
                    "актив": asset, "сигнал": sig, "винрейт": wr,
                    "вход": label, "роль": role, "цена": round(price, 3),
                    "порог_бу": round(be, 2), "перевес_пп": round(wr - be, 2),
                    "EV_на_сделку": round(ev, 4),
                    "прибыльно": ev > 0,
                })

    res = pd.DataFrame(rows)
    res.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 100)
    print("МАТРИЦА ВАРИАНТОВ")
    print("=" * 100)
    print(f"всего комбинаций: {len(res)}")
    print(f"прибыльных: {res.прибыльно.sum()}")
    print(f"убыточных:  {(~res.прибыльно).sum()}")

    good = res[res.прибыльно].sort_values("EV_на_сделку", ascending=False)
    if len(good):
        print("\nПРИБЫЛЬНЫЕ ВАРИАНТЫ:")
        print(good.to_string(index=False))
    else:
        print("\nПРИБЫЛЬНЫХ ВАРИАНТОВ НЕТ.")

    print("\nЛУЧШИЕ 15 ПО EV (включая убыточные):")
    print(res.nlargest(15, "EV_на_сделку").to_string(index=False))

    # Отдельно: реалистичные варианты (без in-sample винрейта)
    real = res[~res.сигнал.str.contains("in-sample")]
    print("\n" + "=" * 100)
    print("ТОЛЬКО ПОДТВЕРЖДЁННЫЕ ВИНРЕЙТЫ (без in-sample подгонки)")
    print("=" * 100)
    print(f"прибыльных: {real.прибыльно.sum()} из {len(real)}")
    rg = real[real.прибыльно]
    if len(rg):
        print(rg.sort_values("EV_на_сделку", ascending=False).to_string(index=False))
    else:
        print("НЕТ НИ ОДНОГО.")

    print(f"\nтаблица сохранена: {OUT_CSV}")


if __name__ == "__main__":
    main()
