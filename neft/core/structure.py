"""Структура рынка: свинги, слом структуры, сессионные диапазоны.

Всё считается ПРИЧИННО: свинг подтверждается только через `right` баров после
экстремума. Заглядывать вперёд нельзя — иначе бэктест увидит то, чего в реальном
времени на графике ещё нет.
"""
import numpy as np
import pandas as pd


def swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Фрактальные свинги. Колонки:

    swing_high / swing_low — цена экстремума на баре, где он ПОДТВЕРЖДЁН
    (то есть со сдвигом на `right` баров вперёд от самого экстремума).
    """
    high, low = df.high.to_numpy(), df.low.to_numpy()
    n = len(df)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)

    for i in range(left, n - right):
        h = high[i]
        if h >= high[i - left:i].max() and h > high[i + 1:i + right + 1].max():
            sh[i + right] = h          # подтверждён только здесь
        lo = low[i]
        if lo <= low[i - left:i].min() and lo < low[i + 1:i + right + 1].min():
            sl[i + right] = lo

    out = pd.DataFrame({"swing_high": sh, "swing_low": sl}, index=df.index)
    # Последний подтверждённый свинг, доступный на каждом баре.
    out["last_high"] = out.swing_high.ffill()
    out["last_low"] = out.swing_low.ffill()
    return out


def structure_state(df: pd.DataFrame, sw: pd.DataFrame) -> pd.Series:
    """Бычья (1) / медвежья (-1) / неопределённая (0) структура.

    Бычья = растущие хаи и лоу, медвежья = падающие. Ровно то, что автор
    объясняет в разборе технического анализа.
    """
    hi = sw.swing_high.dropna()
    lo = sw.swing_low.dropna()
    state = pd.Series(0, index=df.index, dtype=int)

    hh = hi.diff() > 0
    ll = lo.diff() < 0
    hl = lo.diff() > 0
    lh = hi.diff() < 0

    bull = (hh.reindex(df.index).ffill().fillna(False)
            & hl.reindex(df.index).ffill().fillna(False))
    bear = (ll.reindex(df.index).ffill().fillna(False)
            & lh.reindex(df.index).ffill().fillna(False))
    state[bull] = 1
    state[bear] = -1
    return state


def session_range(df: pd.DataFrame, start_hour: int, end_hour: int) -> pd.DataFrame:
    """Хай и лоу сессии за каждый день плюс границы зон.

    Зона строится как у автора: для поддержки — от самого нижнего фитиля до тела
    свечи, поставившей минимум; для сопротивления — зеркально.
    """
    d = df.copy()
    d["day"] = d.time.dt.date
    d["hour"] = d.time.dt.hour
    win = d[(d.hour >= start_hour) & (d.hour < end_hour)]

    rows = []
    for day, g in win.groupby("day"):
        if g.empty:
            continue
        lo_i = g.low.idxmin()
        hi_i = g.high.idxmax()
        lo_bar, hi_bar = d.loc[lo_i], d.loc[hi_i]
        rows.append({
            "day": day,
            "low": float(g.low.min()),
            "low_zone_top": float(max(lo_bar.open, lo_bar.close)),
            "high": float(g.high.max()),
            "high_zone_bot": float(min(hi_bar.open, hi_bar.close)),
            "ready_at": g.time.max(),      # раньше конца сессии зон не существует
        })
    return pd.DataFrame(rows)
