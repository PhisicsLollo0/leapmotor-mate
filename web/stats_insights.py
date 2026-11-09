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
import bisect
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


WINDOW_MIN = 5            # a window this long is long enough for the tank gauge to say something
_WINDOW_FILL = 0.8        # a trip's last window counts if it ran at least this share of WINDOW_MIN
_GEN_L_PER_MIN = 0.02     # burned faster than this, the generator ran in the window
_EV_L = 0.01              # burned less than this, the engine was off the whole window


def _level_at(fuel, t: float) -> Optional[float]:
    """The tank level at t, interpolated in the [(t, litres)] series; None outside it."""
    i = bisect.bisect_left(fuel, (t,))
    if i < len(fuel) and fuel[i][0] == t:
        return fuel[i][1]
    if i == 0 or i == len(fuel) or fuel[i][0] - fuel[i - 1][0] > _MAX_GAP_S:
        return None
    (t0, l0), (t1, l1) = fuel[i - 1], fuel[i]
    return l0 + (l1 - l0) * (t - t0) / (t1 - t0)


def windows(samples, fuel=None, minutes: int = WINDOW_MIN) -> list[dict]:
    """The trips cut into windows of `minutes`, each one judged as a whole.

    Ten seconds are too few for the tank gauge, which moves in hundredths of a litre, and a whole
    trip is too many: a drive that is half motorway and half country road lands in one band and the
    motorway disappears. A window of a few minutes is a stretch of road at one speed with enough
    fuel burned to measure. Never across two trips or a hole in the samples.

    `samples`: (trip_id, recorded_at, speed_kmh, power_kw) sorted by trip then time; the energy is
    the battery power × time (positive drawn, negative put back). `fuel`: [(t, litres)] sorted, the
    tank level over time, or None for a car without a tank."""
    out, cur, prev = [], None, None

    def close(w):
        if w and w["s"] >= minutes * 60 * _WINDOW_FILL and w["km"] > 0:
            litres = None
            if fuel is not None:
                l0, l1 = _level_at(fuel, w["t0"]), _level_at(fuel, w["t1"])
                litres = None if l0 is None or l1 is None else l0 - l1
            out.append({"trip": w["trip"], "t0": w["t0"], "km": w["km"], "kwh": w["kwh"],
                        "hours": w["s"] / 3600, "litres": litres, "tank": fuel is not None})

    for trip, ts, v, p in samples:
        if v is None or p is None:
            close(cur)
            cur = prev = None
            continue
        t, v, p = _ts(ts), float(v), float(p)
        if cur and prev and prev[0] == trip and 0 < t - prev[1] <= _MAX_GAP_S:
            dt = t - prev[1]
            cur["km"] += (v + prev[2]) / 2 * dt / 3600
            cur["kwh"] += (p + prev[3]) / 2 * dt / 3600
            cur["s"] += dt
            cur["t1"] = t
            if cur["s"] >= minutes * 60:
                close(cur)
                cur = None
        else:
            close(cur)
            cur = None
        if cur is None:
            cur = {"trip": trip, "t0": t, "t1": t, "s": 0.0, "km": 0.0, "kwh": 0.0}
        prev = (trip, t, v, p)
    close(cur)
    return out


def window_bands(wins, minutes: int = WINDOW_MIN, head_of=None) -> list[dict]:
    """The windows sorted into speed bands by their own average speed.

    A window that burned petrol is a generator window: its litres over its km give the L/100 km,
    and the battery it charged or drained meanwhile is stated beside it, never folded in. A window
    with the engine off is an electric one, wherever its trip ran the generator before or after.
    A window the engine started in burned real petrol and counts as a generator one; a window with
    a tank but no level recorded says nothing and is left out, as is a level that barely moved.
    `head_of` maps a merged piece to the trip it is shown under, so the trip list links the drive."""
    head_of = head_of or {}
    acc = {key: {"ev_km": 0.0, "ev_kwh": 0.0, "ev_n": 0, "gen_km": 0.0, "litres": 0.0,
                 "gen_kwh": 0.0, "gen_n": 0, "trips": {}} for _, _, key in SPEED_BANDS}
    for w in wins:
        a = acc[_band_of(w["km"] / w["hours"])]
        if w["litres"] is not None and w["litres"] >= _GEN_L_PER_MIN * minutes:
            a["gen_km"] += w["km"]
            a["litres"] += w["litres"]
            a["gen_kwh"] += w["kwh"]
            a["gen_n"] += 1
            kind = "gen_km"
        elif (w["litres"] is None and not w.get("tank")) or \
                (w["litres"] is not None and abs(w["litres"]) < _EV_L):
            a["ev_km"] += w["km"]
            a["ev_kwh"] += w["kwh"]
            a["ev_n"] += 1
            kind = "ev_km"
        else:
            continue
        trip = a["trips"].setdefault(head_of.get(w["trip"], w["trip"]),
                                     {"ev_km": 0.0, "gen_km": 0.0, "ev_kwh": 0.0, "litres": 0.0})
        trip[kind] += w["km"]
        if kind == "gen_km":
            trip["litres"] += w["litres"]
        else:
            trip["ev_kwh"] += w["kwh"]
    out = []
    for lo, hi, key in SPEED_BANDS:
        a = acc[key]
        out.append({
            "key": key, "lo": lo, "hi": hi,
            "km": round(a["ev_km"], 1), "kwh": round(a["ev_kwh"], 2), "windows": a["ev_n"],
            "kwh_100km": round(a["ev_kwh"] / a["ev_km"] * 100, 1)
            if a["ev_km"] >= _BAND_MIN_KM and a["ev_kwh"] > 0 else None,
            "gen_km": round(a["gen_km"], 1), "litres": round(a["litres"], 2), "gen_windows": a["gen_n"],
            "batt_kwh": round(a["gen_kwh"], 1),
            "l_100km": round(a["litres"] / a["gen_km"] * 100, 1) if a["gen_km"] >= _BAND_MIN_KM else None,
            # each trip's own rate in the band, so the one that drags the band's figure stands out
            "trips": [{"id": i, "ev_km": round(x["ev_km"], 1), "gen_km": round(x["gen_km"], 1),
                       "kwh_100km": round(x["ev_kwh"] / x["ev_km"] * 100, 1)
                       if x["ev_km"] >= 1 and x["ev_kwh"] > 0 else None,
                       "l_100km": round(x["litres"] / x["gen_km"] * 100, 1) if x["gen_km"] >= 1 else None}
                      for i, x in a["trips"].items()],
        })
    return out


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
        # Every trip, the generator ones included: their electric stretches are electric windows.
        samples = db.execute(
            "SELECT tp.trip_id, tp.recorded_at, tp.speed_kmh, tp.power_kw FROM trip_positions tp "
            "JOIN trips t ON t.id = tp.trip_id WHERE t.vehicle_id = COALESCE(?, t.vehicle_id) "
            "ORDER BY tp.trip_id, tp.recorded_at", (vid,)).fetchall()
        fuel_series = None
        if is_reev:
            fuel_series = [(_ts(r[0]), r[1]) for r in db.execute(
                "SELECT recorded_at, fuel_liters FROM positions WHERE fuel_liters IS NOT NULL "
                "AND vehicle_id = COALESCE(?, vehicle_id) ORDER BY recorded_at", (vid,))]
        heads = {r[0]: r[1] for r in db.execute(
            "SELECT id, merged_into_id FROM trips WHERE merged_into_id IS NOT NULL")}
        bands = window_bands(windows(samples, fuel_series), head_of=heads)
        start = {r["id"]: r["started_at"] for r in trips}
        for b in bands:
            for tr in b["trips"]:
                tr["started_at"] = start.get(tr["id"])
            b["trips"].sort(key=lambda x: x["started_at"] or "", reverse=True)
        city = next((b["kwh_100km"] for b in bands if b["key"] == "city"), None)
        for b in bands:
            b["vs_city_pct"] = (round((b["kwh_100km"] / city - 1) * 100)
                                if city and b["kwh_100km"] and b["key"] != "city" else None)
        out["speed"] = {"bands": bands, "fuel": is_reev, "minutes": WINDOW_MIN,
                        "max_kwh": max([b["kwh_100km"] or 0 for b in bands], default=0) or None,
                        "max_l": max([b["l_100km"] or 0 for b in bands], default=0) or None}
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
