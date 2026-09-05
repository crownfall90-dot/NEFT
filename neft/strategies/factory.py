"""Сборка стратегии из маршрута. Один вход для бэктеста, форварда и счёта."""
from __future__ import annotations

from neft.core.portfolio import Portfolio
from neft.strategies.london_breakout import LondonBreakout
from neft.strategies.london_sr import LondonSR
from neft.strategies.scalp_ha import ScalpHA
from neft.strategies.session_flow import SessionFlow
from neft.strategies.squeeze import Squeeze

# Binance отдаёт UTC. London S/R в машине MT5 живёт в UTC+3 — для крипты
# те же окна сдвигаем на 3 часа назад.
CRYPTO_LONDON = (8, 13)
CRYPTO_NY = (13, 21)


def crypto_strategy(
    route: dict,
    *,
    rr: float,
    risk: float,
    risk_manager,
    spec,
    pullback: int = 2,
    london: tuple[int, int] = CRYPTO_LONDON,
    flow: dict | None = None,
    rr_by_strategy: dict | None = None,
):
    """rr_by_strategy — R:R, заданный в панели отдельно на каждую стратегию.

    Раньше все стратегии получали один общий rr (из настроек HSS), а Squeeze
    и Breakout ещё и зажимали его снизу через max(...) — то есть выбранное
    в панели значение для них молча игнорировалось. Теперь у каждой свой,
    а общий rr остаётся запасным вариантом, если своего не задано.
    """
    name = route["strategy"]
    sess = route.get("session")
    if isinstance(sess, list):
        sess = (int(sess[0]), int(sess[1]))
    kw = dict(risk_pct=risk, risk_manager=risk_manager, spec=spec)

    def rr_for(key: str, fallback: float) -> float:
        """R:R стратегии из панели; 0/пусто — берём запасной."""
        raw = (rr_by_strategy or {}).get(key)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = 0.0
        return val if val > 0 else fallback

    if name == "HSS":
        return ScalpHA(rr=rr_for("hss", rr), pullback_bars=pullback, session=sess,
                       vol_mode="min", vol_window=3, entry_mode="market",
                       **kw)
    if name in ("London S/R", "LondonSR"):
        return LondonSR(london=london, ny=sess or CRYPTO_NY,
                        min_rr=rr_for("london_sr", rr), **kw)
    if name in ("Breakout", "London Breakout"):
        return LondonBreakout(rr=rr_for("breakout", 2.0), session=sess, **kw)
    if name == "Squeeze":
        return Squeeze(min_rr=rr_for("squeeze", 2.0), session=sess, **kw)
    if name in ("Flow", "Session Flow"):
        f = dict(flow or {})
        setups = route.get("setups") or f.get("setups", "blend")
        return SessionFlow(
            setups=setups,
            gate_regime=bool(f.get("gate_regime", True)),
            rr=rr_for("session_flow", float(f.get("rr", 1.0))),
            session=sess,
            **kw,
        )
    if name in ("Playbook", "All"):
        return crypto_playbook(
            tf=route.get("tf", "5m"), rr=rr, risk=risk,
            risk_manager=risk_manager, spec=spec, pullback=pullback,
            london=london, flow=flow, session=sess,
            rr_by_strategy=rr_by_strategy,
        )
    raise ValueError(f"неизвестная стратегия маршрута: {name}")


def crypto_playbook(
    *,
    tf: str,
    rr: float,
    risk: float,
    risk_manager,
    spec,
    pullback: int = 2,
    london: tuple[int, int] = CRYPTO_LONDON,
    flow: dict | None = None,
    session=None,
    rr_by_strategy: dict | None = None,
) -> Portfolio:
    """Единая машина: все рабочие куски на одном ТФ, позиция всё ещё одна.

    Больше сделок получается за счёт разных режимов на разных барах,
    не за счёт одновременных позиций.
    """
    kw = dict(risk_pct=risk, risk_manager=risk_manager, spec=spec)
    f = dict(flow or {})
    rrs = rr_by_strategy or {}

    def rr_for(key: str, fallback: float) -> float:
        try:
            val = float(rrs.get(key))
        except (TypeError, ValueError):
            val = 0.0
        return val if val > 0 else fallback

    pf = Portfolio()
    pf.name = "playbook"
    m1 = tf in ("1m", "M1")
    if m1:
        pf.add(ScalpHA(rr=rr_for("hss", rr), pullback_bars=pullback, session=session,
                       vol_mode="min", vol_window=3, entry_mode="market",
                       **kw), "HSS")
        pf.add(LondonSR(london=london, ny=session or CRYPTO_NY,
                        min_rr=rr_for("london_sr", rr), **kw), "London S/R")
        return pf
    pf.add(SessionFlow(
        setups=f.get("setups", "blend"),
        gate_regime=bool(f.get("gate_regime", True)),
        rr=rr_for("session_flow", float(f.get("rr", 1.0))),
        session=session,
        **kw,
    ), "Flow")
    if f.get("squeeze"):
        pf.add(Squeeze(min_rr=rr_for("squeeze", float(f.get("squeeze_rr", 2.0))),
                       session=session, **kw), "Squeeze")
    pf.add(LondonSR(london=london, ny=session or CRYPTO_NY,
                    min_rr=rr_for("london_sr", rr), **kw), "London S/R")
    return pf


def label_for(route: dict) -> str:
    extra = route.get("setups")
    if extra and extra not in ("blend", "all", "*"):
        return f"{route['strategy']} {route['tf']} {extra}"
    return f"{route['strategy']} {route['tf']}"
