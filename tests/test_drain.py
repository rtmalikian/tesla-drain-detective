"""Unit tests for the drain decision logic (pure, `now` injected)."""
from datetime import datetime, timedelta

from tesla_drain_detective.config import Settings, VehicleConfig
from tesla_drain_detective.drain import (
    DrainDecider,
    SendDrainAlert,
    VehicleDrainState,
    build_alert,
    identify_culprits,
)

# Night window: 22:00 -> 07:00 (defaults).
EVENING = datetime.now().astimezone().replace(hour=22, minute=5, second=0, microsecond=0)
MORNING = (EVENING + timedelta(days=1)).replace(hour=7, minute=5)


def make_settings(**over):
    kw = dict(
        vehicles=[VehicleConfig(name="Model Y", vin="VIN1")],
        drain_alert_pct_per_h=0.5,
        ntfy_topic="test-topic",
    )
    kw.update(over)
    return Settings(**kw)


def state(**over):
    kw = dict(vin="VIN1", name="Model Y", soc=70.0, sentry_mode=False,
              climate_keeper_mode="off", cop_state="Off", parked=True, asleep=False)
    kw.update(over)
    return VehicleDrainState(**kw)


def record_baseline(decider, soc=70.0, at=EVENING, **st_over):
    dec = decider.decide([state(soc=soc, **st_over)], at)
    assert "baseline" in dec.note.lower()
    return dec


# -- night math ----------------------------------------------------------

def test_night_id_boundaries():
    d = DrainDecider(make_settings())
    day = datetime.now().astimezone().date()
    assert d.night_id_for(EVENING.replace(hour=21, minute=59)) == (day - timedelta(days=1)).isoformat()
    assert d.night_id_for(EVENING.replace(hour=22, minute=0)) == day.isoformat()
    assert d.night_id_for(MORNING.replace(hour=6, minute=59)) == day.isoformat()


def test_in_night_window_wraps_midnight():
    d = DrainDecider(make_settings())
    nid = d.night_id_for(EVENING)
    assert d.in_night_window(nid, EVENING.replace(tzinfo=None))
    assert d.in_night_window(nid, MORNING.replace(hour=3, tzinfo=None))
    assert not d.in_night_window(nid, EVENING.replace(hour=21, tzinfo=None))
    assert not d.in_night_window(nid, MORNING.replace(hour=8, tzinfo=None))


def test_baseline_due_only_in_window_before_recording():
    d = DrainDecider(make_settings())
    assert d.baseline_due("VIN1", EVENING)
    assert not d.baseline_due("VIN1", EVENING.replace(hour=21, minute=0))
    record_baseline(d)
    assert not d.baseline_due("VIN1", EVENING + timedelta(minutes=30))


def test_morning_due_gating():
    d = DrainDecider(make_settings())
    assert not d.morning_due("VIN1", EVENING)          # no baseline yet
    record_baseline(d)
    assert not d.morning_due("VIN1", MORNING - timedelta(minutes=10))
    assert d.morning_due("VIN1", MORNING)              # due now
    assert d.morning_due("VIN1", MORNING + timedelta(hours=3))   # inside grace
    assert not d.morning_due("VIN1", MORNING + timedelta(hours=5))  # past grace


# -- baseline pass -------------------------------------------------------

def test_baseline_not_recorded_when_asleep():
    d = DrainDecider(make_settings())
    dec = d.decide([state(asleep=True, soc=None)], EVENING)
    assert "asleep" in dec.note
    assert d.nights["VIN1"]["baseline_soc"] is None
    # retried later when awake
    dec = d.decide([state(soc=70.0)], EVENING + timedelta(minutes=20))
    assert d.nights["VIN1"]["baseline_soc"] == 70.0


def test_baseline_not_recorded_when_driving():
    d = DrainDecider(make_settings())
    dec = d.decide([state(parked=False, soc=70.0)], EVENING)
    assert "not parked" in dec.note
    assert d.nights["VIN1"]["baseline_soc"] is None


def test_baseline_records_culprits_present_at_night():
    d = DrainDecider(make_settings())
    d.decide([state(soc=70.0, sentry_mode=True)], EVENING)
    assert d.nights["VIN1"]["baseline_culprits"] == ["Sentry Mode"]


# -- morning pass --------------------------------------------------------

def test_alert_fires_above_threshold_with_culprit():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0, sentry_mode=True)
    dec = d.decide([state(soc=64.0, sentry_mode=True)], MORNING)  # 6% over ~9h = 0.67%/h
    alerts = [a for a in dec.actions if isinstance(a, SendDrainAlert)]
    assert len(alerts) == 1
    assert alerts[0].culprits == ["Sentry Mode"]
    assert alerts[0].drain_pct_per_h > 0.5
    assert "Sentry Mode" in alerts[0].message


def test_no_alert_below_threshold():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0)
    dec = d.decide([state(soc=69.0)], MORNING)  # 1% over ~9h = 0.11%/h
    assert not [a for a in dec.actions if isinstance(a, SendDrainAlert)]
    assert "normal" in dec.note
    assert d.nights["VIN1"]["evaluated"] is True


def test_no_alert_at_exact_threshold():
    d = DrainDecider(make_settings())  # threshold 0.5%/h
    base = EVENING
    morning = base + timedelta(hours=10)
    d.decide([state(soc=70.0)], base)
    dec = d.decide([state(soc=65.0)], morning)  # exactly 0.5%/h -> strict >, no alert
    assert not [a for a in dec.actions if isinstance(a, SendDrainAlert)]


def test_no_alert_when_charged_overnight():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=60.0)
    dec = d.decide([state(soc=85.0)], MORNING)
    assert not [a for a in dec.actions if isinstance(a, SendDrainAlert)]
    assert "charged" in dec.note
    assert d.nights["VIN1"]["evaluated"] is True


def test_no_alert_when_driven_since_baseline():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0)
    dec = d.decide([state(soc=70.0, parked=False)], MORNING)
    assert not [a for a in dec.actions if isinstance(a, SendDrainAlert)]
    assert "discarded" in dec.note


def test_alert_not_refired_after_delivery():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0, sentry_mode=True)
    dec = d.decide([state(soc=64.0, sentry_mode=True)], MORNING)
    assert len([a for a in dec.actions if isinstance(a, SendDrainAlert)]) == 1
    d.mark_evaluated("VIN1", MORNING, alerted=True)
    dec2 = d.decide([state(soc=64.0, sentry_mode=True)], MORNING + timedelta(minutes=10))
    assert not [a for a in dec2.actions if isinstance(a, SendDrainAlert)]


def test_alert_retried_until_delivered():
    """A failed send must NOT mark the night evaluated (scheduler retries)."""
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0, sentry_mode=True)
    dec = d.decide([state(soc=64.0, sentry_mode=True)], MORNING)
    assert dec.actions  # action fired...
    assert d.nights["VIN1"]["evaluated"] is False  # ...but night stays open
    dec2 = d.decide([state(soc=64.0, sentry_mode=True)], MORNING + timedelta(minutes=10))
    assert len([a for a in dec2.actions if isinstance(a, SendDrainAlert)]) == 1


def test_morning_asleep_retries():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0)
    dec = d.decide([state(asleep=True, soc=None)], MORNING)
    assert not dec.actions
    assert "retrying" in dec.note
    assert d.nights["VIN1"]["evaluated"] is False


def test_past_grace_unresolved():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0)
    assert not d.past_grace_unresolved("VIN1", MORNING + timedelta(hours=3))
    assert d.past_grace_unresolved("VIN1", MORNING + timedelta(hours=5))
    d.mark_abandoned("VIN1", MORNING + timedelta(hours=5), "never woke")
    assert not d.past_grace_unresolved("VIN1", MORNING + timedelta(hours=6))


def test_night_rollover_resets_state():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0)
    next_evening = EVENING + timedelta(days=1)
    dec = d.decide([state(soc=68.0)], next_evening)
    assert "baseline 68.0% recorded" in dec.note
    assert d.nights["VIN1"]["night_id"] == d.night_id_for(next_evening)


def test_state_persistence_round_trip():
    d = DrainDecider(make_settings())
    record_baseline(d, soc=70.0, sentry_mode=True)
    d2 = DrainDecider(make_settings())
    d2.from_dict(d.to_dict())
    assert d2.nights["VIN1"]["baseline_soc"] == 70.0
    assert d2.morning_due("VIN1", MORNING)


# -- culprit identification ----------------------------------------------

def test_culprits_sentry_keeper_cop():
    st = state(sentry_mode=True, climate_keeper_mode="Dog", cop_state="On")
    assert identify_culprits(st) == ["Sentry Mode", "climate keeper (Dog)",
                                     "Cabin Overheat Protection"]


def test_culprits_empty_when_all_off():
    assert identify_culprits(state()) == []


def test_alert_message_names_no_obvious_culprit():
    title, message = build_alert("Model Y", [], 0.8, 7.0, 9.0, 0.5, 70.0, 63.0)
    assert "Model Y" in title
    assert "No obvious culprit" in message
    assert "0.80%/h" in message
