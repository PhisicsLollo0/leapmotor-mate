"""Fork: where the cabin temperature is heading (db_reader._cabin_trend).

Shapes taken from a real evening (23/10): climate switched on at 18 °C with 13 °C outside, the
cabin first DROPPED to 15.5 (fan blowing cold air) and then climbed in 0.5 °C steps."""
from datetime import datetime, timedelta, timezone

import db_reader

NOW = datetime(2026, 10, 23, 19, 10, tzinfo=timezone.utc)


def _rows(*pts, target=21.0):
    """pts: (seconds_ago, inside, climate_on), any order → newest-first rows."""
    out = [{"recorded_at": (NOW - timedelta(seconds=s)).isoformat(), "inside_temp": t,
            "climate_on": on, "climate_target_temp": target} for s, t, on in pts]
    return sorted(out, key=lambda r: r["recorded_at"], reverse=True)


def test_climate_on_and_climbing_gives_rate_and_eta():
    tr = db_reader._cabin_trend(_rows((0, 17.5, 1), (120, 16.5, 1), (240, 15.5, 1)), NOW)
    assert tr["on"] and tr["rate"] == 0.5          # 2 °C over 4 min
    assert tr["eta_min"] == 7                      # 3.5 °C to go at 0.5/min


def test_first_minutes_of_a_run_give_no_eta():
    # just switched on: the run is shorter than the minimum span, older polls had it off
    tr = db_reader._cabin_trend(_rows((0, 16.0, 1), (30, 16.5, 1), (90, 18.0, 0), (400, 18.0, 0)), NOW)
    assert tr["on"] and tr["rate"] is None and tr["eta_min"] is None


def test_falling_while_heating_gives_no_eta():
    tr = db_reader._cabin_trend(_rows((0, 15.5, 1), (180, 16.5, 1)), NOW)
    assert tr["rate"] < 0 and tr["eta_min"] is None


def test_at_target():
    tr = db_reader._cabin_trend(_rows((0, 21.0, 1), (180, 19.0, 1)), NOW)
    assert tr["reached"]


def test_climate_off_drift_needs_a_full_degree():
    assert db_reader._cabin_trend(_rows((0, 18.5, 0), (1500, 18.0, 0)), NOW) is None
    tr = db_reader._cabin_trend(_rows((0, 16.0, 0), (1500, 18.0, 0)), NOW)
    assert tr == {"on": False, "delta": -2.0, "minutes": 25}


def test_asleep_car_has_no_trend():
    assert db_reader._cabin_trend(_rows((900, 17.5, 1), (1100, 16.0, 1)), NOW) is None


def test_missing_readings_are_skipped():
    rows = _rows((0, 17.5, 1), (60, None, 1), (240, 15.5, 1))
    assert db_reader._cabin_trend(rows, NOW)["rate"] == 0.5
