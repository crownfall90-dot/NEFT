"""Сборка CFD-стратегий для бэктеста и панели."""
from __future__ import annotations

from neft.strategies.apex_shot import ApexShot
from neft.strategies.coil_break import CoilBreak
from neft.strategies.london_breakout import LondonBreakout
from neft.strategies.london_sr import LondonSR
from neft.strategies.nano_fade import NanoFade
from neft.strategies.orb_pulse import OrbPulse
from neft.strategies.pulse_clip import PulseClip
from neft.strategies.scalp_ha import ScalpHA
from neft.strategies.session_flow import SessionFlow
from neft.strategies.slow_tide import SlowTide
from neft.strategies.sonik_open import SonikPulse
from neft.strategies.squeeze import Squeeze
from neft.strategies.vwap_snap import VwapSnap

# Ключ → метаданные для панели и бэктеста.
CFD_CATALOG: dict[str, dict] = {
    "hss": {
        "name": "HSS · Heikin Ashi scalp",
        "file": "neft/strategies/scalp_ha.py",
        "default_tf": "M1",
        "what": "Тренд по EMA100, чистый откат, вход market на doji. RR фиксированный.",
    },
    "sonik_pulse": {
        "name": "SonikPulse",
        "file": "neft/strategies/sonik_open.py",
        "default_tf": "M1",
        "what": "Deep pullback + EMA slope + pivot. Лондон 07–09 UTC+3.",
    },
    "apex_shot": {
        "name": "ApexShot",
        "file": "neft/strategies/apex_shot.py",
        "default_tf": "M5",
        "what": "Редкие A+ сетапы: сжатие ATR, engulfing у EMA, RR ≥ 2.",
    },
    "orb_pulse": {
        "name": "OrbPulse · NY OR",
        "file": "neft/strategies/orb_pulse.py",
        "default_tf": "M5",
        "what": "Пробой opening range NY (16:30), рыночный вход, RR ~1.8.",
    },
    "pulse_clip": {
        "name": "PulseClip",
        "file": "neft/strategies/pulse_clip.py",
        "default_tf": "M5",
        "what": "Частые клипы по EMA34, малый SL/TP в ATR, лимит лузов в день.",
    },
    "vwap_snap": {
        "name": "VwapSnap",
        "file": "neft/strategies/vwap_snap.py",
        "default_tf": "M5",
        "what": "Возврат к сессионному VWAP после перерастяжения.",
    },
    "slow_tide": {
        "name": "SlowTide",
        "file": "neft/strategies/slow_tide.py",
        "default_tf": "M5",
        "what": "Медленный тренд EMA21/55, откат к медленной EMA.",
    },
    "coil_break": {
        "name": "CoilBreak · Asia coil",
        "file": "neft/strategies/coil_break.py",
        "default_tf": "M5",
        "what": "Азиатское сжатие, выход в лондонском окне.",
    },
    "nano_fade": {
        "name": "NanoFade",
        "file": "neft/strategies/nano_fade.py",
        "default_tf": "M5",
        "what": "Микро-разворот после серии однонаправленных свечей.",
    },
    "london_sr": {
        "name": "London S/R",
        "file": "neft/strategies/london_sr.py",
        "default_tf": "M1",
        "what": "Уровни лондона, вход после слома структуры в NY.",
    },
    "breakout": {
        "name": "London Breakout",
        "file": "neft/strategies/london_breakout.py",
        "default_tf": "M5",
        "what": "Бокс лондона, пробой после 09:30 ET.",
    },
    "squeeze": {
        "name": "Squeeze",
        "file": "neft/strategies/squeeze.py",
        "default_tf": "M5",
        "what": "Треугольник, вход на сломе свинга.",
    },
    "session_flow": {
        "name": "Flow · 6 сетапов",
        "file": "neft/strategies/session_flow.py",
        "default_tf": "M5",
        "what": "asian_sweep, ORB, VWAP, EMA pull — сессионные сетапы.",
    },
}

CFD_KEYS = tuple(CFD_CATALOG.keys())


def _session(raw, default=None):
    if raw is None:
        return default
    if isinstance(raw, list) and len(raw) >= 2:
        return (int(raw[0]), int(raw[1]))
    if isinstance(raw, tuple) and len(raw) >= 2:
        return (int(raw[0]), int(raw[1]))
    return default


def _float(raw, default: float) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def strategy_tf(key: str, cfg: dict) -> str:
    st = (cfg.get("strategies") or {}).get(key) or {}
    raw = str(st.get("tf") or CFD_CATALOG.get(key, {}).get("default_tf") or "M5")
    mapping = {"1m": "M1", "5m": "M5", "15m": "M15", "1h": "H1",
               "m1": "M1", "m5": "M5", "m15": "M15"}
    return mapping.get(raw.lower(), raw.upper())


def build_cfd_strategy(
    key: str,
    *,
    cfg: dict,
    risk: float,
    risk_manager,
    spec,
    symbol: str | None = None,
):
    st_all = cfg.get("strategies") or {}
    st = st_all.get(key) or {}
    kw = dict(risk_pct=risk, risk_manager=risk_manager, spec=spec)

    if key == "hss":
        session = None if st.get("all_day") or cfg.get("hss_24h") else _session(
            st.get("session") or cfg.get("hss_session"), (16, 19))
        return ScalpHA(
            ema_period=int(st.get("ema") or 100),
            pullback_bars=int(st.get("pullback") or cfg.get("pullback_bars") or 2),
            rr=_float(st.get("rr") or cfg.get("rr"), 1.0),
            vol_mode=str(st.get("vol_mode") or "min"),
            vol_window=3,
            entry_mode=str(st.get("entry_mode") or "market"),
            require_matching_doji=bool(st.get("matching_doji")),
            ta_filter=str(st.get("ta_filter") or "off"),
            session=session,
            **kw,
        )

    if key == "sonik_pulse":
        return SonikPulse(
            sl_points=_float(st.get("sl_points"), 2.5),
            tp_points=_float(st.get("tp_points"), 4.0),
            **kw,
        )

    if key == "apex_shot":
        return ApexShot(
            session=_session(st.get("session"), (15, 20)),
            rr=_float(st.get("rr"), 2.0),
            **kw,
        )

    if key == "orb_pulse":
        return OrbPulse(rr=_float(st.get("rr"), 1.8), **kw)

    if key == "pulse_clip":
        return PulseClip(
            session=_session(st.get("session"), (16, 20)),
            **kw,
        )

    if key == "vwap_snap":
        return VwapSnap(
            session=_session(st.get("session"), (16, 20)),
            min_rr=_float(st.get("min_rr") or st.get("rr"), 1.5),
            **kw,
        )

    if key == "slow_tide":
        return SlowTide(
            session=_session(st.get("session"), (15, 20)),
            rr=_float(st.get("rr"), 2.0),
            **kw,
        )

    if key == "coil_break":
        return CoilBreak(rr=_float(st.get("rr"), 1.7), **kw)

    if key == "nano_fade":
        return NanoFade(
            session=_session(st.get("session"), (16, 20)),
            rr=_float(st.get("rr"), 1.25),
            **kw,
        )

    if key == "london_sr":
        return LondonSR(
            london=_session(st.get("london") or cfg.get("london"), (11, 16)),
            ny=_session(st.get("ny") or cfg.get("ny"), (16, 23)),
            min_rr=_float(st.get("min_rr") or st.get("rr"), 1.0),
            **kw,
        )

    if key == "breakout":
        return LondonBreakout(rr=_float(st.get("rr"), 2.0), **kw)

    if key == "squeeze":
        return Squeeze(
            min_rr=_float(st.get("min_rr") or st.get("rr"), 2.0),
            min_taps=int(st.get("min_taps") or 2),
            **kw,
        )

    if key == "session_flow":
        return SessionFlow(
            setups=st.get("setups") or "blend",
            gate_regime=bool(st.get("gate_regime", True)),
            rr=_float(st.get("rr"), 1.0),
            session=_session(st.get("session")),
            **kw,
        )

    raise ValueError(f"неизвестная CFD-стратегия: {key}")
