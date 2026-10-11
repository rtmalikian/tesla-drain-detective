"""End-to-end tests: fake fleet over virtual time through the real scheduler.

Core invariants:
  1. An alert fires exactly once per night when drain exceeds the
     threshold, naming the culprit — and never for a quiet car.
  2. The app NEVER wakes the car (wake_up_calls must stay empty).
  3. Only checkpoint reads hit the API (no polling).
"""
from datetime import datetime, timedelta

from tesla_drain_detective.config import Settings, VehicleConfig
from tesla_drain_detective.drain import DrainDecider, SendDrainAlert
from tesla_drain_detective.simulator import FakeVehicle, run_simulation


def make_settings(tmp_path, **over):
    kw = dict(
        vehicles=[VehicleConfig(name="Model Y", vin="VIN1"),
                  VehicleConfig(name="Model 3", vin="VIN2")],
        drain_alert_pct_per_h=0.5,
        ntfy_topic="",
        state_file=str(tmp_path / "state.json"),
    )
    kw.update(over)
    return Settings(**kw)


def alert_actions(report, name=None):
    acts = [a for t in report["timeline"] for a in t["actions"]
            if a[0] == "SendDrainAlert"]
    return [a for a in acts if name is None or a[1] == name]


def test_alert_once_for_sentry_car_never_for_quiet_car(tmp_path):
    start = datetime.now().astimezone().replace(hour=21, minute=30, second=0, microsecond=0)
    vehicles = [
        FakeVehicle(vin="VIN1", name="Model Y", soc=71.0, sentry_mode=True),
        FakeVehicle(vin="VIN2", name="Model 3", soc=64.0),
    ]
    settings = make_settings(tmp_path)
    report = run_simulation(vehicles, DrainDecider(settings), settings, start, hours=11.0)

    y_alerts = alert_actions(report, "Model Y")
    assert len(y_alerts) == 1, f"expected exactly one Model Y alert, got {y_alerts}"
    assert y_alerts[0][2] > 0.5  # drain rate above threshold
    assert not alert_actions(report, "Model 3"), "quiet car must not alert"

    # culprit named in the decision note
    notes = " ".join(t["note"] for t in report["timeline"])
    assert "Sentry Mode" in notes


def test_never_wakes_the_car(tmp_path):
    start = datetime.now().astimezone().replace(hour=21, minute=30, second=0, microsecond=0)
    vehicles = [FakeVehicle(vin="VIN1", name="Model Y", soc=71.0, sentry_mode=True)]
    settings = make_settings(tmp_path, vehicles=[VehicleConfig(name="Model Y", vin="VIN1")])
    report = run_simulation(vehicles, DrainDecider(settings), settings, start, hours=11.0)
    assert report["wake_up_calls"] == [], "drain measurement must never wake the car"
    assert alert_actions(report, "Model Y"), "alert should still fire without any wake"


def test_only_checkpoint_reads(tmp_path):
    """No polling: exactly 2 vehicles x 2 checkpoints = 4 data calls/night."""
    start = datetime.now().astimezone().replace(hour=21, minute=30, second=0, microsecond=0)
    vehicles = [
        FakeVehicle(vin="VIN1", name="Model Y", soc=71.0, sentry_mode=True),
        FakeVehicle(vin="VIN2", name="Model 3", soc=64.0),
    ]
    settings = make_settings(tmp_path)
    report = run_simulation(vehicles, DrainDecider(settings), settings, start, hours=11.0)
    assert report["data_calls"] == 4, f"expected 4 checkpoint reads, got {report['data_calls']}"


def test_asleep_all_night_no_alert_no_crash(tmp_path):
    start = datetime.now().astimezone().replace(hour=21, minute=30, second=0, microsecond=0)
    vehicles = [FakeVehicle(vin="VIN1", name="Model Y", soc=71.0,
                            sentry_mode=True, asleep=True)]
    settings = make_settings(tmp_path, vehicles=[VehicleConfig(name="Model Y", vin="VIN1")])
    report = run_simulation(vehicles, DrainDecider(settings), settings, start, hours=11.0)
    assert not alert_actions(report), "no baseline -> no alert"
    assert report["wake_up_calls"] == []


def test_quiet_night_no_alerts(tmp_path):
    start = datetime.now().astimezone().replace(hour=21, minute=30, second=0, microsecond=0)
    vehicles = [FakeVehicle(vin="VIN1", name="Model Y", soc=71.0)]
    settings = make_settings(tmp_path, vehicles=[VehicleConfig(name="Model Y", vin="VIN1")])
    report = run_simulation(vehicles, DrainDecider(settings), settings, start, hours=11.0)
    assert not alert_actions(report)
    notes = " ".join(t["note"] for t in report["timeline"])
    assert "drain normal" in notes
