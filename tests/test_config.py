"""Config parsing tests."""
from datetime import time

import pytest

from tesla_drain_detective.config import ConfigError, load_settings

BASE = {
    "TESLA_CLIENT_ID": "id",
    "TESLA_CLIENT_SECRET": "secret",
    "TCC_VEHICLES": "Daily=VIN1,Second=VIN2",
}


def test_vehicle_parsing():
    s = load_settings(dict(BASE))
    assert [(v.name, v.vin) for v in s.vehicles] == [("Daily", "VIN1"), ("Second", "VIN2")]


def test_drain_defaults():
    s = load_settings(dict(BASE))
    assert s.drain_alert_pct_per_h == 0.5
    assert s.night_start == time(22, 0)
    assert s.night_end == time(7, 0)
    assert s.morning_grace_hours == 4.0
    assert s.ntfy_topic == ""
    assert s.poll_interval_seconds == 600
    assert s.dry_run is False


def test_night_window_parsing():
    s = load_settings({**BASE, "TCC_NIGHT_START": "23:30", "TCC_NIGHT_END": "06:15",
                       "TCC_DRAIN_ALERT_PCT_PER_H": "1.25",
                       "TCC_NTFY_TOPIC": "secret-topic-123",
                       "TCC_MORNING_GRACE_HOURS": "2"})
    assert s.night_start == time(23, 30)
    assert s.night_end == time(6, 15)
    assert s.drain_alert_pct_per_h == 1.25
    assert s.ntfy_topic == "secret-topic-123"
    assert s.morning_grace_hours == 2.0


def test_bad_time_rejected():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TCC_NIGHT_START": "25:00"})
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TCC_NIGHT_END": "not-a-time"})


def test_nonpositive_threshold_rejected():
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TCC_DRAIN_ALERT_PCT_PER_H": "0"})
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TCC_DRAIN_ALERT_PCT_PER_H": "-1"})
    with pytest.raises(ConfigError):
        load_settings({**BASE, "TCC_DRAIN_ALERT_PCT_PER_H": "abc"})


def test_missing_vehicles_rejected():
    env = dict(BASE)
    del env["TCC_VEHICLES"]
    with pytest.raises(ConfigError):
        load_settings(env)


def test_simulate_needs_no_config():
    s = load_settings({}, require_auth=False)
    assert s.vehicles == []
