"""Статистический разбор лога сделок #SONIK (XAUUSD.f)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console
from rich.table import Table

con = Console()
CSV = Path(__file__).resolve().parents[1] / "data" / "sonik_trades.csv"


def load() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    df["open"] = pd.to_datetime(df["open"])
    df["close"] = pd.to_datetime(df["close"])
    df["hold_min"] = (df["close"] - df["open"]).dt.total_seconds() / 60.0
    # пункты цены: pnl = points * lot * contract(100)
    df["points"] = df["pnl"] / (df["lot"] * 100.0)
    df["win"] = df["pnl"] > 0
    df["hour"] = df["open"].dt.hour
    df["minute"] = df["open"].dt.minute
    df["dow"] = df["open"].dt.dayofweek  # 0=Mon
    df["day"] = df["open"].dt.date
    df["hm"] = df["hour"] * 60 + df["minute"]
    return df


def main() -> None:
    df = load()
    n = len(df)
    wins = df[df.win]
    losses = df[~df.win]
    con.print(f"[cyan]сделок[/] {n} · WR {len(wins)/n*100:.2f}% · "
              f"wins {len(wins)} / losses {len(losses)}")
    con.print(f"сумма pnl ${df.pnl.sum():.2f} · avg win ${wins.pnl.mean():.2f} · "
              f"avg loss ${losses.pnl.mean():.2f}")
    con.print(f"hold: med {df.hold_min.median():.1f}м · "
              f"p25 {df.hold_min.quantile(.25):.1f} · p75 {df.hold_min.quantile(.75):.1f} · "
              f"max {df.hold_min.max():.1f}")
    con.print(f"points win med {wins.points.median():.2f} · "
              f"loss med {losses.points.median():.2f} · "
              f"win p75 {wins.points.quantile(.75):.2f} · "
              f"loss p25 {losses.points.quantile(.25):.2f}")

    # час входа
    t = Table(title="Входы по часу")
    t.add_column("час"); t.add_column("N", justify="right")
    t.add_column("WR%", justify="right"); t.add_column("pnl", justify="right")
    for h, g in df.groupby("hour"):
        t.add_row(f"{h:02d}", str(len(g)), f"{g.win.mean()*100:.0f}",
                  f"${g.pnl.sum():.1f}")
    con.print(t)

    # кластеры окон
    buckets = [
        ("Asia 00-06", (0, 6)),
        ("London open 06-10", (6, 10)),
        ("London mid 10-14", (10, 14)),
        ("NY overlap 14-18", (14, 18)),
        ("Evening 18-24", (18, 24)),
    ]
    t2 = Table(title="Окна сессий")
    t2.add_column("окно"); t2.add_column("N", justify="right")
    t2.add_column("%", justify="right"); t2.add_column("WR%", justify="right")
    t2.add_column("avg hold", justify="right"); t2.add_column("pnl", justify="right")
    for name, (a, b) in buckets:
        g = df[(df.hour >= a) & (df.hour < b)]
        if not len(g):
            continue
        t2.add_row(name, str(len(g)), f"{len(g)/n*100:.0f}",
                   f"{g.win.mean()*100:.0f}", f"{g.hold_min.median():.0f}м",
                   f"${g.pnl.sum():.1f}")
    con.print(t2)

    # сделок в день
    per_day = df.groupby("day").size()
    con.print(f"сделок/день: med {per_day.median():.0f} · mean {per_day.mean():.2f} · "
              f"max {per_day.max()} · дней с 1 сделкой {(per_day==1).mean()*100:.0f}%")

    # buy vs sell
    for side, g in df.groupby("side"):
        con.print(f"{side}: N={len(g)} WR={g.win.mean()*100:.0f}% pnl=${g.pnl.sum():.1f} "
                  f"pts_win_med={g[g.win].points.median():.2f}")

    # фиксированный тейк? гистограмма пунктов выигрыша
    con.print("\n[bold]распределение пунктов (wins)[/]")
    bins = [0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 50]
    wpts = wins.points.clip(lower=0)
    for i in range(len(bins) - 1):
        c = ((wpts >= bins[i]) & (wpts < bins[i + 1])).sum()
        con.print(f"  {bins[i]}–{bins[i+1]}: {c}")

    con.print("\n[bold]распределение пунктов (losses, abs)[/]")
    lpts = (-losses.points).clip(lower=0)
    for i in range(len(bins) - 1):
        c = ((lpts >= bins[i]) & (lpts < bins[i + 1])).sum()
        con.print(f"  {bins[i]}–{bins[i+1]}: {c}")

    # RR implied
    aw, al = wins.points.mean(), abs(losses.points.mean())
    con.print(f"\navg win pts {aw:.2f} / avg loss pts {al:.2f} → payoff {aw/al:.2f}")
    con.print(f"expectancy pts {(wins.points.sum()+losses.points.sum())/n:.3f}")

    # быстрые лузы vs долгие
    con.print(f"loss hold med {losses.hold_min.median():.1f}м · "
              f"win hold med {wins.hold_min.median():.1f}м")

    # подряд одинаковая сторона
    same = (df.side == df.side.shift()).sum()
    con.print(f"доля сделок с той же стороной что предыдущая: {same/(n-1)*100:.0f}%")


if __name__ == "__main__":
    main()
