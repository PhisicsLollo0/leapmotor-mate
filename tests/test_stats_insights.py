"""Fork: the Statistics page's "your questions" section (web/stats_insights.py).

Shapes taken from the owner's own B10 REEV, Oct–Nov 2026: DC sessions at one public column that
fall from ~31 kW to ~6 kW above 90 %, town trips at ~14 kWh/100 km, a motorway day to Padova."""
from datetime import datetime, timedelta, timezone

import stats_insights as si

T0 = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)


def _iso(s):
    return (T0 + timedelta(seconds=s)).isoformat()


def test_speed_bands_integrate_power_and_speed_per_band():
    # 10 min at a steady 36 km/h drawing 6 kW, then 10 min at 100 km/h drawing 18 kW
    samples = [(1, _iso(i * 10), 36.0, 6.0) for i in range(61)]
    samples += [(2, _iso(i * 10), 100.0, 18.0) for i in range(61)]
    b = {x["key"]: x for x in si.speed_bands(samples)}
    assert b["city"]["km"] == 6.0 and b["city"]["kwh_100km"] == 16.7      # 1 kWh over 6 km
    assert b["fast"]["km"] == 16.7 and b["fast"]["kwh_100km"] == 18.0
    assert b["motorway"]["km"] == 0 and b["motorway"]["kwh_100km"] is None


def test_speed_bands_never_join_two_trips_or_a_hole():
    samples = [(1, _iso(0), 40.0, 5.0), (2, _iso(10), 40.0, 5.0),          # trip boundary
               (2, _iso(500), 40.0, 5.0)]                                  # 490 s gap
    assert all(b["km"] == 0 for b in si.speed_bands(samples))


def test_a_band_under_five_km_shows_its_km_but_no_rate():
    samples = [(1, _iso(i * 10), 30.0, 5.0) for i in range(20)]           # ~1.6 km
    city = si.speed_bands(samples)[0]
    assert city["km"] > 0 and city["kwh_100km"] is None


def test_fuel_bands_place_a_trip_by_its_average_speed_and_weigh_by_km():
    b = {x["key"]: x for x in si.fuel_speed_bands([
        {"km": 158, "minutes": 75, "l_100km": 7.2},       # 126 km/h → motorway
        {"km": 100, "minutes": 50, "l_100km": 6.0},       # 120 km/h → motorway
        {"km": 20, "minutes": 30, "l_100km": None}])}     # nothing burned → out
    assert b["motorway"]["trips"] == 2
    assert b["motorway"]["l_100km"] == round((158 * 7.2 + 100 * 6.0) / 258, 1)
    assert b["city"]["trips"] == 0 and b["city"]["l_100km"] is None


def test_charge_prices_against_the_break_even():
    r = si.charge_prices([
        {"ts": "a", "cost": 6.05, "kwh": 15.918, "place": "Go Electric DC"},
        {"ts": "b", "cost": 1.24, "kwh": 2.259, "place": None},
        {"ts": "c", "cost": None, "kwh": 10.0},           # unpriced: unknown, not free
        {"ts": "d", "cost": 3.0, "kwh": 0}], 0.5)
    assert [p["ts"] for p in r["points"]] == ["a", "b"]
    assert r["points"][0]["cheaper"] is True and r["points"][1]["cheaper"] is False
    assert r["cheaper_n"] == 1
    assert r["avg_eur_kwh"] == round(7.29 / 18.2, 3)


def test_charge_prices_without_a_break_even_give_no_verdict():
    r = si.charge_prices([{"ts": "a", "cost": 6.0, "kwh": 15.0}], None)
    assert r["points"][0]["cheaper"] is None and r["cheaper_n"] == 0


def test_drive_mix_prices_the_generator_km_on_the_plug():
    m = si.drive_mix(1407, 903, 168.98, 14.4, 0.389)
    assert (m["ev_km"], m["engine_km"], m["ev_pct"]) == (504, 903, 36)
    assert m["plug_cost"] == round(903 * 0.144 * 0.389)          # 51
    assert m["extra"] == 169 - 51 or m["extra"] == round(168.98 - 903 * 0.144 * 0.389)


def test_drive_mix_without_prices_still_splits_the_km():
    m = si.drive_mix(100, 0, None, None, None)
    assert m["ev_pct"] == 100 and m["extra"] is None
    assert si.drive_mix(0, 0, None, None, None) is None


def test_heating_by_temp_bands_the_climate_share():
    bands = si.heating_by_temp([
        {"temp": 7, "km": 10, "ec_kwh": 2.0, "ec_ac": 0.4},
        {"temp": 8, "km": 10, "ec_kwh": 2.0, "ec_ac": 0.2},
        {"temp": 17, "km": 20, "ec_kwh": 2.4, "ec_ac": 0},
        {"temp": None, "km": 5, "ec_kwh": 1, "ec_ac": 1}])
    assert [(b["lo"], b["trips"]) for b in bands] == [(5, 2), (15, 1)]
    assert bands[0]["kwh_100km"] == 20.0 and bands[0]["heat_pct"] == 15
    assert bands[1]["heat_100km"] == 0.0


def _curve(soc_from, soc_to, kw, step_s=30, temp=15.0):
    """A session that climbs one SoC point per sample (step_s seconds apart)."""
    n = int(soc_to - soc_from)
    return {"times": [_iso(i * step_s) for i in range(n + 1)],
            "soc": [soc_from + i for i in range(n + 1)],
            "power": [kw] * (n + 1), "battery_temp": [temp] * (n + 1)}


def test_curve_bands_give_minutes_per_ten_percent():
    cb = si.charge_curve_bands([_curve(15, 95, 30.0)])
    by = {b["lo"]: b for b in cb}
    assert by[20]["min_per_10"] == 5.0 and by[20]["kw"] == 30.0       # 10 points × 30 s
    assert by[0]["min_per_10"] is None                                # never in 0–10
    assert by[90]["min_per_10"] == 5.0                                # 90→95 is enough to read a rate
    assert by[90]["charges"] == 1
    assert si.charge_curve_bands([_curve(15, 92, 30.0)])[9]["min_per_10"] is None   # 2 points: grazed


def test_curve_bands_skip_holes_in_the_samples():
    c = _curve(20, 40, 30.0)
    c["times"][10] = _iso(10 * 30 + 3600)          # an hour-long hole mid-session
    by = {b["lo"]: b for b in si.charge_curve_bands([c])}
    assert by[20]["min_per_10"] == 5.0             # the hole is not counted as charging time


def test_charge_temps_read_the_mid_session_power():
    rows = si.charge_temps([dict(_curve(15, 95, 30.0, temp=20.0), ts="warm"),
                            dict(_curve(15, 95, 20.0, temp=8.0), ts="cold"),
                            dict(_curve(85, 95, 6.0), ts="top-up")])   # no 20–80 % stretch
    assert [r["ts"] for r in rows] == ["cold", "warm"]
    assert rows[0]["kw_mid"] == 20.0
