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
):
    name = route["strategy"]
    sess = route.get("session")
    if isinstance(sess, list):
        sess = (int(sess[0]), int(sess[1]))
    kw = dict(risk_pct=risk, risk_manager=risk_manager, spec=spec)

    if name == "HSS":
        return ScalpHA(rr=rr, pullback_bars=pullback, session=sess,
                       vol_mode="min", vol_window=2, entry_mode="stop", **kw)
    if name in ("London S/R", "LondonSR"):
        return LondonSR(london=london, ny=sess or CRYPTO_NY, min_rr=rr, **kw)
    if name in ("Breakout", "London Breakout"):
        return LondonBreakout(rr=max(rr, 1.5), session=sess, **kw)
    if name == "Squeeze":
        return Squeeze(min_rr=max(rr, 1.8), session=sess, **kw)
    if name in ("Flow", "Session Flow"):
        f = dict(flow or {})
        setups = route.get("setups") or f.get("setups", "blend")
        return SessionFlow(
            setups=setups,
            gate_regime=bool(f.get("gate_regime", True)),
            rr=float(f.get("rr", 1.0)),
            session=sess,
            **kw,
        )
    if name in ("Playbook", "All"):
        return crypto_playbook(
            tf=route.get("tf", "5m"), rr=rr, risk=risk,
            risk_manager=risk_manager, spec=spec, pullback=pullback,
            london=london, flow=flow, session=sess,
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
) -> Portfolio:
    """Единая машина: все рабочие куски на одном ТФ, позиция всё ещё одна.

    Больше сделок получается за счёт разных режимов на разных барах,
    не за счёт одновременных позиций.
    """
    kw = dict(risk_pct=risk, risk_manager=risk_manager, spec=spec)
    f = dict(flow or {})
    pf = Portfolio()
    pf.name = "playbook"
    m1 = tf in ("1m", "M1")
    if m1:
        pf.add(ScalpHA(rr=rr, pullback_bars=pullback, session=session,
                       vol_mode="min", vol_window=2, entry_mode="stop", **kw), "HSS")
        pf.add(LondonSR(london=london, ny=session or CRYPTO_NY, min_rr=rr, **kw),
               "London S/R")
        return pf
    pf.add(SessionFlow(
        setups=f.get("setups", "blend"),
        gate_regime=bool(f.get("gate_regime", True)),
        rr=float(f.get("rr", 1.0)),
        session=session,
        **kw,
    ), "Flow")
    if f.get("squeeze"):
        pf.add(Squeeze(min_rr=float(f.get("squeeze_rr", 2.0)), session=session, **kw), "Squeeze")
    pf.add(LondonSR(london=london, ny=session or CRYPTO_NY,
                    min_rr=rr, **kw), "London S/R")
    return pf


def label_for(route: dict) -> str:
    extra = route.get("setups")
    if extra and extra not in ("blend", "all", "*"):
        return f"{route['strategy']} {route['tf']} {extra}"
    return f"{route['strategy']} {route['tf']}"
