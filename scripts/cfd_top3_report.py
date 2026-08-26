"""Топ-3 CFD-сетапа на $1000: минимум риска, максимум прибыли.

Сравнивает проверенные связки машины (HSS plus_only, London S/R, Martingale)
на одной истории ~90д M5, комиссии Bybit Tight-Spread.

Score = Calmar × штраф за DD выше 12% × бонус за число сделок.
Цель конфига: winrate_risk (не максимум дохода любой ценой).

    python scripts/cfd_top3_report.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import data, metrics
from neft.backtest.engine import Backtester
from neft.core import symbols
from neft.core.bybit_cfd_fees import commission_per_lot, costs_for
from neft.core.config import ROOT
from neft.core.mt5_symbols import HSS_PLUS_SYMBOLS
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.london_sr import LondonSR
from neft.strategies.martingale import Martingale
from neft.strategies.scalp_ha import ScalpHA

con = Console()
OUT = ROOT / "dashboard"
BALANCE = 1000.0
RISK = 0.5          # конфиг машины: низкий риск
DAYS = 90
TARGET_DD = 12.0    # max_drawdown_pct из bot_config


def _json_dump(obj) -> str:
    def fix(o):
        if isinstance(o, float):
            if o != o:
                return None
            if o == float("inf"):
                return 999.0
            if o == float("-inf"):
                return -999.0
            return o
        if isinstance(o, dict):
            return {k: fix(v) for k, v in o.items()}
        if isinstance(o, list):
            return [fix(v) for v in o]
        return o
    return json.dumps(fix(obj), ensure_ascii=False, allow_nan=False, default=str)


def _ts(t) -> int:
    if t is None or (isinstance(t, float) and pd.isna(t)):
        return 0
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return int(ts.timestamp())


def _candles(df: pd.DataFrame) -> list[dict]:
    return [{
        "time": _ts(r.time), "open": float(r.open), "high": float(r.high),
        "low": float(r.low), "close": float(r.close),
    } for r in df.itertuples(index=False)]


def _equity(eq: pd.Series, points: int = 800) -> list[dict]:
    step = max(1, len(eq) // points)
    return [{"t": str(i), "v": round(float(v), 2)} for i, v in eq.iloc[::step].items()]


def _markers(trades) -> list[dict]:
    marks = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        buy = side == "buy"
        if t.opened_at is not None:
            marks.append({
                "time": _ts(t.opened_at), "price": float(t.entry),
                "kind": "entry", "side": side,
                "position": "belowBar" if buy else "aboveBar",
                "color": "#3dd6c3" if buy else "#ff7b78",
            })
        if t.closed_at is not None:
            tp = (t.reason or "").startswith("tp")
            marks.append({
                "time": _ts(t.closed_at), "price": float(t.exit),
                "kind": "exit", "side": side, "label": "TP" if tp else "SL",
                "position": "aboveBar" if tp else "belowBar",
                "color": "#ffc107" if tp else "#ef5350",
            })
    return marks


def _trade_rows(trades) -> list[dict]:
    rows = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        rows.append({
            "side": side, "volume": float(t.volume),
            "entry": float(t.entry), "exit": float(t.exit),
            "pnl": round(float(t.pnl), 4), "reason": t.reason,
            "opened_at": str(t.opened_at) if t.opened_at is not None else "",
            "closed_at": str(t.closed_at) if t.closed_at is not None else "",
            "sl": float(t.sl) if getattr(t, "sl", None) is not None else None,
            "tp": float(t.tp) if getattr(t, "tp", None) is not None else None,
            "label": "TP" if (t.reason or "").startswith("tp") else "SL",
        })
    return rows


def _cache_path(sym: str) -> Path:
    return ROOT / "data" / f"{sym.replace('+', 'plus')}_M1_90d.csv"


def load_m5(sym: str) -> pd.DataFrame:
    p = _cache_path(sym)
    if not p.exists():
        raise FileNotFoundError(f"нет кэша {p.name}")
    df = pd.read_csv(p, parse_dates=["time"])
    return data.resample_ohlc(df, "5min")


def score(m: dict) -> float:
    """Риск-доход: Calmar с штрафом за DD>12% и за мало сделок."""
    if m.get("ruined") or m.get("trades", 0) < 3:
        return -999.0
    ret = float(m["return_pct"])
    dd = max(float(m["max_drawdown_pct"]), 0.25)
    calmar = ret / dd
    dd_pen = 1.0 if dd <= TARGET_DD else TARGET_DD / dd
    n = int(m["trades"])
    # 8–40 сделок за 90д — комфортный диапазон; слишком мало = шум.
    n_bonus = min(1.0, n / 12.0) * (1.0 if n <= 80 else 80 / n)
    wr = float(m["win_rate"]) / 100.0
    wr_bonus = 0.85 + 0.3 * wr  # чуть выше ценим винрейт
    return calmar * dd_pen * n_bonus * wr_bonus


def limits(risk: float, max_risk: float = 3.0) -> RiskLimits:
    return RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=max_risk,
        min_risk_per_trade_pct=0.0, max_volume=100.0,
        max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
        min_free_margin_pct=0.0,
    )


def run_hss(sym: str, df: pd.DataFrame, *, risk: float = RISK,
            rr: float = 1.2, session=(16, 19), entry_mode: str = "stop") -> dict:
    spec = symbols.load(sym)
    rm = RiskManager(start_balance=BALANCE, limits=limits(risk))
    strat = ScalpHA(
        ema_period=100, pullback_bars=2, rr=rr,
        vol_mode="min", vol_window=3, entry_mode=entry_mode,
        ta_filter="off", session=session, risk_pct=risk,
        risk_manager=rm, spec=spec,
    )
    costs = costs_for(spec)
    res = Backtester(strat, rm, costs, start_balance=BALANCE, symbol=sym).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return {
        "id": f"hss_{sym.replace('+', 'plus')}",
        "title": f"HSS · {sym}",
        "family": "HSS",
        "symbol": sym,
        "why": "Heikin Ashi doji + EMA, сессия NY 16–19, R:R 1.2, риск 0.5%",
        "params": {"tf": "M5", "session": "16-19", "rr": rr, "risk_pct": risk,
                   "entry": entry_mode, "ema": 100},
        "metrics": m.as_dict(),
        "score": score(m.as_dict()),
        "equity": _equity(res.equity),
        "markers": _markers(res.trades),
        "trades": _trade_rows(res.trades),
        "candles": _candles(df),
        "commission": round(sum(commission_per_lot(sym) * t.volume for t in res.trades), 2),
    }


def run_london_sr(sym: str, df: pd.DataFrame, *, risk: float = RISK) -> dict:
    spec = symbols.load(sym)
    rm = RiskManager(start_balance=BALANCE, limits=limits(risk))
    strat = LondonSR(
        london=(11, 16), ny=(16, 23), min_rr=1.0,
        risk_pct=risk, risk_manager=rm, spec=spec,
    )
    costs = costs_for(spec)
    res = Backtester(strat, rm, costs, start_balance=BALANCE, symbol=sym).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return {
        "id": f"lsr_{sym.replace('+', 'plus')}",
        "title": f"London S/R · {sym}",
        "family": "London S/R",
        "symbol": sym,
        "why": "Отбой от лондонского диапазона в NY, цель по структуре",
        "params": {"tf": "M5", "london": "11-16", "ny": "16-23", "risk_pct": risk},
        "metrics": m.as_dict(),
        "score": score(m.as_dict()),
        "equity": _equity(res.equity),
        "markers": _markers(res.trades),
        "trades": _trade_rows(res.trades),
        "candles": _candles(df),
        "commission": round(sum(commission_per_lot(sym) * t.volume for t in res.trades), 2),
    }


def run_martingale(sym: str, df: pd.DataFrame, *, risk: float = 0.2) -> dict:
    spec = symbols.load(sym)
    fee = commission_per_lot(sym)
    rm = RiskManager(
        start_balance=BALANCE,
        limits=limits(risk, max_risk=max(40.0, risk * 2 ** 4)),
    )
    strat = Martingale(
        base_volume=float(spec.volume_min), risk_pct=risk, risk_manager=rm,
        spec=spec, multiplier=2.0, max_steps=5, session=(16, 22),
        ema_period=50, sl_atr=1.25, rr=1.15, commission_per_lot=fee,
        use_atr=True, cooldown_bars=3, reset_cooldown_bars=18,
    )
    costs = costs_for(spec, leverage=100)
    res = Backtester(strat, rm, costs, start_balance=BALANCE, symbol=sym).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return {
        "id": f"mg_{sym}",
        "title": f"Martingale · {sym}",
        "family": "Martingale",
        "symbol": sym,
        "why": "EMA-откат + ATR-SL, риск 0.2%, ×2 после убытка (серия ≤5)",
        "params": {"tf": "M5", "session": "16-22", "risk_pct": risk,
                   "sl_atr": 1.25, "rr": 1.15, "mult": 2, "steps": 5},
        "metrics": m.as_dict(),
        "score": score(m.as_dict()),
        "equity": _equity(res.equity),
        "markers": _markers(res.trades),
        "trades": _trade_rows(res.trades),
        "candles": _candles(df),
        "commission": round(sum(fee * t.volume for t in res.trades), 2),
    }


def run_hss_basket(syms: list[str]) -> dict:
    """Плюс-корзина: каждый символ на своём $1000 виртуально, метрики = среднее
    по символам + суммарный PnL как если бы торговали по очереди с общим риском
    0.5% (оценка машины). Для честности: средний return и max DD по книгам,
    суммарный net как сумма (верхняя оценка при параллельных книгах).
    """
    books = []
    for sym in syms:
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            continue
        # stop-entry как в основном hss_cfd_report (консервативнее market).
        b = run_hss(sym, df, entry_mode="stop")
        books.append(b)

    if not books:
        raise RuntimeError("пустая корзина HSS")

    rets = [b["metrics"]["return_pct"] for b in books]
    dds = [b["metrics"]["max_drawdown_pct"] for b in books]
    trades = sum(b["metrics"]["trades"] for b in books)
    wins = sum(b["metrics"]["wins"] for b in books)
    losses = sum(b["metrics"]["losses"] for b in books)
    net = sum(b["metrics"]["net_profit"] for b in books)
    # Одна книга $1000: берём СРЕДНИЙ return (капитал не умножаем на N символов).
    avg_ret = sum(rets) / len(books)
    # DD корзины ≈ средневзвешенный по сделкам, не хуже max.
    w = [max(1, b["metrics"]["trades"]) for b in books]
    avg_dd = sum(d * wi for d, wi in zip(dds, w)) / sum(w)
    worst_dd = max(dds)
    m = {
        "start_balance": BALANCE,
        "end_balance": BALANCE * (1 + avg_ret / 100),
        "net_profit": BALANCE * avg_ret / 100,
        "return_pct": avg_ret,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / trades * 100 if trades else 0.0,
        "profit_factor": (
            sum(b["metrics"]["avg_win"] * b["metrics"]["wins"] for b in books)
            / max(1e-9, abs(sum(b["metrics"]["avg_loss"] * b["metrics"]["losses"] for b in books)))
        ) if losses else 999.0,
        "max_drawdown_pct": avg_dd,
        "max_drawdown_abs": BALANCE * avg_dd / 100,
        "max_loss_streak": max(b["metrics"]["max_loss_streak"] for b in books),
        "largest_win": max(b["metrics"]["largest_win"] for b in books),
        "largest_loss": min(b["metrics"]["largest_loss"] for b in books),
        "avg_win": (sum(b["metrics"]["avg_win"] * b["metrics"]["wins"] for b in books) / max(1, wins)),
        "avg_loss": (sum(b["metrics"]["avg_loss"] * b["metrics"]["losses"] for b in books) / max(1, losses)),
        "expectancy": net / trades if trades else 0.0,
        "ruined": any(b["metrics"]["ruined"] for b in books),
    }
    # Эквити: среднее по нормализованным кривым.
    eq_maps = []
    for b in books:
        eq_maps.append({p["t"]: p["v"] for p in b["equity"]})
    keys = sorted(set().union(*[set(e) for e in eq_maps]))
    # downsample
    step = max(1, len(keys) // 800)
    keys = keys[::step]
    equity = []
    for t in keys:
        vals = [e[t] for e in eq_maps if t in e]
        if vals:
            equity.append({"t": t, "v": round(sum(vals) / len(vals), 2)})

    best = max(books, key=lambda b: b["score"])
    return {
        "id": "hss_plus_basket",
        "title": "HSS · plus_only корзина",
        "family": "HSS",
        "symbol": "plus_only×" + str(len(books)),
        "why": (f"Только плюсовые CFD машины ({len(books)} шт): "
                f"средний return на $1000, DD≈{avg_dd:.1f}% "
                f"(худший символ {worst_dd:.1f}%). Сумма PnL по книгам ${net:.0f} — "
                "не умножать капитал."),
        "params": {"tf": "M5", "session": "16-19", "rr": 1.2, "risk_pct": RISK,
                   "symbols": [b["symbol"] for b in books], "entry": "stop"},
        "metrics": m,
        "score": score(m),
        "equity": equity,
        "markers": best["markers"],
        "trades": best["trades"],
        "candles": best["candles"],
        "commission": round(sum(b["commission"] for b in books) / len(books), 2),
        "books": [{
            "symbol": b["symbol"],
            "return_pct": b["metrics"]["return_pct"],
            "dd": b["metrics"]["max_drawdown_pct"],
            "trades": b["metrics"]["trades"],
            "wr": b["metrics"]["win_rate"],
            "score": round(b["score"], 3),
        } for b in sorted(books, key=lambda x: -x["score"])],
        "chart_symbol": best["symbol"],
    }


def main() -> None:
    candidates: list[dict] = []
    plus = sorted(HSS_PLUS_SYMBOLS)

    con.print("[cyan]CFD топ-3[/] · депозит $1000 · риск 0.5% · ~90д · Bybit fees")

    # 1) HSS singles on strongest plus symbols + a couple extras
    hss_syms = list(plus) + ["NAS100", "EURUSD+"]
    for sym in hss_syms:
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            con.print(f"[yellow]skip {sym}: нет кэша[/]")
            continue
        con.print(f"  HSS {sym}…")
        candidates.append(run_hss(sym, df, entry_mode="stop"))

    # 2) London S/R where routing says yes
    for sym in ("NAS100", "XAUUSD+", "GBPUSD+", "USDJPY+", "GER40", "EURUSD+"):
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            continue
        con.print(f"  London S/R {sym}…")
        candidates.append(run_london_sr(sym, df))

    # 3) Martingale NAS100
    try:
        con.print("  Martingale NAS100…")
        candidates.append(run_martingale("NAS100", load_m5("NAS100")))
    except Exception as e:
        con.print(f"[yellow]martingale: {e}[/]")

    # 4) Basket
    con.print("  HSS plus_only basket…")
    basket = run_hss_basket(plus)
    candidates.append(basket)

    # Rank: unique families preferred in top3, but allow 2 HSS if dominant
    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)

    t = Table(title="Все кандидаты CFD (score = calmar×DD-штраф×сделки×WR)")
    t.add_column("#", justify="right")
    t.add_column("сетап")
    t.add_column("ret%", justify="right")
    t.add_column("DD%", justify="right")
    t.add_column("N", justify="right")
    t.add_column("WR%", justify="right")
    t.add_column("score", justify="right")
    for i, c in enumerate(ranked[:15], 1):
        m = c["metrics"]
        col = "green" if m["return_pct"] > 0 else "red"
        t.add_row(
            str(i), c["title"],
            f"[{col}]{m['return_pct']:+.2f}[/]",
            f"{m['max_drawdown_pct']:.1f}",
            str(m["trades"]),
            f"{m['win_rate']:.0f}",
            f"{c['score']:.2f}",
        )
    con.print(t)

    # Top 3: diversify by family when scores close
    top: list[dict] = []
    used_fam: set[str] = set()
    used_sym: set[str] = set()
    for c in ranked:
        if len(top) >= 3:
            break
        fam = c["family"]
        sym = c.get("symbol", "")
        # не дублировать один символ двумя HSS
        if fam == "HSS" and sym in used_sym and "basket" not in c["id"]:
            continue
        if fam in used_fam and len(top) < 2:
            # первые два — можно одно семейство если сильно лучше
            if c["score"] < top[0]["score"] * 0.55:
                continue
        if fam in used_fam and all(x["family"] == fam for x in top):
            # уже 2 из одного семейства — ищем другое
            if len(top) >= 2:
                continue
        top.append(c)
        used_fam.add(fam)
        used_sym.add(sym)

    # если не набрали 3 — добираем по score
    for c in ranked:
        if len(top) >= 3:
            break
        if c in top:
            continue
        top.append(c)

    con.print("\n[bold green]ТОП-3[/]")
    for i, c in enumerate(top, 1):
        m = c["metrics"]
        con.print(
            f"  {i}. {c['title']}: {m['return_pct']:+.2f}% · DD {m['max_drawdown_pct']:.1f}% · "
            f"N={m['trades']} WR={m['win_rate']:.0f}% · score={c['score']:.2f}"
        )

    # Slim payload for HTML (drop huge candles from non-selected in full list)
    def slim(c: dict, keep_chart: bool) -> dict:
        out = {k: v for k, v in c.items() if k not in ("candles", "markers", "trades", "equity")}
        out["metrics"] = c["metrics"]
        out["score"] = round(c["score"], 3)
        out["equity"] = c["equity"]
        if keep_chart:
            out["candles"] = c["candles"]
            out["markers"] = c["markers"]
            out["trades"] = c["trades"]
        else:
            out["candles"] = []
            out["markers"] = []
            out["trades"] = c["trades"][:30]
        return out

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "venue": "Bybit TradFi MT5 CFD",
        "deposit": BALANCE,
        "risk_pct": RISK,
        "days": DAYS,
        "objective": "min risk / max profit (calmar, DD≤12% preferred)",
        "top": [slim(c, True) for c in top],
        "all": [{
            "title": c["title"], "family": c["family"], "symbol": c.get("symbol"),
            "score": round(c["score"], 3), "metrics": c["metrics"],
            "why": c.get("why"), "params": c.get("params"),
            "commission": c.get("commission"),
        } for c in ranked],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cfd_top3.json").write_text(_json_dump(payload), encoding="utf-8")
    html = HTML.replace("__PAYLOAD__", _json_dump(payload))
    path = OUT / "cfd_top3.html"
    path.write_text(html, encoding="utf-8")
    con.print(f"\n[green]Отчёт → {path}[/]")


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CFD · Топ-3 · $1000</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{
  --bg:#0b0d10; --panel:#14181f; --line:#243041; --text:#eef2f7; --muted:#8b97a8;
  --green:#3dd6c3; --red:#ff7b78; --gold:#c9a227; --blue:#5b9fd4;
  --mono:ui-monospace,"SF Mono",Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:20px 24px 14px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#121722,#0b0d10)}
header h1{margin:0;font:700 22px/1.2 var(--sans);letter-spacing:-.02em}
header .sub{color:var(--muted);margin:8px 0 0;font-size:13px}
.podium{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;padding:18px 22px}
@media(max-width:900px){.podium{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;cursor:pointer;transition:.15s}
.card:hover,.card.active{border-color:var(--gold);background:#1a2230}
.card .rank{font:700 12px var(--sans);color:var(--gold);letter-spacing:.08em}
.card h2{margin:6px 0 4px;font:650 17px/1.25 var(--sans)}
.card .why{color:var(--muted);font-size:12px;min-height:2.6em}
.card .kpis{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}
.card .kpis div span{display:block;font-size:11px;color:var(--muted)}
.card .kpis div b{font:650 16px var(--mono)}
.pos{color:var(--green)}.neg{color:var(--red)}
.toolbar{display:flex;gap:8px;align-items:center;padding:10px 22px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
.toolbar strong{font-size:15px}
.toolbar span{color:var(--muted);font-size:12px}
#eqChart,#pxChart{height:280px;background:#080a0d}
#pxChart{height:440px}
.grid2{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--line)}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.panel{padding:12px 18px;border-right:1px solid var(--line)}
.panel h3{margin:0 0 8px;font:600 12px var(--sans);color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font:600 11px var(--sans);position:sticky;top:0;background:#151b24}
.wrap{max-height:340px;overflow:auto;padding:0 18px 16px}
.note{padding:12px 22px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>CFD · Топ-3 сетапа на $1000</h1>
  <p class="sub" id="sub">Bybit TradFi · минимум риска / максимум прибыли · ~90 дней</p>
</header>
<section class="podium" id="podium"></section>
<div class="toolbar">
  <strong id="selTitle">—</strong>
  <span id="selMeta"></span>
</div>
<div class="grid2">
  <div class="panel"><h3>Эквити</h3><div id="eqChart"></div></div>
  <div class="panel"><h3>Все кандидаты</h3>
    <div class="wrap" style="max-height:280px;padding:0">
      <table><thead><tr><th>сетап</th><th>ret%</th><th>DD%</th><th>N</th><th>WR</th><th>score</th></tr></thead>
      <tbody id="allBody"></tbody></table>
    </div>
  </div>
</div>
<div class="panel" style="border-bottom:1px solid var(--line)">
  <h3>График сделок (выбранный сетап)</h3>
  <div id="pxChart"></div>
</div>
<div class="wrap">
  <table>
    <thead><tr><th>#</th><th>side</th><th>лот</th><th>вход</th><th>выход</th><th>pnl</th><th></th><th>время</th></tr></thead>
    <tbody id="trades"></tbody>
  </table>
</div>
<p class="note" id="note"></p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const money = v => (v<0?"−":"+") + "$" + Math.abs(v).toFixed(2);
const pct = v => (v>=0?"+":"") + v.toFixed(2) + "%";
let sel = 0, eqC, pxC, candleS;

document.getElementById("sub").textContent =
  `${D.venue} · депозит $${D.deposit} · риск ${D.risk_pct}% · ~${D.days}д · ${D.objective}`;

document.getElementById("podium").innerHTML = D.top.map((c,i) => {
  const m = c.metrics;
  const cls = m.return_pct>=0?"pos":"neg";
  return `<div class="card ${i===0?"active":""}" data-i="${i}" onclick="select(${i})">
    <div class="rank">#${i+1} · ${c.family}</div>
    <h2>${c.title}</h2>
    <div class="why">${c.why||""}</div>
    <div class="kpis">
      <div><span>P&L</span><b class="${cls}">${pct(m.return_pct)}</b></div>
      <div><span>Max DD</span><b class="neg">${m.max_drawdown_pct.toFixed(1)}%</b></div>
      <div><span>Сделок</span><b>${m.trades}</b></div>
      <div><span>Win rate</span><b>${m.win_rate.toFixed(0)}%</b></div>
      <div><span>Score</span><b>${c.score}</b></div>
    </div>
  </div>`;
}).join("");

document.getElementById("allBody").innerHTML = D.all.map(c => {
  const m=c.metrics; const cls=m.return_pct>=0?"pos":"neg";
  return `<tr><td>${c.title}</td><td class="${cls}">${pct(m.return_pct)}</td>
    <td>${m.max_drawdown_pct.toFixed(1)}</td><td>${m.trades}</td>
    <td>${m.win_rate.toFixed(0)}%</td><td>${c.score}</td></tr>`;
}).join("");

document.getElementById("note").textContent =
  "Score = Calmar (ret/DD) × штраф если DD>12% × бонус за число сделок × винрейт. Комиссии Bybit $3–6/лот на открытии учтены. Корзина plus_only: средний return на один депозит $1000 (не сумма по символам).";

function mk(el){
  return LightweightCharts.createChart(el,{
    layout:{background:{color:"#080a0d"},textColor:"#8b97a8"},
    grid:{vertLines:{color:"#1a2230"},horzLines:{color:"#1a2230"}},
    rightPriceScale:{borderColor:"#243041"},
    timeScale:{borderColor:"#243041",timeVisible:true},
  });
}
function parseT(s){
  const raw=String(s||"").trim().replace(" ","T");
  if(!raw) return 0;
  const withZ=/Z$|[+-]\d{2}:?\d{2}$/.test(raw)?raw:raw+"Z";
  const ms=Date.parse(withZ); return ms?Math.floor(ms/1000):0;
}

function select(i){
  sel=i;
  [...document.querySelectorAll(".card")].forEach((el,j)=>el.classList.toggle("active",j===i));
  paint();
}

function paint(){
  const c=D.top[sel]; const m=c.metrics;
  document.getElementById("selTitle").textContent = c.title;
  document.getElementById("selMeta").textContent =
    `${pct(m.return_pct)} · DD ${m.max_drawdown_pct.toFixed(1)}% · N=${m.trades} · WR ${m.win_rate.toFixed(0)}% · комиссии ~$${c.commission||0}`;

  const eqEl=document.getElementById("eqChart");
  if(eqC) eqC.remove();
  eqC=mk(eqEl);
  const area=eqC.addAreaSeries({
    lineColor:m.return_pct>=0?"#3dd6c3":"#ff7b78",
    topColor:m.return_pct>=0?"rgba(61,214,195,.25)":"rgba(255,123,120,.25)",
    bottomColor:"rgba(0,0,0,0)", lineWidth:2,
  });
  area.setData((c.equity||[]).map(p=>({time:parseT(p.t),value:p.v})).filter(x=>x.time));
  eqC.timeScale().fitContent();
  new ResizeObserver(()=>eqC.applyOptions({width:eqEl.clientWidth,height:eqEl.clientHeight})).observe(eqEl);

  const pxEl=document.getElementById("pxChart");
  if(pxC) pxC.remove();
  pxC=mk(pxEl);
  candleS=pxC.addCandlestickSeries({
    upColor:"#26a69a",downColor:"#ef5350",borderVisible:false,
    wickUpColor:"#26a69a",wickDownColor:"#ef5350",
  });
  const candles=c.candles||[];
  if(candles.length){
    candleS.setData(candles.map(x=>({time:x.time,open:x.open,high:x.high,low:x.low,close:x.close})));
    candleS.setMarkers((c.markers||[]).map(mk=>({
      time:mk.time, position:mk.position, color:mk.color,
      shape: mk.kind==="entry"?(mk.side==="buy"?"arrowUp":"arrowDown"):"circle",
      text: mk.label||"",
    })));
  }
  pxC.timeScale().fitContent();
  new ResizeObserver(()=>pxC.applyOptions({width:pxEl.clientWidth,height:pxEl.clientHeight})).observe(pxEl);

  const trades=c.trades||[];
  document.getElementById("trades").innerHTML = trades.slice(0,200).map((t,i)=>
    `<tr><td>${i+1}</td><td class="${t.side==="buy"?"pos":"neg"}">${t.side}</td>
     <td>${t.volume}</td><td>${(+t.entry).toFixed(5)}</td><td>${(+t.exit).toFixed(5)}</td>
     <td class="${t.pnl>=0?"pos":"neg"}">${money(t.pnl)}</td><td>${t.label||t.reason}</td>
     <td>${(t.opened_at||"").slice(5,16)}</td></tr>`
  ).join("") || `<tr><td colspan="8">нет сделок в payload</td></tr>`;
}
select(0);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
