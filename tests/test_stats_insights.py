"""Fork: the Statistics page's "your questions" section (web/stats_insights.py).

Shapes taken from the owner's own B10 REEV, Oct–Nov 2026: DC sessions at one public column that
fall from ~31 kW to ~6 kW above 90 %, town trips at ~14 kWh/100 km, a motorway day to Padova."""
import pytest
from datetime import datetime, timedelta, timezone

import stats_insights as si

T0 = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)


def _iso(s):
    return (T0 + timedelta(seconds=s)).isoformat()


def _bands(samples, fuel=None, **kw):
    return {x["key"]: x for x in si.window_bands(si.windows(samples, fuel), **kw)}


def test_windows_integrate_power_and_speed_per_band():
    # 10 min at a steady 36 km/h drawing 6 kW, then 10 min at 100 km/h drawing 18 kW: no tank
    samples = [(1, _iso(i * 10), 36.0, 6.0) for i in range(61)]
    samples += [(2, _iso(i * 10), 100.0, 18.0) for i in range(61)]
    b = _bands(samples)
    assert b["city"]["km"] == 6.0 and b["city"]["kwh_100km"] == 16.7      # 1 kWh over 6 km
    assert b["fast"]["km"] == 16.7 and b["fast"]["kwh_100km"] == 18.0
    assert b["motorway"]["km"] == 0 and b["motorway"]["kwh_100km"] is None
    assert b["city"]["windows"] == 2 and b["city"]["trips"][0]["id"] == 1


def test_windows_never_join_two_trips_or_a_hole():
    samples = [(1, _iso(0), 40.0, 5.0), (2, _iso(10), 40.0, 5.0),          # trip boundary
               (2, _iso(500), 40.0, 5.0)]                                  # 490 s gap
    assert si.windows(samples) == []


def test_a_window_too_short_is_dropped():
    samples = [(1, _iso(i * 10), 30.0, 5.0) for i in range(20)]           # 190 s < 0.8 × 5 min
    assert si.windows(samples) == []


def _tank(litres_per_min, start=40.0, minutes=60):
    return [((T0 + timedelta(seconds=10 * i)).timestamp(), start - litres_per_min * i / 6)
            for i in range(minutes * 6 + 1)]


def test_half_motorway_half_country_road_splits_into_both_bands():
    # 5/11/2026 shape: 20 min of country road at 70 km/h, then 30 min at 125 km/h, generator on
    # throughout. By its average (≈102 km/h) the whole trip was one band; by windows it is two.
    samples = [(49, _iso(i * 10), 70.0, -2.0) for i in range(121)]
    samples += [(49, _iso(1200 + i * 10), 125.0, 0.0) for i in range(1, 181)]
    b = _bands(samples, _tank(0.2))
    assert b["road"]["gen_windows"] == 4 and b["motorway"]["gen_windows"] == 6
    assert b["motorway"]["gen_km"] == pytest.approx(62.5, abs=0.2)
    assert b["motorway"]["l_100km"] == pytest.approx(6.0 / 62.5 * 100, abs=0.1)
    # the generator put 2 kW back into the battery on the country road: said, not folded in
    assert b["road"]["batt_kwh"] == pytest.approx(-0.7, abs=0.05)
    assert b["fast"]["gen_windows"] == 0


def test_an_electric_stretch_inside_a_generator_trip_is_electric():
    # 13 min at 115 km/h on the battery (tank flat), then the generator starts
    tank = [((T0 + timedelta(seconds=10 * i)).timestamp(), 49.47 if i < 78 else 49.47 - 0.03 * (i - 78))
            for i in range(200)]
    samples = [(49, _iso(i * 10), 116.0, 24.0) for i in range(181)]
    b = _bands(samples, tank)
    m = b["motorway"]
    assert m["windows"] == 2 and m["kwh_100km"] == pytest.approx(24 / 116 * 100, abs=0.1)
    assert m["gen_windows"] == 4                 # the one the engine started in burned real petrol


def test_windows_of_a_merged_piece_list_the_drive_it_belongs_to():
    samples = [(36, _iso(i * 10), 125.0, 0.0) for i in range(61)]
    b = _bands(samples, _tank(0.2), head_of={36: 35})
    assert [tr["id"] for tr in b["motorway"]["trips"]] == [35]


def test_each_trip_in_a_band_carries_its_own_rate():
    # the 5/11 shape: one trip at 8.4 L/100 km, the other at 11.1, both in the motorway band
    tank = _tank(0.2) + [(t + 3600, l) for t, l in _tank(0.25, start=28.0)[1:]]
    samples = [(49, _iso(i * 10), 120.0, 0.0) for i in range(61)]
    samples += [(51, _iso(3600 + i * 10), 120.0, 0.0) for i in range(61)]
    rates = {tr["id"]: tr["l_100km"] for tr in _bands(samples, tank)["motorway"]["trips"]}
    assert rates[49] == pytest.approx(10.0, abs=0.1) and rates[51] == pytest.approx(12.5, abs=0.1)


def test_without_tank_data_a_range_extender_window_is_not_called_electric():
    samples = [(1, _iso(i * 10), 60.0, 10.0) for i in range(61)]
    b = _bands(samples, fuel=[])                 # a tank, but no level recorded then
    assert b["road"]["windows"] == 0 and b["road"]["gen_windows"] == 0


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


def test_a_descent_is_levelled_out_of_the_electric_consumption():
    # 7/11/2026 shape: 10 min at 100 km/h on the battery down the Apennines, 6 kW, −300 m
    samples = [(70, _iso(i * 10), 100.0, 6.0, 600.0 - 300 * i / 60) for i in range(61)]
    fast = _bands(samples)["fast"]
    raw = 1.0 / 16.7 * 100                                   # 1 kWh over 16.7 km
    lift = 300 * si._KWH_PER_M / 16.7 * 100                   # what the descent lent
    assert fast["kwh_100km"] == pytest.approx(raw + lift, abs=0.2)


def _road(trip, t0, v, kw, n=61):
    return [(trip, _iso(t0 + i * 10), v, kw) for i in range(n)]


def test_litres_per_kwh_come_from_one_band_only():
    # road band: 10 min on the battery at 18 kWh/100 km, then 10 min on the generator at 0.2 L/min
    # with the battery flat → the generator made 18 kWh/100 km × 10 km = 1.8 kWh for 2 L
    samples = _road(1, 0, 60.0, 10.8) + _road(2, 3600, 60.0, 0.0)
    tank = [((T0 + timedelta(seconds=10 * i)).timestamp(), 40.0 if i <= 360 else 40.0 - 0.2 * (i - 360) / 6)
            for i in range(430)]
    road = _bands(samples, tank)["road"]
    assert road["kwh_100km"] == 18.0 and road["l_100km"] == 20.0
    assert road["g_l_kwh"] == pytest.approx(2.0 / 1.8, abs=0.01)
    # and the motorway, never driven on the battery, gets no ratio borrowed from the road
    assert _bands(samples, tank)["motorway"]["g_l_kwh"] is None


def test_battery_charged_by_the_generator_counts_as_energy_it_made():
    samples = _road(1, 0, 60.0, 10.8) + _road(2, 3600, 60.0, -6.0)    # 1 kWh into the pack
    tank = [((T0 + timedelta(seconds=10 * i)).timestamp(), 40.0 if i <= 360 else 40.0 - 0.2 * (i - 360) / 6)
            for i in range(430)]
    road = _bands(samples, tank)["road"]
    assert road["g_l_kwh"] == pytest.approx(2.0 / 2.8, abs=0.01)


def test_charge_efficiency_leaves_out_an_impossible_charge():
    rows = [(15.0, 15.918), (15.416, 16.33), (3.835, 13.639), (None, 5.0)]   # 3rd: 28 %, a typo
    eta = si.charge_efficiency(rows)
    assert eta["n"] == 2 and eta["eta"] == pytest.approx(30.416 / 32.248, abs=0.001)
    assert si.charge_efficiency([]) is None


def test_breakeven_is_petrol_price_times_litres_per_kwh_times_efficiency():
    bands = [{"key": "road", "lo": 50, "hi": 90, "litres": 17.6, "produced_kwh": 42.9,
              "g_l_kwh": 0.41, "gen_km": 264.0, "km": 115.0},
             {"key": "motorway", "lo": 115, "hi": 999, "litres": 24.0, "produced_kwh": None,
              "g_l_kwh": None, "gen_km": 245.0, "km": 0.0}]
    be = si.breakevens(bands, 1.974, {"eta": 0.93, "n": 7})
    assert be["value"] == pytest.approx(17.6 / 42.9 * 1.974 * 0.93, abs=0.001)
    assert be["bands"][0]["value"] == pytest.approx(0.41 * 1.974 * 0.93, abs=0.001)
    assert be["bands"][1]["value"] is None                 # no electric motorway: no figure
    assert si.breakevens(bands, None, None) is None


def test_the_plug_price_of_generator_kilometres_uses_the_energy_the_generator_made():
    # 1134 km at 9.0 L/100 km, 0.412 L/kWh, 93 % into the pack, 0.389 €/kWh billed
    elec_100 = 9.0 / 0.412 / 0.93
    mix = si.drive_mix(1710, 1134, 203, elec_100, 0.389)
    assert mix["plug_cost"] == round(1134 * elec_100 / 100 * 0.389)       # ≈ 103 €, not 64


def test_each_charge_is_judged_on_the_kwh_that_reached_the_battery():
    r = si.charge_prices([
        {"ts": "dc", "cost": 6.05, "kwh": 15.918, "battery_kwh": 15.0},
        {"ts": "ac", "cost": 7.5, "kwh": 13.639, "battery_kwh": 11.844},
        {"ts": "typed", "cost": 3.0, "kwh": 10.0}], 0.76, 0.93)
    dc, ac, typed = r["points"]
    assert dc["eur_kwh_batt"] == round(6.05 / 15.0, 3) and ac["eur_kwh_batt"] == round(7.5 / 11.844, 3)
    assert typed["battery_kwh"] == 9.3                       # no battery figure: the average η
    assert r["breakeven_batt"] == round(0.76 / 0.93, 3)      # € per kWh the generator puts in
    assert [p["cheaper"] for p in r["points"]] == [True, True, True]
    assert r["avg_eur_kwh_batt"] == round(16.55 / (15.0 + 11.8 + 9.3), 3)
