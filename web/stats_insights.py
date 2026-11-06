"""Fork: the Statistics page's "your questions" section — five answers built on figures Mate already
keeps, each one a question the owner of a B10 REEV actually asked:

  1. what does driving cost me, and how much of it is petrol?         → drive_mix
  2. is the charger I use cheaper than the generator?                 → charge_prices
  3. how much more does the car drink at 90 or 130 than in town?      → speed_bands / fuel_speed_bands
  4. what does the cold cost — on the road and at the charger?        → heating_by_temp / charge_temps
  5. how fast does it really charge, and how long is 90 → 100 %?      → charge_curve_bands

Kept out of db_reader on purpose: that file is upstream's, and every line added there is a line to
re-resolve on the next rebase. The pure functions take plain rows and are tested without a database;
`get_insights` is the only thing that reads one, and only through db_reader's own figures."""
import logging
from datetime import datetime
from typing import Optional

import db_reader as dr

log = logging.getLogger(__name__)

# (low km/h, high km/h, key) — the bands the owner talks in: town, the 80–90 of a "statale",
# the 100–110 he drove in EV to measure it, and the motorway.
SPEED_BANDS = ((0, 50, "city"), (50, 90, "road"), (90, 115, "fast"), (115, 999, "motorway"))
_MAX_GAP_S = 60          # two samples further apart than this are a hole, not a segment
_BAND_MIN_KM = 5.0       # under this the band's rate is noise: km shown, rate withheld
_CURVE_MIN_SOC = 3.0     # a 10 % band a charge crossed less than this much of says nothing about it


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _band_of(v: float) -> str:
    for lo, hi, key in SPEED_BANDS:
        if lo <= v < hi:
            return key
    return SPEED_BANDS[-1][2]


def speed_bands(samples) -> list[dict]:
    """Electric consumption by speed, from the 10-second trip samples: each pair of consecutive
    samples of one trip is a segment, its energy is the mean battery power × time and its distance
    the mean speed × time. Standing still counts in the town band — the energy spent at a red light
    is town driving's.

    `samples`: (trip_id, recorded_at, speed_kmh, power_kw) sorted by trip then time. Only feed it
    trips the generator sat out: with the engine on, the pack's power is net of what the generator
    pushes in and stops describing the driving."""
    acc = {key: [0.0, 0.0, 0.0] for _, _, key in SPEED_BANDS}     # km, kWh, hours
    prev = None
    for trip, ts, v, p in samples:
        if v is None or p is None:
            prev = None
            continue
        cur = (trip, _ts(ts), float(v), float(p))
        if prev and prev[0] == trip:
            dt = cur[1] - prev[1]
            if 0 < dt <= _MAX_GAP_S:
                vm, pm, h = (prev[2] + cur[2]) / 2, (prev[3] + cur[3]) / 2, dt / 3600
                a = acc[_band_of(vm)]
                a[0] += vm * h
                a[1] += pm * h
                a[2] += h
        prev = cur
    out = []
    for lo, hi, key in SPEED_BANDS:
        km, kwh, h = acc[key]
        out.append({"key": key, "lo": lo, "hi": hi, "km": round(km, 1), "kwh": round(kwh, 2),
                    "minutes": round(h * 60),
                    "kwh_100km": round(kwh / km * 100, 1) if km >= _BAND_MIN_KM and kwh > 0 else None})
    return out


def fuel_speed_bands(trips) -> list[dict]:
    """Petrol by speed — per TRIP, not per sample: the fuel counter moves in steps far coarser than
    ten seconds, so a trip lands in the band of its average speed. `trips`: dicts with km, minutes
    and l_100km (generator trips only). Weighted by distance, like every L/100 km in Mate."""
    acc = {key: [0.0, 0.0, 0] for _, _, key in SPEED_BANDS}       # km, litres, trips
    for t in trips:
        km, mins, l100 = t.get("km") or 0, t.get("minutes") or 0, t.get("l_100km")
        if km <= 0 or mins <= 0 or not l100:
            continue
        a = acc[_band_of(km / (mins / 60))]
        a[0] += km
        a[1] += l100 * km / 100
        a[2] += 1
    return [{"key": key, "km": round(acc[key][0], 1), "trips": acc[key][2],
             "l_100km": round(acc[key][1] / acc[key][0] * 100, 1) if acc[key][0] >= _BAND_MIN_KM else None}
            for _, _, key in SPEED_BANDS]


def charge_prices(charges, breakeven: Optional[float]) -> dict:
    """What each charge cost per kWh, against the break-even of `reev_breakeven_kwh_price`.
    `charges`: dicts with ts, cost, kwh (the BILLED kWh, `_billed_kwh`), place. Unpriced ones stay
    out — unknown, not free."""
    pts = []
    for c in charges:
        if c.get("cost") is None or c["cost"] < 0 or not c.get("kwh") or c["kwh"] <= 0:
            continue
        rate = c["cost"] / c["kwh"]
        pts.append({"ts": c["ts"], "place": c.get("place"), "kwh": round(c["kwh"], 1),
                    "cost": round(c["cost"], 2), "eur_kwh": round(rate, 3),
                    "cheaper": (rate < breakeven) if breakeven else None})
    kwh = sum(p["kwh"] for p in pts)
    avg = sum(p["cost"] for p in pts) / kwh if kwh else None
    scale = max([p["eur_kwh"] for p in pts] + [breakeven or 0]) * 1.15 if pts else None
    return {"points": pts, "avg_eur_kwh": round(avg, 3) if avg else None, "breakeven": breakeven,
            "scale_max": round(scale, 3) if scale else None,
            "cheaper_n": sum(1 for p in pts if p["cheaper"])}


def drive_mix(total_km: float, engine_km: float, fuel_cost: Optional[float],
              elec_kwh_100km: Optional[float], avg_eur_kwh: Optional[float]) -> Optional[dict]:
    """How the kilometres split between the battery and the generator, and what the generator ones
    WOULD have cost on the plug: the petrol actually burned on them (litres × the tank's blended
    €/L, `reev_fuel_summary`) against the same kilometres at the owner's own electric consumption
    and his own average €/kWh — never a tariff made up here. A ceiling, and the page says so: it
    assumes every generator kilometre could have been driven on a charge, which a motorway day
    longer than the battery cannot."""
    if not total_km or total_km < 0.5:
        return None
    engine_km = min(max(engine_km or 0, 0), total_km)
    ev_km = total_km - engine_km
    out = {"total_km": round(total_km), "ev_km": round(ev_km), "engine_km": round(engine_km),
           "ev_pct": round(ev_km / total_km * 100), "fuel_cost": None, "plug_cost": None, "extra": None}
    if fuel_cost and engine_km and elec_kwh_100km and avg_eur_kwh:
        plug = engine_km * elec_kwh_100km / 100 * avg_eur_kwh
        out.update(fuel_cost=round(fuel_cost), plug_cost=round(plug), extra=round(fuel_cost - plug))
    return out


def heating_by_temp(trips) -> list[dict]:
    """How much of the electric consumption went to the climate, by outside temperature (5 °C bands).
    `trips`: dicts with temp, km, ec_kwh (the car's metered kWh) and ec_ac (its climate share)."""
    acc: dict[int, list] = {}
    for t in trips:
        if t.get("temp") is None or not t.get("ec_kwh") or not t.get("km"):
            continue
        lo = int(t["temp"] // 5 * 5)
        a = acc.setdefault(lo, [0.0, 0.0, 0.0, 0])
        a[0] += t["km"]
        a[1] += t["ec_kwh"]
        a[2] += t.get("ec_ac") or 0
        a[3] += 1
    return [{"lo": lo, "hi": lo + 5, "trips": a[3], "km": round(a[0], 1),
             "kwh_100km": round(a[1] / a[0] * 100, 1),
             "heat_100km": round(a[2] / a[0] * 100, 1),
             "heat_pct": round(a[2] / a[1] * 100) if a[1] else None}
            for lo, a in sorted(acc.items())]


def charge_curve_bands(curves) -> list[dict]:
    """Minutes per 10 % of charge, by SoC band, over every curve given (DC sessions). Each pair of
    consecutive samples puts its time and its SoC gain into the band it started in; a band reads as
    total minutes ÷ total SoC gained × 10, so a session that only grazed a band weighs little.
    `curves`: dicts with times, soc, power (get_charge_power_curve)."""
    acc = {b: [0.0, 0.0, 0.0, 0.0, set()] for b in range(10)}   # min, soc, kW·min, min w/ power, charges
    for i, c in enumerate(curves):
        ts, soc, pw = c.get("times") or [], c.get("soc") or [], c.get("power") or []
        for j in range(1, len(ts)):
            if soc[j - 1] is None or soc[j] is None:
                continue
            dt = (_ts(ts[j]) - _ts(ts[j - 1])) / 60
            if not (0 < dt <= _MAX_GAP_S * 3 / 60):
                continue
            b = min(int(soc[j - 1] // 10), 9)
            a = acc[b]
            a[0] += dt
            a[1] += max(soc[j] - soc[j - 1], 0)
            if j < len(pw) and pw[j] is not None:
                a[2] += pw[j] * dt
                a[3] += dt
            a[4].add(i)
    out = []
    for b in range(10):
        m, s, kwm, pm, ids = acc[b]
        ok = s >= _CURVE_MIN_SOC
        out.append({"lo": b * 10, "hi": b * 10 + 10, "charges": len(ids),
                    "min_per_10": round(m / s * 10, 1) if ok else None,
                    "kw": round(kwm / pm, 1) if ok and pm else None})
    return out


def charge_temps(curves) -> list[dict]:
    """One row per DC session: the coldest cell at the start and the mean power while the pack sat
    between 20 and 80 % — the stretch every session shares, so a cold start shows as a slow middle
    rather than as a short session."""
    out = []
    for c in curves:
        temps = [t for t in (c.get("battery_temp") or []) if t is not None]
        soc, pw = c.get("soc") or [], c.get("power") or []
        mid = [p for s, p in zip(soc, pw) if s is not None and p and 20 <= s <= 80]
        if not temps or len(mid) < 5:
            continue
        out.append({"ts": c.get("ts"), "temp_start": round(temps[0], 1),
                    "kw_mid": round(sum(mid) / len(mid), 1)})
    return sorted(out, key=lambda r: r["temp_start"])


def _sum(curve_bands, lo, hi) -> Optional[int]:
    vals = [b["min_per_10"] for b in curve_bands if lo <= b["lo"] < hi]
    return round(sum(vals)) if vals and all(v is not None for v in vals) else None


# ── the one reader ───────────────────────────────────────────────────────────────────────────────

def _engine_trip_ids(rows) -> set:
    """The tank test `reev_breakeven_kwh_price` applies: a level that fell means the generator ran."""
    out = set()
    for r in rows:
        burned = ((r["fuel_start_l"] or 0) - (r["fuel_end_l"] or 0)) if r["fuel_start_l"] is not None else \
                 ((r["fuel_start_pct"] or 0) - (r["fuel_end_pct"] or 0))
        if burned > 0:
            out.add(r["id"])
    return out


def _charges(db) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM charges WHERE vehicle_id = COALESCE(?, vehicle_id) AND ended_at IS NOT NULL "
        "ORDER BY started_at", (dr._current_vehicle_id(),)).fetchall()
    out = []
    for pieces in dr._charges_as_shown(rows):
        pieces = [dict(p) for p in pieces]
        head = min(pieces, key=lambda p: p["started_at"])
        priced = [p["cost"] for p in pieces if p["cost"] is not None]
        out.append({"id": head["id"], "ts": head["started_at"], "type": head.get("charge_type"),
                    "place": head.get("charging_place_name") or head.get("location_name"),
                    "cost": sum(priced) if priced else None,
                    "kwh": sum(dr._billed_kwh(p) or 0 for p in pieces)})
    return out


def get_insights(is_reev: bool, breakeven: Optional[dict], fuel: Optional[dict],
                 total: Optional[dict], efftemp: Optional[dict]) -> dict:
    """Everything the section draws. Takes the figures the page has already computed (break-even,
    fuel summary, totals, the temperature series) so none of them is worked out twice, and each block
    fails on its own: a reader that raises costs its card, never the page."""
    db = dr._get()
    out: dict = {}
    vid = dr._current_vehicle_id()

    try:
        trips = db.execute(
            "SELECT id, started_at, distance_km, duration_min, ec_kwh, ec_ac, ec_stable, "
            "outside_temp_start_c, outside_temp_end_c, fuel_start_l, fuel_end_l, fuel_start_pct, fuel_end_pct "
            "FROM trips WHERE vehicle_id = COALESCE(?, vehicle_id) AND ended_at IS NOT NULL "
            "AND merged_into_id IS NULL", (vid,)).fetchall()
        engine_ids = _engine_trip_ids(trips) if is_reev else set()
    except Exception:
        log.exception("insights: trips")
        trips, engine_ids = [], set()

    charges = []
    try:
        charges = _charges(db)
        be = breakeven["breakeven_kwh"] if breakeven else None
        out["prices"] = charge_prices(charges, be)
    except Exception:
        log.exception("insights: prices")

    try:
        total_km = (total or {}).get("total_km") or sum(r["distance_km"] or 0 for r in trips)
        engine_km = (fuel or {}).get("engine_km") or 0
        out["mix"] = drive_mix(total_km, engine_km, (fuel or {}).get("total_cost"),
                               (breakeven or {}).get("elec_kwh_100km"),
                               (out.get("prices") or {}).get("avg_eur_kwh"))
    except Exception:
        log.exception("insights: mix")

    try:
        ev_ids = [r["id"] for r in trips if r["id"] not in engine_ids]
        samples = []
        if ev_ids:
            samples = db.execute(
                f"SELECT trip_id, recorded_at, speed_kmh, power_kw FROM trip_positions "
                f"WHERE trip_id IN ({','.join('?' * len(ev_ids))}) ORDER BY trip_id, recorded_at",
                ev_ids).fetchall()
        bands = speed_bands(samples)
        fuel_bands = None
        if is_reev and efftemp and efftemp.get("fuel_points"):
            dur = {r["id"]: r["duration_min"] for r in trips}
            fuel_bands = fuel_speed_bands(
                [{"km": p["km"], "minutes": dur.get(p["id"]), "l_100km": p["l"]}
                 for p in efftemp["fuel_points"]])
        city = next((b["kwh_100km"] for b in bands if b["key"] == "city"), None)
        for b in bands:
            b["vs_city_pct"] = (round((b["kwh_100km"] / city - 1) * 100)
                                if city and b["kwh_100km"] and b["key"] != "city" else None)
        out["speed"] = {"elec": bands, "fuel": fuel_bands,
                        "max_kwh": max([b["kwh_100km"] or 0 for b in bands], default=0) or None,
                        "max_l": max([b["l_100km"] or 0 for b in fuel_bands or []], default=0) or None}
    except Exception:
        log.exception("insights: speed")

    curves = []
    try:
        for c in charges:
            if c["type"] != "DC":
                continue
            cv = dr.get_charge_power_curve(c["id"])
            if cv.get("times"):
                cv["ts"] = c["ts"]
                curves.append(cv)
        cb = charge_curve_bands(curves)
        out["curve"] = {"bands": cb, "sessions": len(curves),
                        "max_min": max([b["min_per_10"] or 0 for b in cb], default=0) or None,
                        "t10_80": _sum(cb, 10, 80), "t80_100": _sum(cb, 80, 100),
                        "t90_100": _sum(cb, 90, 100)}
    except Exception:
        log.exception("insights: curve")

    try:
        ht = []
        for r in trips:
            if r["id"] in engine_ids or not r["ec_stable"]:
                continue
            temps = [v for v in (r["outside_temp_start_c"], r["outside_temp_end_c"]) if v is not None]
            if not temps or (r["distance_km"] or 0) < 3:
                continue
            ht.append({"temp": sum(temps) / len(temps), "km": r["distance_km"],
                       "ec_kwh": r["ec_kwh"], "ec_ac": r["ec_ac"]})
        out["cold"] = {"bands": heating_by_temp(ht), "charges": charge_temps(curves)}
    except Exception:
        log.exception("insights: cold")

    return out
